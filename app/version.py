"""服务端版本号：当前运行代码的 git 提交短哈希，供界面展示与问题排查。

优先级：SA_GIT_VERSION 环境变量（Docker 构建时 .git 不存在，由构建参数烘入）
→ git rev-parse（直接跑在 git 仓库里，如 macOS 本地开发）→ unknown。
结果缓存，进程内只解析一次。
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def git_version() -> str:
    env = (os.environ.get("SA_GIT_VERSION") or "").strip()
    if env:
        return env[:40]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_APP_ROOT, capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()[:40]
    except Exception:  # noqa: BLE001
        pass
    return "unknown"
