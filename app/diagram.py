"""数学错题示意图：AI 按题干重绘 SVG 矢量图。

背景：数学几何题的题干常引用"图1""图2"，纯文本错题不利于阅读。
方案：模型按题干描述输出极简 JSON 图形描述，服务端拼成 SVG（矢量，清晰可缩放），
存入台账，错题详情页渲染。AI 重绘仅供参考，不作为判分依据。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import escape
from typing import Optional

from .config import DiagramConfig

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
你只负责画图，不要求解、证明、计算答案或猜测原图的点位和阴影。
最多 60 个图形，直接输出最终 JSON。若依赖原图但文字不足以确定结构，输出 []。
纯代数题（无图形描述）输出 []。
示例：[{"t":"rect","x":50,"y":50,"w":100,"h":100,"fill":"none"},{"t":"text","x":45,"y":45,"s":"A"}]
"""

_MAX_SVG_BYTES = 100 * 1024
# 台账详情页（GET 请求）里的懒生成要同步等结果，不能套用自动绘图的长时限。
LAZY_DEADLINE_SECONDS = 90.0


@dataclass(frozen=True)
class DiagramResult:
    status: str
    svg: str = ""
    reason: str = ""
    message: str = ""
    provider: str = ""
    attempts: int = 0

    def metadata(self) -> dict:
        return {"status": self.status, "reason": self.reason,
                "message": self.message, "provider": self.provider,
                "attempts": self.attempts}


def apply_diagram_result(question: dict, result: DiagramResult) -> None:
    question["diagram"] = result.metadata()
    if result.svg:
        question["diagram_svg"] = result.svg


def shapes_to_svg(shapes: list) -> str:
    """JSON 图形描述转 SVG 字符串。"""
    parts = [
        "<svg xmlns='http://www.w3.org/2000/svg' width='400' height='300' "
        "viewBox='0 0 400 300'>",
        "<rect x='0' y='0' width='400' height='300' fill='white'/>",
    ]
    geometry_count = 0
    for sh in shapes:
        if not isinstance(sh, dict):
            return ""
        t = sh.get("t")
        try:
            if t == "rect":
                x, y, w, h = int(sh["x"]), int(sh["y"]), int(sh["w"]), int(sh["h"])
                if w <= 0 or h <= 0:
                    return ""
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
                if r <= 0:
                    return ""
                parts.append(
                    f"<circle cx='{cx}' cy='{cy}' r='{r}' "
                    f"fill='none' stroke='black' stroke-width='2'/>"
                )
            elif t == "poly":
                pts = sh.get("pts") or []
                if 3 <= len(pts) <= 20:
                    p = " ".join(f"{int(x)},{int(y)}" for x, y in pts)
                    fill = "gray" if sh.get("fill") == "gray" else "none"
                    parts.append(
                        f"<polygon points='{p}' fill='{fill}' "
                        f"stroke='black' stroke-width='2'/>"
                    )
                else:
                    return ""
            elif t == "text":
                x, y = int(sh["x"]), int(sh["y"])
                s = escape(str(sh.get("s", ""))[:10])
                parts.append(
                    f"<text x='{x}' y='{y}' font-size='12' "
                    f"font-family='sans-serif'>{s}</text>"
                )
            else:
                return ""
            if t != "text":
                geometry_count += 1
        except (KeyError, ValueError, TypeError):
            # 不把部分残缺图当作完整成功，更不能只返回白色背景。
            return ""
    if not geometry_count:
        return ""
    parts.append("</svg>")
    return "".join(parts)


def sanitize_svg(raw: str) -> str:
    """清洗并校验 SVG。合法返回 SVG 字符串，否则返回 ""。"""
    if not raw or len(raw.encode("utf-8")) > _MAX_SVG_BYTES:
        return ""
    m = re.search(r"<svg\b(?:[^>]*?/\s*>|.*?</svg\s*>)", raw, re.S | re.I)
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
    # SVG 会以内联 HTML 显示：只接受静态几何元素，拒绝外链、动画及异名空间脚本。
    tags = {"svg", "g", "rect", "line", "circle", "ellipse", "polygon",
            "polyline", "path", "text", "tspan", "title", "desc"}
    attrs = {"id", "width", "height", "viewBox", "x", "y", "x1", "y1", "x2", "y2",
             "cx", "cy", "r", "rx", "ry", "points", "d", "fill", "stroke",
             "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin",
             "font-size", "font-family", "font-weight", "text-anchor", "dominant-baseline",
             "transform", "opacity", "fill-opacity", "stroke-opacity", "dx", "dy"}
    for node in root.iter():
        tag = node.tag
        if tag.startswith("{http://www.w3.org/2000/svg}"):
            tag = tag.split("}", 1)[1]
        if tag not in tags:
            return ""
        for attr, value in node.attrib.items():
            if attr not in attrs or "url(" in value.lower():
                return ""
            if attr in ("fill", "stroke") and not re.fullmatch(
                r"[a-zA-Z]+|#[0-9a-fA-F]{3,8}|rgba?\([0-9.,%\s]+\)", value
            ):
                return ""
    return svg


def _budgets(cfg: DiagramConfig, cap: int) -> list:
    """本候选的额度档位：首档 + 可选的正文截断重试档，均不超过厂商上限。"""
    tiers = [cfg.max_tokens]
    if cfg.retry_max_tokens > cfg.max_tokens:
        tiers.append(cfg.retry_max_tokens)
    return list(dict.fromkeys(min(b, cap) if cap > 0 else b for b in tiers))


