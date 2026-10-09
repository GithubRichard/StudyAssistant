"""数学错题示意图：AI 按题干重绘 SVG 矢量图。

背景：数学几何题的题干常引用"图1""图2"，纯文本错题不利于阅读。
方案：模型按题干描述输出极简 JSON 图形描述，服务端拼成 SVG（矢量，清晰可缩放），
存入台账，错题详情页渲染。AI 重绘仅供参考，不作为判分依据。
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET

log = logging.getLogger("studyassistant.diagram")

DIAGRAM_SYSTEM = """你是几何示意图助手。根据题目文字，用 JSON 描述几何图形。
只输出 JSON 数组，不要任何其他文字，不要 markdown 代码块。
每个图形是 {"t": 类型, ...}：
- rect 矩形/正方形: {"t":"rect","x":左,"y":上,"w":宽,"h":高,"fill":"none|gray"}
- line 线段: {"t":"line","x1":..,"y1":..,"x2":..,"y2":..}
- circle 圆: {"t":"circle","cx":..,"cy":..,"r":..}
- poly 多边形: {"t":"poly","pts":[[x1,y1],[x2,y2],...],"fill":"none|gray"}
- text 标注: {"t":"text","x":..,"y":..,"s":"A"}
画布 400x300，坐标整数。只画题目明确描述的图形，不确定的不画。
纯代数题（无图形描述）输出 []。
示例：[{"t":"rect","x":50,"y":50,"w":100,"h":100,"fill":"none"},{"t":"text","x":45,"y":45,"s":"A"}]
"""

_MAX_SVG_BYTES = 100 * 1024


def shapes_to_svg(shapes: list) -> str:
    """JSON 图形描述转 SVG 字符串。"""
    parts = [
        "<svg xmlns='http://www.w3.org/2000/svg' width='400' height='300' "
        "viewBox='0 0 400 300'>",
        "<rect x='0' y='0' width='400' height='300' fill='white'/>",
    ]
    for sh in shapes:
        if not isinstance(sh, dict):
            continue
        t = sh.get("t")
        try:
            if t == "rect":
                x, y, w, h = int(sh["x"]), int(sh["y"]), int(sh["w"]), int(sh["h"])
                fill = "gray" if sh.get("fill") == "gray" else "none"
                parts.append(
                    f"<rect x='{x}' y='{y}' width='{w}' height='{h}' "
                    f"fill='{fill}' stroke='black' stroke-width='2'/>"
                )
            elif t == "line":
                x1, y1 = int(sh["x1"]), int(sh["y1"])
                x2, y2 = int(sh["x2"]), int(sh["y2"])
                parts.append(
                    f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' "
                    f"stroke='black' stroke-width='2'/>"
                )
            elif t == "circle":
                cx, cy, r = int(sh["cx"]), int(sh["cy"]), int(sh["r"])
                parts.append(
                    f"<circle cx='{cx}' cy='{cy}' r='{r}' "
                    f"fill='none' stroke='black' stroke-width='2'/>"
                )
            elif t == "poly":
                pts = sh.get("pts") or []
                if len(pts) >= 3:
                    p = " ".join(f"{int(x)},{int(y)}" for x, y in pts[:20])
                    fill = "gray" if sh.get("fill") == "gray" else "none"
                    parts.append(
                        f"<polygon points='{p}' fill='{fill}' "
                        f"stroke='black' stroke-width='2'/>"
                    )
            elif t == "text":
                x, y = int(sh["x"]), int(sh["y"])
                s = str(sh.get("s", ""))[:10].replace("<", "").replace(">", "")
                parts.append(
                    f"<text x='{x}' y='{y}' font-size='12' "
                    f"font-family='sans-serif'>{s}</text>"
                )
        except (KeyError, ValueError, TypeError):
            continue
    parts.append("</svg>")
    return "".join(parts)


def sanitize_svg(raw: str) -> str:
    """清洗并校验 SVG。合法返回 SVG 字符串，否则返回 ""。"""
    if not raw:
        return ""
    m = re.search(r"<svg\b.*?</svg>", raw, re.S | re.I)
    if not m:
        log.warning("示意图未含 <svg> 标签，已丢弃（前200字）：%s", raw[:200])
        return ""
    svg = m.group(0)
    if len(svg.encode("utf-8")) > _MAX_SVG_BYTES:
        log.warning("示意图过大，已丢弃")
        return ""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        log.warning("示意图 XML 非法，已丢弃：%s", e)
        return ""
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        log.warning("示意图根节点不是 svg，已丢弃")
        return ""
    lowered = svg.lower()
    if "<script" in lowered:
        log.warning("示意图含脚本，已丢弃")
        return ""
    if "javascript:" in lowered:
        log.warning("示意图含 javascript:，已丢弃")
        return ""
    if re.search(r"\son\w+\s*=", lowered):
        log.warning("示意图含事件属性，已丢弃")
        return ""
    return svg


async def generate_diagram_svg(stem: str, providers) -> str:
    """按题干生成示意图 SVG。失败返回 ""（fail-open，不阻断台账写入）。
    
    providers: 单个 provider 或 provider 列表。列表时逐个尝试，
    直到某个返回非空（DeepSeek 等推理模型可能把输出全放在 thinking 里导致 text 为空）。
    """
    stem = (stem or "").strip()
    if not stem:
        return ""
    if not isinstance(providers, (list, tuple)):
        providers = [providers]
    for prov in providers:
        try:
            outcome = await prov.complete_text(
                DIAGRAM_SYSTEM,
                f"题目：\n{stem}",
                max_tokens=2000,
            )
            text = (outcome.text or "").strip()
            if not text:
                log.warning("示意图模型 %s 返回空，尝试下一个",
                            getattr(prov, "name", "?"))
                continue
            # 提取 JSON 数组
            m = re.search(r"\[.*\]", text, re.S)
            if not m:
                log.warning("示意图未含 JSON 数组，已丢弃（前200字）：%s", text[:200])
                continue
            try:
                shapes = json.loads(m.group(0))
            except json.JSONDecodeError as e:
                log.warning("示意图 JSON 非法，已丢弃：%s", e)
                continue
            if not isinstance(shapes, list) or not shapes:
                continue
            svg = shapes_to_svg(shapes)
            svg = sanitize_svg(svg)
            if svg:
                return svg
        except Exception as e:
            log.warning("示意图生成失败，已跳过：%s", e)
            continue
    return ""


def is_math_subject(subject: str) -> bool:
    return "数学" in (subject or "")


_GEO_KEYWORDS = (
    "图", "正方形", "长方形", "三角形", "圆形", "圆", "梯形",
    "平行四边形", "菱形", "几何", "∠", "△", "⊙", "阴影", "面积",
)


def stem_looks_geometric(stem: str) -> bool:
    s = stem or ""
    return any(k in s for k in _GEO_KEYWORDS)


def should_attempt_diagram(subject: str, stem: str = "") -> bool:
    """是否值得尝试生成示意图：数学科目直接生成；科目为空时按题干关键词判断。"""
    if is_math_subject(subject):
        return True
    if not (subject or "").strip():
        return stem_looks_geometric(stem)
    return False
