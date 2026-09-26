"""应用包初始化。

重要：把第三方库的日志级别压到 WARNING。
`httpx` 默认在 INFO 级打印完整请求 URL，而微信 `jscode2session` 的密钥是**查询参数**，
实测会把 AppSecret 明文写进日志（真实事故）。这里统一降噪，业务自身的日志照常输出。
"""
from __future__ import annotations

import logging

for _noisy in ("httpx", "httpcore", "aiosqlite", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
