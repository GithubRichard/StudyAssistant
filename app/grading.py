"""批改 Agent：按 provider 链调用模型，输出 JSON 强校验，失败自动换备胎。"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from . import providers, thinking
from .config import Settings, provider_chain
from .providers import ProviderError

log = logging.getLogger(__name__)


class QuestionResult(BaseModel):
    no: str
    student_answer: str = ""
    is_correct: bool
    correct_answer: str = ""
    explanation: list[str] = Field(default_factory=list)
    knowledge_point: str = ""


class GradingResult(BaseModel):
    total_questions: int = 0
    correct_count: int = 0
    questions: list[QuestionResult] = Field(default_factory=list)
    summary: str = ""


_JSON_DECODER = json.JSONDecoder()
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def first_json_object(text: str) -> Optional[dict]:
    """在文本里找第一个能完整解析的 JSON 对象（允许后面还有多余内容）。

    真实事故：模型在结果对象后面又输出了一段 JSON（或说明），
    旧的「第一个 { 到最后一个 }」整体解析会报 `Extra data`，整阶段被判失败并切备胎。
    这里改为逐位置 raw_decode：解析到第一个完整对象就返回，后面是说明文字、
    还是多出来的对象都不影响；前面的花括号（如示例占位 `{}`）解析不了会自动跳过。
    """
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            value, _ = _JSON_DECODER.raw_decode(text, idx)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON（兼容 ```json 包裹、裸 JSON、JSON 后跟多余内容）。"""
    body = text or ""
    candidates = [m.group(1) for m in _FENCE_RE.finditer(body)]
    candidates.append(body)
    for cand in candidates:
        value = first_json_object(cand)
        if value is not None:
            return value
    raise ValueError("模型输出中没有找到 JSON")


async def grade_image(image_bytes: bytes, mime: str, subject: str,
                      grade_level: str, settings: Settings
                      ) -> tuple[GradingResult, str, str, int, int, float]:
    """批改一张作业图。

    返回：(结构化结果, provider名, 模型名, 输入tokens, 输出tokens, 花费元)
    """
    chain = provider_chain(settings)
    if not chain:
        raise ProviderError("没有可用的模型 provider：请检查 config.yaml 的 api_key 与 enabled")

    user_prompt = settings.user_prompt_template.format(
        subject=subject, grade_level=grade_level)
    errors: list[str] = []

    for name in chain:
        cfg = settings.llm.providers[name]
        provider = providers.make_provider(name, cfg)
        try:
            outcome = await provider.grade(
                image_bytes, mime, settings.system_prompt, user_prompt)
        except ProviderError as e:
            errors.append(f"{name}: {e}")
            log.warning("切换备胎模型（调用失败）: %s", e)
            continue

        try:
            result = GradingResult.model_validate(extract_json(outcome.text))
        except (ValueError, ValidationError, json.JSONDecodeError) as e:
            errors.append(f"{name}: 输出格式校验失败({e})")
            log.warning("切换备胎模型（格式非法）: %s", e)
            continue

        cost = (outcome.input_tokens / 1_000_000) * cfg.price_input_per_1m \
             + (outcome.output_tokens / 1_000_000) * cfg.price_output_per_1m
        cost = round(cost, 4)
        log.info("批改成功 provider=%s tokens=%d/%d cost≈%.4f元",
                 name, outcome.input_tokens, outcome.output_tokens, cost)
        thinking.log_thinking(
            f"provider={name} model={outcome.model}（整体批改）", outcome.thinking)
        return result, name, outcome.model, outcome.input_tokens, outcome.output_tokens, cost

    raise ProviderError("所有模型都失败了: " + " | ".join(errors))
