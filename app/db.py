"""SQLite 数据层（零配置）。

约定：
- 所有写操作只通过本模块提供的函数，字段名走白名单，避免动态 SQL 注入。
- 配额检查与预留、任务创建、幂等记录在同一事务内完成。
- 执行任务由数据库认领（claim + 租约），不依赖进程内 BackgroundTasks 的存活。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import aiosqlite

from . import migrations

log = logging.getLogger(__name__)

# 兼容旧引用：历史代码可能 `from .db import SCHEMA`
SCHEMA = migrations.V1_DDL

TASK_STATUSES = ("pending", "grading", "waiting_input", "interrupted", "done", "failed")
RUN_STATUSES = ("queued", "running", "done", "failed", "interrupted", "waiting_input")

_TASK_FIELDS = {
    "subject", "grade_level", "task_type", "input_text", "status", "result_json",
    "provider", "model", "input_tokens", "output_tokens", "cost_cny", "error",
    "run_count", "claim_owner", "claim_expires_at", "archive_path", "updated_at",
    "exam_scope", "training_kind", "scope_start", "scope_end", "git_status",
}
_RUN_FIELDS = {
    "status", "started_at", "finished_at", "error", "result_json",
    "hermes_session_id", "input_text",
}


async def init_db(path: str) -> dict:
    """建库并执行迁移，返回迁移结果（含备份路径，便于启动日志如实记录）。"""
    result = await migrations.run_migrations(path)
    if result.get("applied"):
        log.info("数据库迁移完成: %s 备份=%s", result["applied"], result.get("backup"))
    return result


def _today() -> str:
    return date.today().isoformat()


# ---------- 用户与配额 ----------

async def get_or_create_user(db_path: str, openid: str, bonus: int) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE openid=?", (openid,)) as cur:
            row = await cur.fetchone()
        if row:
            return dict(row)
        now = time.time()
        await db.execute(
            "INSERT INTO users(openid, bonus_quota, created_at) VALUES(?,?,?)",
            (openid, bonus, now),
        )
        await db.commit()
        return {"openid": openid, "bonus_quota": bonus, "created_at": now}


async def quota_remaining(db_path: str, openid: str, daily_free: int) -> int:
    """剩余次数 = 赠送次数 + 每日免费 - 今日已用。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT bonus_quota FROM users WHERE openid=?", (openid,)) as cur:
            row = await cur.fetchone()
        bonus = row[0] if row else 0
        async with db.execute(
            "SELECT used FROM quota_usage WHERE openid=? AND day=?", (openid, _today())
        ) as cur:
            row = await cur.fetchone()
        used = row[0] if row else 0
        return max(0, bonus + daily_free - used)


async def consume_quota(db_path: str, openid: str) -> bool:
    """兼容旧调用：直接扣 1 次。新流程请用 reserve_quota。"""
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            ok = await _consume_locked(db, openid)
            await db.execute("COMMIT")
            return ok
        except Exception:  # noqa: BLE001
            await db.execute("ROLLBACK")
            raise


