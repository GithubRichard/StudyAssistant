"""数学错题示意图：AI 按题干重绘 SVG 矢量图。

背景：数学几何题的题干常引用"图1""图2"，纯文本错题不利于阅读。
方案：模型按题干描述重绘示意图（SVG 矢量，清晰可缩放），存入台账，
错题详情页渲染。AI 重绘仅供参考，不作为判分依据。
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

log = logging.getLogger("studyassistant.diagram")

DIAGRAM_SYSTEM = """你是几何示意图绘制员。根据题目描述，用 SVG 绘制示意图。
要求：
1. 只画题目明确描述的几何图形（点、线、形），标注字母与关键尺寸/数值。
   线条黑色，重要区域可浅灰填充，标注用 12px 无衬线字体。
2. 画布 400x300，图形居中，四周留白；线条粗细 2，字不压线。
3. 不确定的部分不要画、不要猜；纯代数题（无图形描述）输出空字符串。
4. 只输出 <svg>...</svg> 代码，不要任何其他文字，不要 markdown 代码块。
"""

_MAX_SVG_BYTES = 100 * 1024


def sanitize_svg(raw: str) -> str:
    """清洗并校验模型输出的 SVG。合法返回 SVG 字符串，否则返回 ""。

    - 提取 <svg>...</svg> 片段
    - XML 合法、根节点为 svg
    - 禁止 script、外部引用（http/https/data: 以外的 href）、事件属性
    - 大小上限 100KB
    """
    if not raw:
        return ""
    m = re.search(r"<svg\b.*?</svg>", raw, re.S | re.I)
    if not m:
        return ""
    svg = m.group(0).strip()
    if len(svg.encode("utf-8")) > _MAX_SVG_BYTES:
        log.warning("示意图过大，已丢弃（%d bytes）", len(svg))
        return ""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        log.warning("示意图 XML 非法，已丢弃：%s", e)
        return ""
    tag = root.tag.lower()
    if not tag.endswith("svg"):
        log.warning("示意图根节点不是 svg，已丢弃")
        return ""
    lowered = svg.lower()
    if "<script" in lowered or "javascript:" in lowered:
        log.warning("示意图含脚本，已丢弃")
        return ""
    # 事件属性 onload=/onclick= 等
    if re.search(r"\son\w+\s*=", lowered):
        log.warning("示意图含事件属性，已丢弃")
        return ""
    return svg


async def generate_diagram_svg(stem: str, provider) -> str:
    """按题干生成示意图 SVG。失败返回 ""（fail-open，不阻断台账写入）。"""
    stem = (stem or "").strip()
    if not stem:
        return ""
    try:
        outcome = await provider.complete_text(
            DIAGRAM_SYSTEM,
            f"题目：\n{stem}",
            max_tokens=2000,
        )
        text = (outcome.text or "").strip()
        if not text:
            return ""
        return sanitize_svg(text)
    except Exception as e:
        log.warning("示意图生成失败，已跳过：%s", e)
        return ""


def is_math_subject(subject: str) -> bool:
    return "数学" in (subject or "")
