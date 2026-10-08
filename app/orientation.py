"""页面方向：OSD → 一次视觉补判 → 持久化等待人工确认。角度均为顺时针。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool

from . import image_prep, providers, thinking
from .hermes import extract_result_json

log = logging.getLogger(__name__)
SYSTEM = """你是页面方向检查员。必须直接读取附图，只判断文字阅读方向，不解题、不猜答案。
返回一个 JSON 对象：{"rotation":0,"certain":true,"readable":true,"cue":"页面标题或印刷文字证据"}。
rotation 是对附图施加的顺时针旋转角度，只能为 0、90、180、270。
转动后印刷文字应从左到右、从上到下阅读。公式、图形、纸张横竖不能单独作为方向证据。
字迹少、模糊、只有图片摘要或不能确认时 certain=false；无法读出题目时 readable=false。
不要把低置信度描述成已确认。"""


class Direction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rotation: Literal[0, 90, 180, 270]
    certain: StrictBool
    readable: StrictBool
    cue: str


class ConfirmationRequired(Exception):
    """材料保留，等待方向确认；没有执行转写和解题。"""


async def prepare_pages(images, settings, chain, saved=None, on_update=None,
                        provider_factory=None):
    saved = saved or {}
    previous = {p["page"]: p for p in saved.get("pages", [])}
    pages, prepared = [], []
    cfg = settings.staged_grading
    names = [cfg.orientation_provider] if cfg.orientation_provider else chain
    vision = next((n for n in names if n in settings.llm.providers
                   and settings.llm.providers[n].enabled
                   and settings.llm.providers[n].supports_vision), None)

    async def persist():
        if on_update:
            await on_update({"pages": pages})

    for number, (raw, mime) in enumerate(images, 1):
        digest = hashlib.sha256(raw).hexdigest()
        old = previous.get(number, {})
        if old.get("sha256") != digest:
            old = {}
        confirmed = old.get("rotation") if old.get("confirmed") else None
        pb, pm, info = await asyncio.to_thread(
            image_prep.prepare_extract_image, raw, mime,
            cfg.extract_image_min_long_side, cfg.extract_image_max_long_side,
            confirmed_rotation=confirmed)
        page = dict(old) if old else {
            "page": number, "sha256": digest, "rotation": info["text_rotation_degrees"],
            "confirmed": not info["orientation_check_required"],
            "source": "osd", "osd_confidence": info["orientation_confidence"],
            "osd_error": info["orientation_error"], "visual_attempted": False,
            "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
        }
        pages.append(page)
        if not page["confirmed"] and not page.get("visual_attempted") and vision and cfg.orientation_visual_fallback:
            # Persist before dispatch: timeout/restart/resume must not cause a second call.
            page["visual_attempted"] = True
            page["source"] = "visual"
            await persist()
            provider_cfg = settings.llm.providers[vision].model_copy(
                update={"max_retries": 1, "timeout": min(settings.llm.providers[vision].timeout, 25)})
            try:
                provider = (provider_factory or providers.make_provider)(vision, provider_cfg)
                thumb, thumb_mime, _ = await asyncio.to_thread(
                    image_prep.prepare_extract_image, raw, mime, 0, 1600,
                    confirmed_rotation=0)
                outcome = await provider.grade_multi(
                    [(thumb, thumb_mime)], SYSTEM,
                    "请直接看图，判断页面需要顺时针旋转多少度才能正常阅读。",
                    max_tokens=min(provider_cfg.max_output_tokens or 1500, 1500))
                page.update(provider=vision, model=outcome.model,
                            input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
                            cost=round((outcome.input_tokens * provider_cfg.price_input_per_1m
                                        + outcome.output_tokens * provider_cfg.price_output_per_1m) / 1_000_000, 6))
                thinking.log_thinking(f"stage=orientation page={number} sha256={digest} provider={vision}", outcome.thinking)
                decision = Direction.model_validate(extract_result_json(outcome.text))
                if outcome.finish_reason != "length" and decision.certain and decision.readable and decision.cue.strip():
                    # 视觉补判不是终审：把模型给的角度实际转一次，再跑 OSD 复核。
                    # OSD 高置信度说仍不正 → 不采信，回 waiting_input 等人工；
                    # OSD 无法判断 → 接受视觉结论（OSD 本来就不确定）。
                    recheck = await asyncio.to_thread(
                        image_prep.check_rotation, raw, decision.rotation)
                    if recheck == "wrong":
                        page["visual_error"] = "rotation_recheck_failed"
                        log.warning("视觉判向被 OSD 复核否决 page=%s rotation=%s",
                                    number, decision.rotation)
                    else:
                        page.update(rotation=decision.rotation, confirmed=True, cue=decision.cue)
                        pb, pm, info = await asyncio.to_thread(
                            image_prep.prepare_extract_image, raw, mime,
                            cfg.extract_image_min_long_side, cfg.extract_image_max_long_side,
                            confirmed_rotation=decision.rotation)
            except Exception as exc:
                # No retry or provider cascade for direction alone.
                page["visual_error"] = type(exc).__name__
                log.warning("视觉判向未确认 page=%s error_type=%s", number, type(exc).__name__)
        prepared.append((pb, pm))
        page.update(width=info["width"], height=info["height"],
                    exif_orientation=info["exif_orientation"],
                    prepared_sha256=hashlib.sha256(pb).hexdigest())
        await persist()
        thinking.log_event("orientation", page)
    if any(not p["confirmed"] for p in pages):
        raise ConfirmationRequired("请查看待确认页面，旋转到文字朝上后确认；图片已保留，确认后继续批改。")
    return prepared, {"pages": pages}
