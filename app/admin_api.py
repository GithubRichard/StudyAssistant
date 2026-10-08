"""管理员 API：删除任意批改任务、查看服务器日志。

鉴权：openid 为 web:<用户名> 且用户名在 auth.admin_users 名单。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from . import auth, db
from .api import Session, get_settings
from .config import Settings

log = logging.getLogger("studyassistant.admin")
router = APIRouter(prefix="/admin", tags=["admin"])

# 允许查看的日志文件（相对于 <data_dir>/logs），防止路径穿越
_ALLOWED_LOGS = ("thinking.log", "grader.log")

_TAIL_CHUNK = 64 * 1024


def _require_admin(ctx: dict, s: Settings) -> None:
    if not auth.is_admin(ctx.get("openid", ""), s.auth.admin_users):
        raise HTTPException(403, "需要管理员权限")


def _log_path(s: Settings, name: str) -> Path:
    if name not in _ALLOWED_LOGS:
        raise HTTPException(400, f"不支持的日志文件：{name}")
    p = Path(s.data_dir) / "logs" / name
    if not p.is_file():
        raise HTTPException(404, f"日志文件不存在：{name}")
    return p


def _tail_lines(path: Path, n: int) -> list:
    """取文件最后 n 行（大文件只读尾部）。"""
    n = max(1, min(n, 2000))
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        pos, found, buf = size, 0, b""
        while pos > 0 and found <= n:
            step = min(_TAIL_CHUNK, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step)
            buf = chunk + buf
            found = buf.count(b"\n")
        lines = buf.split(b"\n")
    text = b"\n".join(lines[-(n + 1):]).decode("utf-8", errors="replace")
    out = text.split("\n")
    if out and not out[-1]:
        out.pop()
    return out[-n:]


@router.get("/tasks")
async def list_tasks_for_admin(limit: int = Query(50, ge=1, le=200),
                               offset: int = Query(0, ge=0),
                               ctx: dict = Session):
    s = get_settings()
    _require_admin(ctx, s)
    rows = await db.list_all_tasks(s.db_path, limit=limit, offset=offset)
    return {"tasks": [
        {"id": r["id"], "subject": r.get("subject", ""), "status": r.get("status", ""),
         "openid": r.get("openid", ""), "created_at": r.get("created_at"),
         "updated_at": r.get("updated_at")}
        for r in rows
    ]}


@router.delete("/tasks/{task_id}")
async def admin_delete_task(task_id: str, ctx: dict = Session):
    """管理员强制删除任务：不限制状态，连带清理台账/事件/附件。"""
    s = get_settings()
    _require_admin(ctx, s)
    task = await db.get_task(s.db_path, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    orphan_files = await db.delete_task(s.db_path, task_id)
    for p in orphan_files:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError as e:
            log.warning("删除孤儿附件失败 %s：%s", p, e)
    log.info("管理员 %s 删除任务 %s（状态 %s）", ctx.get("openid"), task_id,
             task.get("status"))
    return {"deleted": task_id}


@router.get("/logs")
async def list_logs(ctx: dict = Session):
    s = get_settings()
    _require_admin(ctx, s)
    out = []
    for name in _ALLOWED_LOGS:
        p = Path(s.data_dir) / "logs" / name
        out.append({"name": name, "exists": p.is_file(),
                    "size": p.stat().st_size if p.is_file() else 0,
                    "mtime": p.stat().st_mtime if p.is_file() else 0})
    return {"logs": out}


@router.get("/logs/tail")
async def tail_log(file: str = Query("thinking.log"),
                   lines: int = Query(200, ge=1, le=2000),
                   ctx: dict = Session):
    s = get_settings()
    _require_admin(ctx, s)
    path = _log_path(s, file)
    return {"file": file, "lines": _tail_lines(path, lines)}


@router.get("/logs/stream")
async def stream_log(file: str = Query("thinking.log"),
                     token: str = Query("", description="EventSource 无法带 header，用 query 传 token"),
                     authorization: str = Header(None)):
    """SSE 实时推送日志新增行。"""
    s = get_settings()
    # EventSource 不支持自定义 header，允许 query 传 token
    raw = token or auth.extract_bearer(authorization)
    try:
        ctx = await auth.authenticate(s.db_path, raw)
    except auth.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    _require_admin(ctx, s)
    path = _log_path(s, file)

    async def gen() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        text = line.decode("utf-8", errors="replace").rstrip("\n")
                        yield f"data: {text}\n\n"
                    else:
                        await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            yield f"event: error\ndata: {e}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