async def _consume_locked(db: aiosqlite.Connection, openid: str) -> Optional[str]:
    """在已开启的事务中扣 1 次。

    口径：`quota_usage.used` 统计当日实际发起的任务数（含赠送额度），
    剩余次数 = 赠送额度 + 每日免费 - 当日已用，因此赠送额度不会被重复扣两次。
    用户不存在返回 None。
    """
    async with db.execute("SELECT bonus_quota FROM users WHERE openid=?", (openid,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    await db.execute(
        """INSERT INTO quota_usage(openid, day, used) VALUES(?,?,1)
           ON CONFLICT(openid, day) DO UPDATE SET used=used+1""",
        (openid, _today()),
    )
    return "daily"


async def reserve_quota(db_path: str, openid: str, task_id: str, daily_free: int,
                        max_per_day: int) -> Dict[str, Any]:
    """创建任务时预留配额，与任务创建在同一事务中调用。"""
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if not await _user_exists(db, openid):
                await db.execute("ROLLBACK")
                return {"ok": False, "reason": "用户不存在，请重新登录"}
            async with db.execute(
                "SELECT used FROM quota_usage WHERE openid=? AND day=?", (openid, _today())
            ) as cur:
                row = await cur.fetchone()
            used_today = row[0] if row else 0
            if max_per_day and used_today >= max_per_day:
                await db.execute("ROLLBACK")
                return {"ok": False, "reason": f"今日已达上限 {max_per_day} 次"}
            remaining = await _remaining_locked(db, openid, daily_free)
            if remaining <= 0:
                await db.execute("ROLLBACK")
                return {"ok": False, "reason": "今日次数已用完，请明天再试"}
            source = await _consume_locked(db, openid)
            now = time.time()
            await db.execute(
                """INSERT INTO quota_reservations(id, openid, task_id, source, state, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex[:16], openid, task_id, source, "reserved", now, now),
            )
            await db.execute("COMMIT")
            return {"ok": True, "source": source}
        except Exception as e:  # noqa: BLE001
            await db.execute("ROLLBACK")
            raise e


async def settle_reservation(db_path: str, task_id: str) -> None:
    """任务执行完成：预留转为已结算，不再重复扣次。"""
    await _close_reservation(db_path, task_id, "settled", refund=False)


async def release_reservation(db_path: str, task_id: str, refund: bool = False) -> None:
    """确定未执行时才退款；执行结果未知时不要调用（避免与真实扣费不一致）。"""
    await _close_reservation(db_path, task_id, "released", refund=refund)


async def _close_reservation(db_path: str, task_id: str, state: str, refund: bool) -> None:
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT id, openid, source, state FROM quota_reservations WHERE task_id=? ORDER BY created_at DESC",
                (task_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row or row[3] != "reserved":
                await db.execute("COMMIT")
                return
            if refund:
                await db.execute(
                    """UPDATE quota_usage SET used=MAX(0, used-1)
                       WHERE openid=? AND day=?""", (row[1], _today()))
            await db.execute(
                "UPDATE quota_reservations SET state=?, updated_at=? WHERE id=?",
                (state, time.time(), row[0]),
            )
            await db.execute("COMMIT")
        except Exception:  # noqa: BLE001
            await db.execute("ROLLBACK")
            raise


async def _user_exists(db: aiosqlite.Connection, openid: str) -> bool:
    async with db.execute("SELECT 1 FROM users WHERE openid=?", (openid,)) as cur:
        return await cur.fetchone() is not None


async def _remaining_locked(db: aiosqlite.Connection, openid: str, daily_free: int) -> int:
    async with db.execute("SELECT bonus_quota FROM users WHERE openid=?", (openid,)) as cur:
        row = await cur.fetchone()
    bonus = row[0] if row else 0
    async with db.execute(
        "SELECT used FROM quota_usage WHERE openid=? AND day=?", (openid, _today())
    ) as cur:
        row2 = await cur.fetchone()
    used = row2[0] if row2 else 0
    return max(0, bonus + daily_free - used)


# ---------- 会话 ----------

async def create_session(db_path: str, session: Dict[str, Any]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO sessions(session_id, openid, token_hash, created_at, expires_at, last_seen_at)
               VALUES(?,?,?,?,?,?)""",
            (session["session_id"], session["openid"], session["token_hash"],
             session["created_at"], session["expires_at"], session["last_seen_at"]),
        )
        await db.commit()


async def get_session_by_token(db_path: str, token_hash: str) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def touch_session(db_path: str, session_id: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?",
                         (time.time(), session_id))
        await db.commit()


async def delete_session(db_path: str, session_id: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        await db.commit()


async def purge_expired_sessions(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
        await db.commit()
        return cur.rowcount or 0


# ---------- 附件 ----------

async def create_asset(db_path: str, asset: Dict[str, Any]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO assets(id, openid, sha256, mime, bytes, width, height, path, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (asset["id"], asset["openid"], asset["sha256"], asset["mime"], asset["bytes"],
             asset.get("width", 0), asset.get("height", 0), asset["path"], asset["created_at"]),
        )
        await db.commit()


async def get_asset(db_path: str, asset_id: str) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def list_assets(db_path: str, asset_ids: Iterable[str]) -> List[dict]:
    ids = list(dict.fromkeys(asset_ids))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM assets WHERE id IN ({placeholders})", ids
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def link_task_assets(db_path: str, task_id: str, run_id: str,
                           asset_ids: Iterable[str]) -> None:
    now = time.time()
    async with aiosqlite.connect(db_path) as db:
        for position, asset_id in enumerate(asset_ids):
            await db.execute(
                """INSERT OR IGNORE INTO task_assets(task_id, run_id, asset_id, position, created_at)
                   VALUES(?,?,?,?,?)""",
                (task_id, run_id, asset_id, position, now),
            )
        await db.commit()


async def list_task_assets(db_path: str, task_id: str, run_id: str = "") -> List[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if run_id:
            sql = """SELECT a.* FROM task_assets t JOIN assets a ON a.id=t.asset_id
                     WHERE t.task_id=? AND t.run_id=? ORDER BY t.position"""
            args = (task_id, run_id)
        else:
            sql = """SELECT a.* FROM task_assets t JOIN assets a ON a.id=t.asset_id
                     WHERE t.task_id=? ORDER BY t.position"""
            args = (task_id,)
        async with db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ---------- 任务 ----------

async def create_task_atomic(db_path: str, task: Dict[str, Any], *,
                             daily_free: int = 0, max_per_day: int = 0,
                             idempotency: Optional[Dict[str, Any]] = None,
                             reserve: bool = True) -> Dict[str, Any]:
    """同一事务内完成：幂等查重 → 配额预留 → 任务写入 → 幂等记录。

    返回 {ok, task_id, duplicate, reason}；duplicate=True 表示命中幂等键。
    """
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if idempotency:
                async with db.execute(
                    "SELECT task_id, request_hash FROM idempotency WHERE key=?",
                    (idempotency["key"],),
                ) as cur:
                    row = await cur.fetchone()
                if row:
                    await db.execute("COMMIT")
                    if row[1] != idempotency["request_hash"]:
                        return {"ok": False, "duplicate": False, "conflict": True,
                                "reason": "同一幂等键对应不同请求内容"}
                    return {"ok": True, "duplicate": True, "task_id": row[0]}

            source = None
            if reserve:
                if not await _user_exists(db, task["openid"]):
                    await db.execute("ROLLBACK")
                    return {"ok": False, "reason": "用户不存在，请重新登录"}
                if max_per_day:
                    async with db.execute(
                        "SELECT used FROM quota_usage WHERE openid=? AND day=?",
                        (task["openid"], _today()),
                    ) as cur:
                        used_row = await cur.fetchone()
                    if used_row and used_row[0] >= max_per_day:
                        await db.execute("ROLLBACK")
                        return {"ok": False, "reason": f"今日已达上限 {max_per_day} 次"}
                remaining = await _remaining_locked(db, task["openid"], daily_free)
                if remaining <= 0:
                    await db.execute("ROLLBACK")
                    return {"ok": False, "reason": "今日次数已用完，请明天再试"}
                source = await _consume_locked(db, task["openid"])

            await db.execute(
                """INSERT INTO tasks(id, openid, subject, grade_level, image_path, status,
                                     task_type, input_text, idempotency_key, exam_scope,
                                     training_kind, scope_start, scope_end, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task["id"], task["openid"], task["subject"], task["grade_level"],
                 task.get("image_path", ""), task.get("status", "pending"),
                 task.get("task_type", "grading"), task.get("input_text", ""),
                 (idempotency or {}).get("key", ""), task.get("exam_scope", ""),
                 task.get("training_kind", ""), task.get("scope_start", ""),
                 task.get("scope_end", ""), task["created_at"], task["created_at"]),
            )
            if reserve:
                now = time.time()
                await db.execute(
                    """INSERT INTO quota_reservations(id, openid, task_id, source, state, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex[:16], task["openid"], task["id"], source or "daily",
                     "reserved", now, now),
                )
            if idempotency:
                await db.execute(
                    """INSERT INTO idempotency(key, openid, endpoint, request_hash, task_id, created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (idempotency["key"], idempotency["openid"], idempotency["endpoint"],
                     idempotency["request_hash"], task["id"], time.time()),
                )
            await db.execute("COMMIT")
            return {"ok": True, "duplicate": False, "task_id": task["id"]}
        except Exception:  # noqa: BLE001
            await db.execute("ROLLBACK")
            raise


async def get_task(db_path: str, task_id: str) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def update_task(db_path: str, task_id: str, **fields) -> None:
    unknown = set(fields) - _TASK_FIELDS
    if unknown:
        raise ValueError(f"不允许更新的字段: {sorted(unknown)}")
    fields["updated_at"] = time.time()
    keys = ", ".join(f"{k}=?" for k in fields)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"UPDATE tasks SET {keys} WHERE id=?", (*fields.values(), task_id))
        await db.commit()


async def list_tasks(db_path: str, openid: str, limit: int = 20,
                     offset: int = 0) -> List[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM tasks WHERE openid=?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (openid, limit, max(0, offset)),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def claim_next_task(db_path: str, owner: str, lease_seconds: int = 300) -> Optional[dict]:
    """认领一个待执行任务；只认领 pending，避免重复派发有副作用的调用。"""
    now = time.time()
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                """SELECT * FROM tasks WHERE status='pending'
                   ORDER BY created_at LIMIT 1"""
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await db.execute("COMMIT")
                return None
            task = dict(row)
            await db.execute(
                """UPDATE tasks SET status='grading', claim_owner=?, claim_expires_at=?, updated_at=?
                   WHERE id=? AND status='pending'""",
                (owner, now + lease_seconds, now, task["id"]),
            )
            await db.execute("COMMIT")
            return task
        except Exception:  # noqa: BLE001
            await db.execute("ROLLBACK")
            raise


async def recover_interrupted(db_path: str) -> List[str]:
    """启动恢复：已派发但未结束的任务标记为 interrupted，不自动重放。

    返回被标记的任务号，调用方需在日志或状态中原样上报，避免谎称仍在执行。
    """
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        now = time.time()
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT id FROM tasks WHERE status='grading'"
            ) as cur:
                ids = [r[0] for r in await cur.fetchall()]
            if ids:
                await db.execute(
                    """UPDATE tasks SET status='interrupted', claim_owner='', claim_expires_at=0,
                       error='服务重启，执行结果未确认，需要重新发起或补充材料', updated_at=?
                       WHERE status='grading'""",
                    (now,),
                )
                await db.execute(
                    """UPDATE task_runs SET status='interrupted', finished_at=?,
                       error='服务重启，执行结果未确认' WHERE status IN ('queued','running')""",
                    (now,),
                )
            await db.execute("COMMIT")
            return ids
        except Exception:  # noqa: BLE001
            await db.execute("ROLLBACK")
            raise


# ---------- 执行轮次 ----------

async def create_run(db_path: str, run: Dict[str, Any]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO task_runs(id, task_id, run_no, kind, input_text, status,
                                     hermes_session_id, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (run["id"], run["task_id"], run["run_no"], run["kind"],
             run.get("input_text", ""), run.get("status", "queued"),
             run.get("hermes_session_id", ""), run["created_at"]),
        )
        await db.commit()


async def update_run(db_path: str, run_id: str, **fields) -> None:
    unknown = set(fields) - _RUN_FIELDS
    if unknown:
        raise ValueError(f"不允许更新的执行轮次字段: {sorted(unknown)}")
    keys = ", ".join(f"{k}=?" for k in fields)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"UPDATE task_runs SET {keys} WHERE id=?", (*fields.values(), run_id))
        await db.commit()


async def get_run(db_path: str, run_id: str) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def list_runs(db_path: str, task_id: str) -> List[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM task_runs WHERE task_id=? ORDER BY run_no", (task_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def has_unconfirmed_run(db_path: str, task_id: str) -> bool:
    """是否存在执行结果未确认的轮次：存在时不允许同任务继续派发。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT 1 FROM task_runs
               WHERE task_id=? AND status IN ('queued','running','interrupted') LIMIT 1""",
            (task_id,),
        ) as cur:
            return await cur.fetchone() is not None


# ---------- 幂等 ----------

async def get_idempotency(db_path: str, key: str) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM idempotency WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


# ---------- 成果文件 ----------

async def add_artifact(db_path: str, artifact: Dict[str, Any]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO artifacts(id, task_id, run_id, kind, path, bytes, sha256, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (artifact["id"], artifact["task_id"], artifact.get("run_id", ""),
             artifact["kind"], artifact["path"], artifact.get("bytes", 0),
             artifact.get("sha256", ""), artifact["created_at"]),
        )
        await db.commit()


async def list_artifacts(db_path: str, task_id: str) -> List[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_artifact(db_path: str, task_id: str, artifact_id: str) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM artifacts WHERE task_id=? AND id=?", (task_id, artifact_id)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


# ---------- 费用 ----------

async def add_daily_cost(db_path: str, cost: float) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO daily_cost(day, cost) VALUES(?,?)
               ON CONFLICT(day) DO UPDATE SET cost=cost+excluded.cost""",
            (_today(), cost),
        )
        await db.commit()


async def get_daily_cost(db_path: str) -> float:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT cost FROM daily_cost WHERE day=?", (_today(),)) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row else 0.0


# ---------- 错题本 ----------

async def save_mistake(db_path: str, openid: str, task_id: str,
                       question_no: str, knowledge_point: str, note: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """INSERT INTO mistakes(openid, task_id, question_no, knowledge_point, note, created_at)
               VALUES(?,?,?,?,?,?)""",
            (openid, task_id, question_no, knowledge_point, note, time.time()),
        )
        await db.commit()
        return cur.lastrowid


async def list_mistakes(db_path: str, openid: str, limit: int = 100,
                        offset: int = 0) -> List[dict]:
    """人工收藏的错题（不含自动台账条目：台账条目的 question_uid 非空）。"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM mistakes WHERE openid=? AND question_uid=''
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (openid, limit, max(0, offset)),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_mistake(db_path: str, openid: str, mistake_id: int) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM mistakes WHERE id=? AND openid=?", (mistake_id, openid)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


def db_path_for(data_dir: str) -> str:
    return str(Path(data_dir) / "app.db")


# ---------- 家庭设置 ----------

async def get_family_settings(db_path: str, openid: str) -> Optional[dict]:
    """读取家庭设置；未保存过返回 None（由调用方回落到配置文件默认值）。"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM family_settings WHERE openid=?", (openid,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    data = dict(row)
    data["subjects"] = _load_subjects(data.get("subjects", ""))
    return data


def _load_subjects(raw: str) -> List[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(s) for s in parsed] if isinstance(parsed, list) else []


async def save_family_settings(db_path: str, openid: str, *, grade_level: str,
                               subjects: List[str], term_start_date: str) -> dict:
    """保存家庭设置（学科清单以 JSON 文本存储，便于扩展新学科）。"""
    now = time.time()
    payload = json.dumps(list(subjects), ensure_ascii=False)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO family_settings(openid, grade_level, subjects, term_start_date, updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(openid) DO UPDATE SET grade_level=excluded.grade_level,
                 subjects=excluded.subjects, term_start_date=excluded.term_start_date,
                 updated_at=excluded.updated_at""",
            (openid, grade_level, payload, term_start_date, now),
        )
        await db.commit()
    return {"openid": openid, "grade_level": grade_level, "subjects": list(subjects),
            "term_start_date": term_start_date, "updated_at": now}


# ---------- Git 同步日志 ----------

async def log_git_sync(db_path: str, record: Dict[str, Any]) -> str:
    rid = record.get("id") or uuid.uuid4().hex[:16]
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO git_sync_log(id, task_id, status, committed, pushed, commit_hash,
                                        paths, conflict_record, reason, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (rid, record.get("task_id", ""), record.get("status", "not_configured"),
             1 if record.get("committed") else 0, 1 if record.get("pushed") else 0,
             record.get("commit", ""),
             json.dumps(record.get("paths") or [], ensure_ascii=False),
             record.get("conflict_record", ""), record.get("reason", ""), time.time()),
        )
        await db.commit()
    return rid


async def latest_git_sync(db_path: str) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM git_sync_log ORDER BY created_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    data = dict(row)
    data["committed"] = bool(data.get("committed"))
    data["pushed"] = bool(data.get("pushed"))
    try:
        data["paths"] = json.loads(data.get("paths") or "[]")
    except (TypeError, ValueError):
        data["paths"] = []
    return data


# ---------- 错题台账（去重键：来源+日期+页码+题号 → question_uid）----------

_LEDGER_UPDATABLE = (
    "subject", "source", "page", "stem", "student_answer", "correct_answer",
    "error_rule", "knowledge_point", "status", "archive_path",
)


async def upsert_ledger_question(db_path: str, openid: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """按 (openid, question_uid) 去重写入台账。

    已存在时只更新「可演进字段」，不覆盖 `remediation_state`（该字段由订正/复测事件驱动），
    也不新增重复条目；返回 {id, created}。
    """
    uid = (entry.get("question_uid") or "").strip()
    now = time.time()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        existing = None
        if uid:
            async with db.execute(
                "SELECT * FROM mistakes WHERE openid=? AND question_uid=?", (openid, uid)
            ) as cur:
                existing = await cur.fetchone()

        if existing:
            row = dict(existing)
            fields = {k: entry.get(k, row.get(k, "")) for k in _LEDGER_UPDATABLE}
            # 只有任务结果显式给出新的订正状态时才覆盖（由结果/事件驱动，不被空值抹掉）
            new_state = (entry.get("remediation_state") or "").strip()
            if new_state and new_state != "not_applicable":
                fields["remediation_state"] = new_state
            sets = ", ".join(f"{k}=?" for k in fields)
            await db.execute(
                f"UPDATE mistakes SET {sets}, last_event_at=? WHERE id=?",
                (*fields.values(), now, row["id"]),
            )
            await db.commit()
            return {"id": row["id"], "created": False}

        state = entry.get("remediation_state") or "pending_correction"
        cur = await db.execute(
            """INSERT INTO mistakes(openid, task_id, question_no, knowledge_point, note,
                                    created_at, subject, source, page, question_uid, stem,
                                    student_answer, correct_answer, error_rule, status,
                                    remediation_state, last_event_at, archive_path)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (openid, entry.get("task_id", ""), entry.get("question_no", ""),
             entry.get("knowledge_point", ""), entry.get("note", ""), now,
             entry.get("subject", ""), entry.get("source", ""), entry.get("page", ""),
             uid, entry.get("stem", ""), entry.get("student_answer", ""),
             entry.get("correct_answer", ""), entry.get("error_rule", ""),
             entry.get("status", "wrong"), state, now, entry.get("archive_path", "")),
        )
        await db.commit()
        return {"id": cur.lastrowid, "created": True}


async def list_ledger(db_path: str, openid: str, subject: str = "",
                      states: Optional[Iterable[str]] = None,
                      limit: int = 200, offset: int = 0) -> List[dict]:
    sql = "SELECT * FROM mistakes WHERE openid=? AND question_uid<>''"
    args: List[Any] = [openid]
    if subject:
        sql += " AND subject=?"
        args.append(subject)
    state_list = list(states or [])
    if state_list:
        placeholders = ",".join("?" for _ in state_list)
        sql += f" AND remediation_state IN ({placeholders})"
        args.extend(state_list)
    sql += " ORDER BY COALESCE(NULLIF(last_event_at, 0), created_at) DESC LIMIT ? OFFSET ?"
    args.extend([limit, max(0, offset)])
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def ledger_counts(db_path: str, openid: str) -> Dict[str, int]:
    """按订正状态统计台账条目数（用于复习页概览）；不含人工收藏条目。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT remediation_state, COUNT(*) FROM mistakes
               WHERE openid=? AND question_uid<>'' GROUP BY remediation_state""",
            (openid,),
        ) as cur:
            return {str(row[0] or "unknown"): int(row[1]) for row in await cur.fetchall()}


async def ledger_subjects(db_path: str, openid: str) -> List[str]:
    """台账中出现过的学科（按学科切换 chips 使用）。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT DISTINCT subject FROM mistakes
               WHERE openid=? AND subject<>'' AND question_uid<>'' ORDER BY subject""",
            (openid,),
        ) as cur:
            return [str(row[0]) for row in await cur.fetchall()]


async def get_ledger_by_uid(db_path: str, openid: str, uid: str) -> Optional[dict]:
    if not (uid or "").strip():
        return None
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM mistakes WHERE openid=? AND question_uid=?", (openid, uid)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def list_ledger_by_task(db_path: str, openid: str, task_id: str) -> List[dict]:
    """该任务写入台账的条目（用于任务详情展示与人工核对）。"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM mistakes WHERE openid=? AND task_id=?
               ORDER BY COALESCE(NULLIF(last_event_at, 0), created_at)""",
            (openid, task_id),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_ledger_entry(db_path: str, openid: str, entry_id: int) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM mistakes WHERE id=? AND openid=?", (entry_id, openid)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def update_ledger_state(db_path: str, openid: str, entry_id: int, *,
                              remediation_state: str, last_event_at: float = 0.0,
                              archive_path: str = "") -> None:
    """只更新台账状态相关字段，不改写题目内容与历史判定。"""
    now = last_event_at or time.time()
    async with aiosqlite.connect(db_path) as db:
        if archive_path:
            await db.execute(
                """UPDATE mistakes SET remediation_state=?, last_event_at=?, archive_path=?
                   WHERE id=? AND openid=?""",
                (remediation_state, now, archive_path, entry_id, openid),
            )
        else:
            await db.execute(
                """UPDATE mistakes SET remediation_state=?, last_event_at=?
                   WHERE id=? AND openid=?""",
                (remediation_state, now, entry_id, openid),
            )
        await db.commit()


async def add_question_event(db_path: str, openid: str, event: Dict[str, Any]) -> str:
    """追加订正/复测事件；只追加不改写，历史判定保持可追溯。"""
    rid = uuid.uuid4().hex[:16]
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO question_events(id, openid, question_uid, subject, event_type,
                                           result, occurred_date, student_answer, note,
                                           source_task_id, archive_path, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, openid, event.get("question_uid", ""), event.get("subject", ""),
             event.get("event_type", "retest"), event.get("result", ""),
             event.get("occurred_date", ""), event.get("student_answer", ""),
             event.get("note", ""), event.get("source_task_id", ""),
             event.get("archive_path", ""), time.time()),
        )
        await db.commit()
    return rid


async def list_question_events(db_path: str, openid: str, question_uid: str = "",
                               limit: int = 200) -> List[dict]:
    sql = "SELECT * FROM question_events WHERE openid=?"
    args: List[Any] = [openid]
    if question_uid:
        sql += " AND question_uid=?"
        args.append(question_uid)
    sql += " ORDER BY occurred_date DESC, created_at DESC LIMIT ?"
    args.append(limit)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]