async def generate_diagram(stem: str, providers,
                           cfg: Optional[DiagramConfig] = None) -> DiagramResult:
    """返回可诊断的状态；失败不阻断批改。不解析 reasoning_content 中的半成品。

    按调用链原顺序逐个尝试候选（不再把 glm 提前：2026-10-09 实测 glm-5.3-flash
    同样是思考模型，且同额度下比 deepseek-flash 慢约一倍）。
    """
    cfg = cfg or DiagramConfig()
    stem = (stem or "").strip()
    if not stem:
        return DiagramResult("skipped", reason="empty_stem", message="题干为空，未绘图")
    if not isinstance(providers, (list, tuple)):
        providers = [providers]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.deadline_seconds if cfg.deadline_seconds > 0 else None
    attempts = 0
    last = DiagramResult("failed", reason="no_provider", message="没有可用绘图模型")
    for prov in providers:
        name = getattr(prov, "name", "?")
        cap = int(getattr(getattr(prov, "cfg", None), "max_output_tokens", 0) or 0)
        for budget in _budgets(cfg, cap):
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                log.warning("示意图超过合计时限 %.0fs，停止尝试（已尝试 %d 次）",
                            cfg.deadline_seconds, attempts)
                return DiagramResult("failed", reason="timeout",
                                     message="绘图超时，请稍后重试",
                                     provider=last.provider, attempts=attempts)
            call_timeout = cfg.timeout_seconds or None
            if remaining is not None:
                call_timeout = min(call_timeout, remaining) if call_timeout else remaining
            attempts += 1
            retry = False
            try:
                call = prov.complete_text(DIAGRAM_SYSTEM, f"题目：\n{stem}",
                                          max_tokens=budget, timeout=call_timeout)
                # provider 内部会按 max_retries 重试超时，这里用合计时限兜底，
                # 保证单题绘图不会超过 deadline_seconds。
                outcome = await (asyncio.wait_for(call, remaining)
                                 if remaining is not None else call)
                text = (outcome.text or "").strip()
                thinking = getattr(outcome, "thinking", "") or ""
                finish = getattr(outcome, "finish_reason", "")
                if finish == "length" and text:
                    # 正文确实被截断：放大额度重试有意义（需配置 retry_max_tokens）。
                    # 即使碰巧拿到合法 JSON，也不能把截断内容当完整图放行。
                    reason, message = "truncated", f"绘图输出被截断（额度 {budget} tokens）"
                    retry = True
                elif finish == "length":
                    # 正文 0 字：额度被思考占满。实测 4000/8000 两档思考都把额度用满
                    # （glmf 12181/22831 字、ds 6243/11935 字），说明首档就该给足；
                    # 首档都不够时，再放大到哪一档够没有依据，同一模型再试只会徒增耗时。
                    # 因此不重试，直接换下一个候选。
                    reason = "thinking_exhausted" if thinking else "truncated"
                    message = ("绘图模型把额度都用在了思考上，未产出图形数据" if thinking
                               else f"绘图输出被截断且没有正文（额度 {budget} tokens）")
                elif not text:
                    # 只有思考、没有正式输出：同样不是额度不够，不重试，换下一个候选。
                    reason, message = "empty_output", "绘图模型未返回图形数据"
                else:
                    m = re.search(r"\[.*\]", text, re.S)
                    shapes = json.loads(m.group(0)) if m else None
                    if shapes == []:
                        if not stem_looks_geometric(stem):
                            return DiagramResult("skipped", reason="no_geometry",
                                                 message="题目无需几何示意图", provider=name,
                                                 attempts=attempts)
                        reason, message = "insufficient_geometry", "题干几何信息不足，请补充原图或点位、连线及阴影关系"
                    elif not isinstance(shapes, list) or len(shapes) > 60:
                        reason, message = "invalid_json", "绘图模型未返回有效的图形 JSON 数组"
                    else:
                        svg = sanitize_svg(shapes_to_svg(shapes))
                        if svg:
                            return DiagramResult("generated", svg=svg, provider=name,
                                                 attempts=attempts)
                        reason, message = "invalid_shapes", "绘图数据残缺或 SVG 校验失败"
            except json.JSONDecodeError:
                reason, message = "invalid_json", "绘图模型返回了不完整或非法的 JSON"
            except asyncio.TimeoutError:
                reason, message = "timeout", "绘图超时，请稍后重试"
            except Exception:
                # 不把含网关响应/凭证的异常原文透传到页面。
                log.exception("示意图调用失败 provider=%s", name)
                reason, message = "provider_error", "绘图模型调用失败，请稍后重试"
            last = DiagramResult("failed", reason=reason, message=message,
                                 provider=name, attempts=attempts)
            log.warning("示意图 provider=%s budget=%d reason=%s attempt=%d",
                        name, budget, reason, attempts)
            if not retry:
                break
    return last


async def generate_diagram_svg(stem: str, providers,
                               cfg: Optional[DiagramConfig] = None) -> str:
    """旧调用兼容：失败返回空，不影响台账写入；新调用请使用 generate_diagram。"""
    return (await generate_diagram(stem, providers, cfg)).svg


def lazy_config(cfg: DiagramConfig) -> DiagramConfig:
    """GET 请求里同步懒生成用：合计时限收紧到 LAZY_DEADLINE_SECONDS，避免页面长时间挂起。"""
    deadline = cfg.deadline_seconds
    deadline = min(deadline, LAZY_DEADLINE_SECONDS) if deadline > 0 else LAZY_DEADLINE_SECONDS
    return cfg.model_copy(update={"deadline_seconds": deadline})


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
