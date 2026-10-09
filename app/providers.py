"""多大模型统一封装。

设计要点：所有国产大模型都提供 OpenAI 兼容接口，
所以只用一套实现类吃掉所有厂商。新增厂商 = 配置文件加 6 行，
不需要写代码。
"""
from __future__ import annotations

import base64
import logging
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel

from . import thinking

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """调用模型失败（网络错误 / 非 200 / 超时等）。"""


class GradeOutcome(BaseModel):
    text: str            # 模型原始输出
    input_tokens: int = 0
    output_tokens: int = 0
    provider: str
    model: str
    thinking: str = ""   # 模型思考过程（reasoning_content），为空表示网关没返回
    # 上游结束原因（"length" 表示被 max_tokens 截断），用于避免把截断当成功
    finish_reason: str = ""


class BaseProvider(ABC):
    """新增非 OpenAI 协议的厂商时，继承这个类实现 grade() 即可。"""

    def __init__(self, name: str, cfg) -> None:
        self.name = name
        self.cfg = cfg

    @abstractmethod
    async def grade(self, image_bytes: bytes, mime: str,
                    system_prompt: str, user_prompt: str) -> GradeOutcome:
        ...

    @abstractmethod
    async def complete_text(self, system_prompt: str, user_prompt: str,
                            max_tokens: int = 4000) -> GradeOutcome:
        """纯文本补全（分阶段批改的求解/比对/诊断阶段用，不传图）。"""

    @abstractmethod
    async def grade_multi(self, images: list, system_prompt: str,
                          user_prompt: str, max_tokens: int = 8000) -> GradeOutcome:
        """多图批改：images 为 [(image_bytes, mime)]，一次调用看全所有图片。"""


class OpenAICompatibleProvider(BaseProvider):
    """走 /chat/completions 的厂商：千问 / GLM / DeepSeek / 豆包 / Kimi…"""

    async def grade(self, image_bytes: bytes, mime: str,
                    system_prompt: str, user_prompt: str) -> GradeOutcome:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}",
                               "detail": "high"}},
            ]},
        ]
        return await self._chat(messages, max_tokens=4000)

    async def complete_text(self, system_prompt: str, user_prompt: str,
                            max_tokens: int = 4000) -> GradeOutcome:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self._chat(messages, max_tokens=max_tokens)

    async def grade_multi(self, images: list, system_prompt: str,
                          user_prompt: str, max_tokens: int = 8000) -> GradeOutcome:
        parts: list = [{"type": "text", "text": user_prompt}]
        for image_bytes, mime in images:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{b64}",
                                        "detail": "high"}})
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": parts},
        ]
        return await self._chat(messages, max_tokens=max_tokens)

    async def _chat(self, messages: list, max_tokens: int) -> GradeOutcome:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.cfg.model,
            "temperature": 0.2,          # 批改要稳定，温度调低
            "max_tokens": max_tokens,
            "messages": messages,
        }
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}

        last_err: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                choice = data["choices"][0] or {}
                message = choice.get("message") or {}
                content = message.get("content") or ""
                usage = data.get("usage", {}) or {}
                finish_reason = str(choice.get("finish_reason") or "")
                reasoning = thinking.extract_reasoning(message)
                if finish_reason == "length":
                    # 输出被 max_tokens 截断：同参数重试没有意义，交由上层换备胎。
                    # 必须记下正文/思考字数：正文为 0 说明思考吃光了输出额度，
                    # 此时加大额度只会更慢（甚至超时），要压思考或换非思考模型。
                    log.warning(
                        "provider=%s 输出被截断（max_tokens=%d，正文 %d 字，思考 %d 字），不再重试",
                        self.name, max_tokens, len(content), len(reasoning))
                return GradeOutcome(
                    text=content,
                    input_tokens=int(usage.get("prompt_tokens", 0)),
                    output_tokens=int(usage.get("completion_tokens", 0)),
                    provider=self.name,
                    model=self.cfg.model,
                    thinking=reasoning,
                    finish_reason=finish_reason,
                )
            except Exception as e:  # noqa: BLE001 - 统一重试
                last_err = e
                # 超时类异常（httpx.ReadTimeout 等）的 str() 是空串，必须带上类型名，
                # 否则日志只剩「第N次调用失败: 」，看不出是超时还是别的失败。
                log.warning("provider=%s 第%d次调用失败（%s）: %s",
                            self.name, attempt, type(e).__name__, e)
        raise ProviderError(
            f"{self.name} 调用失败（{type(last_err).__name__}）: {last_err}")


def make_provider(name: str, cfg) -> BaseProvider:
    ptype = (cfg.type or "openai_compatible").lower()
    if ptype == "openai_compatible":
        return OpenAICompatibleProvider(name, cfg)
    raise ValueError(f"未知 provider 类型: {ptype}（目前只支持 openai_compatible）")
