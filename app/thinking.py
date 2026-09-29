"""临时调试：记录模型的思考过程（reasoning_content）。

开启方式：环境变量 ``SA_DEBUG_THINKING=1``（docker-compose 已透传，
在宿主机 `.env` 里加一行即可，改完重建容器）。

开启后，凡是返回了思考过程的模型调用（分阶段各阶段 / 整体批改 /
Hermes 主流程 / 复查模型），其思考内容会打到 ``studyassistant.thinking``
日志（即 docker 日志），首尾有明确分隔标记，方便 grep 分析：

    docker logs <容器名> | grep -A 200 模型思考过程

默认关闭。分析完把环境变量删掉或设为 0 并重建容器即可关闭，
不产生任何持久化副作用（不进数据库、不进批改结果）。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("studyassistant.thinking")

_TRUE_VALUES = {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """每次调用都读环境变量，改 .env 重建容器即生效。"""
    return os.environ.get("SA_DEBUG_THINKING", "").strip().lower() in _TRUE_VALUES


def extract_reasoning(message: Any) -> str:
    """从 OpenAI 兼容响应的 message 里提取思考过程。

    不同网关/模型的字段名不统一，常见形态：
    - ``message["reasoning_content"]``：字符串（DeepSeek 系最常见）
    - ``message["reasoning"]``：字符串或 {"text": ...} 或分段列表
    拿不到返回空字符串。
    """
    if not isinstance(message, dict):
        return ""
    raw = message.get("reasoning_content", message.get("reasoning"))
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for key in ("text", "content"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        try:
            return json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(raw)
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                val = item.get("text", item.get("content"))
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())
        return "\n".join(parts)
    return str(raw).strip()


def log_thinking(context: str, reasoning: str) -> None:
    """记录一次模型思考过程。未开启开关或内容为空时直接返回。"""
    if not is_enabled():
        return
    text = (reasoning or "").strip()
    if not text:
        return
    log.info("【模型思考过程】%s\n%s\n【思考过程结束】", context, text)
