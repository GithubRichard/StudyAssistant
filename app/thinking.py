"""临时调试：记录模型的思考过程（reasoning_content）。

开启方式：环境变量 ``SA_DEBUG_THINKING=1``（docker-compose 已透传，
在宿主机 `.env` 里加一行即可，改完重建容器）。

开启后，凡是返回了思考过程的模型调用（分阶段各阶段 / 整体批改 /
Hermes 主流程 / 复查模型），其思考内容会写入
``<data_dir>/logs/thinking.log``（当天），按天轮转，
历史文件 ``thinking.log.YYYY-MM-DD``，最多保留 5 天（含今天）；
不再进 docker 日志。查看：

    tail -f data/logs/thinking.log

默认关闭。分析完把环境变量删掉或设为 0 并重建容器即可关闭，
不产生任何持久化副作用（不进数据库、不进批改结果）。
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

log = logging.getLogger("studyassistant.thinking")

_TRUE_VALUES = {"1", "true", "yes", "on"}

#: 保留天数（含今天）：thinking.log + 4 个历史文件
RETAIN_DAYS = 5
_context = ContextVar("thinking_task_context", default="")


@contextmanager
def task_context(task_id: str, run_no: int):
    token = _context.set(f"session=study-{task_id}-{run_no} task_id={task_id} run_no={run_no}")
    try:
        yield
    finally:
        _context.reset(token)


def log_event(stage: str, data: Any) -> None:
    if is_enabled():
        log.info("【任务阶段】%s stage=%s\n%s\n【任务阶段结束】", _context.get(), stage,
                 json.dumps(data, ensure_ascii=False))


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
    log.info("【模型思考过程】%s %s\n%s\n【思考过程结束】", _context.get(), context, text)


def setup_file_logging(data_dir: str) -> str:
    """给 studyassistant.thinking 配置按天轮转的日志文件。

    路径：``<data_dir>/logs/thinking.log``（当天）；轮转后
    ``thinking.log.YYYY-MM-DD``，最多保留 5 天（含今天）。
    思考过程动辄上万 token，只写文件，不再进 docker 日志。

    返回当天日志文件路径；初始化失败返回空字符串（此时保持
    输出到 docker 日志的旧行为）。
    """
    for h in log.handlers:
        if isinstance(h, TimedRotatingFileHandler):
            return str(Path(data_dir) / "logs" / "thinking.log")
    log_dir = Path(data_dir) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(
            str(log_dir / "thinking.log"),
            when="midnight", interval=1,
            backupCount=RETAIN_DAYS - 1,
            encoding="utf-8", delay=True,
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        # 只写文件：不再进 docker 日志
        log.propagate = False
        return str(log_dir / "thinking.log")
    except OSError as e:
        logging.getLogger(__name__).warning(
            "思考过程日志文件初始化失败，保持输出到 docker 日志: %s", e)
        return ""
