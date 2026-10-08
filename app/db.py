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
    "hermes_session_id", "input_text", "stage", "stages_json",
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


async def delete_task(db_path: str, task_id: str) -> List[str]:
    """删除任务及其全部关联数据（轮次、附件关联、错题、事件、成果索引、归档日志）。

    返回可删除的本地文件路径列表（已无人引用的附件文件）。
    成果文件（artifacts 在 git 归档工作区内）只删索引不删文件。
    调用方需先校验任务归属与状态（仅失败/中断任务允许删除）。
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT asset_id FROM task_assets WHERE task_id=?", (task_id,)) as cur:
            asset_ids = [r["asset_id"] for r in await cur.fetchall()]

        await db.execute("DELETE FROM task_assets WHERE task_id=?", (task_id,))
        await db.execute("DELETE FROM task_runs WHERE task_id=?", (task_id,))
        await db.execute("DELETE FROM artifacts WHERE task_id=?", (task_id,))
        await db.execute("DELETE FROM mistakes WHERE task_id=?", (task_id,))
        await db.execute("DELETE FROM question_events WHERE source_task_id=?",
                         (task_id,))
        await db.execute("DELETE FROM git_sync_log WHERE task_id=?", (task_id,))
        await db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

        # 附件按 sha256 去重存储：仅当没有其它任务引用时才删文件
        orphan_paths: List[str] = []
        for aid in dict.fromkeys(asset_ids):
            async with db.execute(
                "SELECT 1 FROM task_assets WHERE asset_id=? LIMIT 1",
                (aid,)) as cur:
                if await cur.fetchone():
                    continue
            async with db.execute("SELECT path FROM assets WHERE id=?",
                                 (aid,)) as cur:
                row = await cur.fetchone()
            if row and row["path"]:
                orphan_paths.append(row["path"])
            await db.execute("DELETE FROM assets WHERE id=?", (aid,))
        await db.commit()
    return orphan_paths


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


async def resume_orientation(db_path: str, task_id: str, openid: str,
                             run_id: str, old_stages: str, new_stages: str) -> bool:
    """原轮次原子恢复，重复确认/过期页面/并发请求不能重复派发。"""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cur = await conn.execute(
            """UPDATE task_runs SET status='queued', error='', stages_json=?
               WHERE id=? AND task_id=? AND status='waiting_input' AND stage='orientation'
               AND stages_json=? AND run_no=(SELECT MAX(run_no) FROM task_runs WHERE task_id=?)
               AND EXISTS(SELECT 1 FROM tasks WHERE id=? AND openid=? AND status='waiting_input')""",
            (new_stages, run_id, task_id, old_stages, task_id, task_id, openid))
        if cur.rowcount != 1:
            await conn.rollback()
            return False
        await conn.execute("""UPDATE tasks SET status='pending', error='', claim_owner='',
                              claim_expires_at=0, updated_at=? WHERE id=?""", (time.time(), task_id))
        await conn.commit()
        return True


async def list_runs(db_path: str, task_id: str) -> List[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM task_runs WHERE task_id=? ORDER BY run_no", (task_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def latest_run_stages(db_path: str, task_ids: List[str]) -> Dict[str, str]:
    """取一批任务各自最新轮次的 stage（列表页用：区分 waiting_input 是待确认方向还是待补充材料）。"""
    if not task_ids:
        return {}
    placeholders = ",".join("?" for _ in task_ids)
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            f"""SELECT task_id, stage FROM task_runs
                WHERE task_id IN ({placeholders})
                  AND run_no = (SELECT MAX(run_no) FROM task_runs r2
                                WHERE r2.task_id = task_runs.task_id)""",
            tuple(task_ids),
        ) as cur:
            return {str(r[0]): str(r[1] or "") for r in await cur.fetchall()}


async def expire_stale_orientation_waits(db_path: str, wait_days: int) -> int:
    """将超期未确认方向的任务转 interrupted（之后可删除），避免永久残留。

    只处理方向确认阶段（最新轮次 stage='orientation'）的 waiting_input；
    补充材料（missing_info）的 waiting_input 不受影响。返回转换的任务数。
    wait_days<=0 时关闭。
    """
    if wait_days <= 0:
        return 0
    now = time.time()
    cutoff = now - wait_days * 86400.0
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT id FROM tasks
               WHERE status='waiting_input' AND updated_at < ?
               AND EXISTS (
                   SELECT 1 FROM task_runs r
                   WHERE r.task_id = tasks.id AND r.stage='orientation'
                   AND r.run_no = (SELECT MAX(run_no) FROM task_runs r2
                                   WHERE r2.task_id = tasks.id))""",
            (cutoff,),
        ) as cur:
            ids = [r[0] for r in await cur.fetchall()]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"""UPDATE tasks SET status='interrupted',
                   error='方向确认超时未处理，任务已中止，可删除或重新提交',
                   claim_owner='', claim_expires_at=0, updated_at=?
               WHERE id IN ({placeholders})""",
            (now, *ids),
        )
        await db.execute(
            f"""UPDATE task_runs SET status='interrupted', finished_at=?
               WHERE task_id IN ({placeholders})
               AND run_no = (SELECT MAX(run_no) FROM task_runs r2
                             WHERE r2.task_id = task_runs.task_id)""",
            (now, *ids),
        )
        await db.commit()
        return len(ids)


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


async def save_family_settings(db_path: str, openid: str, *,
                               subjects: List[str], term_start_date: str) -> dict:
    """保存家庭设置（学科清单以 JSON 文本存储，便于扩展新学科）。

    grade_level 不再由产品层写入，但旧列为兼容仍保留；写入时保持原值。
    """
    now = time.time()
    payload = json.dumps(list(subjects), ensure_ascii=False)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO family_settings(openid, subjects, term_start_date, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(openid) DO UPDATE SET subjects=excluded.subjects,
                 term_start_date=excluded.term_start_date, updated_at=excluded.updated_at""",
            (openid, payload, term_start_date, now),
        )
        await db.commit()
    return {"openid": openid, "subjects": list(subjects),
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
    else:
        # 默认视图不含已撤回条目（用户点"我觉得判错了"的题）
        sql += " AND remediation_state<>'withdrawn'"
    sql += " ORDER BY COALESCE(NULLIF(last_event_at, 0), created_at) DESC LIMIT ? OFFSET ?"
    args.extend([limit, max(0, offset)])
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def ledger_counts(db_path: str, openid: str) -> Dict[str, int]:
    """按订正状态统计台账条目数（用于复习页概览）；不含人工收藏条目、不含已撤回条目。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT remediation_state, COUNT(*) FROM mistakes
               WHERE openid=? AND question_uid<>'' AND remediation_state<>'withdrawn'
               GROUP BY remediation_state""",
            (openid,),
        ) as cur:
            return {str(row[0] or "unknown"): int(row[1]) for row in await cur.fetchall()}


async def grading_stats(db_path: str, openid: str, start_ts: float, end_ts: float) -> dict:
    """指定时间窗内已完成批改任务的逐题统计。

    口径：每任务取最后一轮已完成 run 的 result_json；correct=答对；
    checked=correct+wrong+unanswered（uncertain/unprocessed 未给出确定结论，不计入）。
    解析失败的 run 直接跳过，不影响整体。
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT r.result_json FROM task_runs r
               JOIN tasks t ON t.id = r.task_id
               WHERE t.openid=? AND t.task_type='grading' AND r.status='finished'
                 AND r.finished_at>=? AND r.finished_at<?
                 AND r.run_no=(SELECT MAX(run_no) FROM task_runs WHERE task_id=r.task_id)""",
            (openid, start_ts, end_ts),
        ) as cur:
            rows = await cur.fetchall()
    correct = checked = 0
    for (raw,) in rows:
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        for q in payload.get("questions") or []:
            status = (q or {}).get("status", "")
            if status == "correct":
                correct += 1
                checked += 1
            elif status in ("wrong", "unanswered"):
                checked += 1
    return {"correct": correct, "checked": checked}


async def top_error_causes(db_path: str, openid: str, since_ts: float,
                           limit: int = 3) -> List[dict]:
    """近 N 天台账高频错因（按 error_rule 归一化统计）。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT error_rule, COUNT(*) FROM mistakes
               WHERE openid=? AND error_rule<>'' AND created_at>=?
               GROUP BY error_rule ORDER BY COUNT(*) DESC LIMIT ?""",
            (openid, since_ts, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [{"cause": str(cause), "count": int(n)} for cause, n in rows]


# ---------- 周总结（每周日凌晨生成，按账号×科目）----------

_UNCATEGORIZED_SUBJECT = "未分类"


def normalize_subject(subject: str) -> str:
    """科目归一化：空字符串统一记为「未分类」，保证按科目聚合不丢数据。"""
    return (subject or "").strip() or _UNCATEGORIZED_SUBJECT


async def weekly_grading_by_subject(db_path: str, openid: str,
                                    start_ts: float, end_ts: float) -> Dict[str, dict]:
    """指定时间窗内已完成批改任务的逐题统计，按任务科目分组。

    口径与 grading_stats 一致：每任务取最后一轮已完成 run 的 result_json；
    uncertain 未给出确定结论，不计入正确率分母。
    返回 {subject: {tasks, correct, wrong, unanswered, uncertain, checked}}。
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT t.subject, r.result_json FROM task_runs r
               JOIN tasks t ON t.id = r.task_id
               WHERE t.openid=? AND t.task_type='grading' AND r.status='finished'
                 AND r.finished_at>=? AND r.finished_at<?
                 AND r.run_no=(SELECT MAX(run_no) FROM task_runs WHERE task_id=r.task_id)""",
            (openid, start_ts, end_ts),
        ) as cur:
            rows = await cur.fetchall()
    stats: Dict[str, dict] = {}
    for raw_subject, raw in rows:
        subject = normalize_subject(str(raw_subject or ""))
        st = stats.setdefault(subject, {"tasks": 0, "correct": 0, "wrong": 0,
                                        "unanswered": 0, "uncertain": 0, "checked": 0})
        st["tasks"] += 1
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        for q in payload.get("questions") or []:
            status = (q or {}).get("status", "")
            if status == "correct":
                st["correct"] += 1
                st["checked"] += 1
            elif status == "wrong":
                st["wrong"] += 1
                st["checked"] += 1
            elif status == "unanswered":
                st["unanswered"] += 1
                st["checked"] += 1
            elif status == "uncertain":
                st["uncertain"] += 1
    return stats


async def weekly_mistake_stats(db_path: str, openid: str,
                               start_ts: float, end_ts: float) -> Dict[str, dict]:
    """指定时间窗内新增台账条目统计，按科目分组。

    用户点「我觉得判错了」已撤回的条目不计入新增错题。
    返回 {subject: {new_mistakes, top_causes[{cause,count}], top_points[{point,count}]}}。
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT subject, error_rule, knowledge_point FROM mistakes
               WHERE openid=? AND question_uid<>''
                 AND remediation_state<>'withdrawn'
                 AND created_at>=? AND created_at<?""",
            (openid, start_ts, end_ts),
        ) as cur:
            rows = await cur.fetchall()
    stats: Dict[str, dict] = {}
    for raw_subject, cause, point in rows:
        subject = normalize_subject(str(raw_subject or ""))
        st = stats.setdefault(subject, {"new_mistakes": 0, "causes": {}, "points": {}})
        st["new_mistakes"] += 1
        cause = (cause or "").strip()
        if cause:
            st["causes"][cause] = st["causes"].get(cause, 0) + 1
        point = (point or "").strip()
        if point:
            st["points"][point] = st["points"].get(point, 0) + 1
    out = {}
    for subject, st in stats.items():
        out[subject] = {
            "new_mistakes": st["new_mistakes"],
            "top_causes": [{"cause": c, "count": n}
                           for c, n in sorted(st["causes"].items(),
                                              key=lambda kv: (-kv[1], kv[0]))[:5]],
            "top_points": [{"point": p, "count": n}
                           for p, n in sorted(st["points"].items(),
                                              key=lambda kv: (-kv[1], kv[0]))[:5]],
        }
    return out


async def weekly_event_counts(db_path: str, openid: str,
                              start_ts: float, end_ts: float) -> Dict[str, dict]:
    """指定时间窗内订正/复测事件数，按科目分组。

    返回 {subject: {corrections, retests}}。
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT subject, event_type, COUNT(*) FROM question_events
               WHERE openid=? AND created_at>=? AND created_at<?
               GROUP BY subject, event_type""",
            (openid, start_ts, end_ts),
        ) as cur:
            rows = await cur.fetchall()
    stats: Dict[str, dict] = {}
    for raw_subject, event_type, n in rows:
        subject = normalize_subject(str(raw_subject or ""))
        st = stats.setdefault(subject, {"corrections": 0, "retests": 0})
        if event_type == "correction":
            st["corrections"] += int(n)
        elif event_type == "retest":
            st["retests"] += int(n)
    return stats


async def weekly_pending_by_subject(db_path: str, openid: str) -> Dict[str, dict]:
    """当前各科目的待办快照：待订正 / 待复测（含复测未通过）。已撤回的不计。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT subject, remediation_state, COUNT(*) FROM mistakes
               WHERE openid=? AND question_uid<>''
                 AND remediation_state<>'withdrawn'
               GROUP BY subject, remediation_state""",
            (openid,),
        ) as cur:
            rows = await cur.fetchall()
    stats: Dict[str, dict] = {}
    for raw_subject, state, n in rows:
        subject = normalize_subject(str(raw_subject or ""))
        st = stats.setdefault(subject, {"pending_correction": 0, "pending_retest": 0})
        if state == "pending_correction":
            st["pending_correction"] += int(n)
        elif state in ("corrected_pending_retest", "retest_failed"):
            st["pending_retest"] += int(n)
    return stats


async def list_week_mistakes(db_path: str, openid: str, subject: str,
                           start_ts: float, end_ts: float,
                           limit: int = 30) -> List[dict]:
    """指定时间窗内某科目新增错题的详情（供 AI 归类分析用）。

    subject 为归一化后的科目名（空科目记「未分类」）；不含已撤回条目；
    按创建时间排序，取前 limit 条。
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT subject, question_no, stem, student_answer, correct_answer,
                      error_rule, knowledge_point FROM mistakes
               WHERE openid=? AND question_uid<>''
                 AND remediation_state<>'withdrawn'
                 AND created_at>=? AND created_at<?
               ORDER BY created_at LIMIT ?""",
            (openid, start_ts, end_ts, max(1, limit) * 2),
        ) as cur:
            rows = await cur.fetchall()
    out = []
    for r in rows:
        if normalize_subject(str(r[0] or "")) != subject:
            continue
        out.append({
            "question_no": str(r[1] or ""),
            "stem": str(r[2] or ""),
            "student_answer": str(r[3] or ""),
            "correct_answer": str(r[4] or ""),
            "error_rule": str(r[5] or ""),
            "knowledge_point": str(r[6] or ""),
        })
        if len(out) >= max(1, limit):
            break
    return out


async def week_has_data(db_path: str, openid: str,
                        start_ts: float, end_ts: float) -> bool:
    """该周是否有可总结的数据：有已完成的批改任务或新增台账条目。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT 1 FROM task_runs r JOIN tasks t ON t.id=r.task_id
               WHERE t.openid=? AND t.task_type='grading' AND r.status='finished'
                 AND r.finished_at>=? AND r.finished_at<?
                 AND r.run_no=(SELECT MAX(run_no) FROM task_runs WHERE task_id=r.task_id)
               LIMIT 1""",
            (openid, start_ts, end_ts),
        ) as cur:
            if await cur.fetchone():
                return True
        async with db.execute(
            """SELECT 1 FROM mistakes
               WHERE openid=? AND question_uid<>'' AND created_at>=? AND created_at<?
               LIMIT 1""",
            (openid, start_ts, end_ts),
        ) as cur:
            return bool(await cur.fetchone())


async def upsert_weekly_summary(db_path: str, openid: str, week_start: str,
                                subject: str, summary: dict) -> None:
    """写入/覆盖某账号某周某科目的周总结（幂等：同一周重复生成直接覆盖）。"""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO weekly_summaries(openid, week_start, subject, summary_json, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(openid, week_start, subject)
               DO UPDATE SET summary_json=excluded.summary_json,
                             created_at=excluded.created_at""",
            (openid, week_start, subject, json.dumps(summary, ensure_ascii=False),
             time.time()),
        )
        await db.commit()


async def has_weekly_summary(db_path: str, openid: str, week_start: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT 1 FROM weekly_summaries WHERE openid=? AND week_start=? LIMIT 1",
            (openid, week_start),
        ) as cur:
            return bool(await cur.fetchone())


async def list_weekly_weeks(db_path: str, openid: str, limit: int = 12) -> List[str]:
    """该账号已生成周总结的周一日期列表（倒序）。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT DISTINCT week_start FROM weekly_summaries
               WHERE openid=? ORDER BY week_start DESC LIMIT ?""",
            (openid, limit),
        ) as cur:
            return [str(row[0]) for row in await cur.fetchall()]


async def get_weekly_summaries(db_path: str, openid: str,
                               week_start: str) -> List[dict]:
    """取某账号某周各科目的周总结（按科目名排序）。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT subject, summary_json FROM weekly_summaries
               WHERE openid=? AND week_start=? ORDER BY subject""",
            (openid, week_start),
        ) as cur:
            rows = await cur.fetchall()
    out = []
    for subject, raw in rows:
        try:
            summary = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        out.append({"subject": str(subject), "summary": summary})
    return out


async def list_user_openids(db_path: str) -> List[str]:
    """所有登录过或产生过数据的账号身份（用于全账号任务：定时清理、工作区骨架）。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT openid FROM users UNION
               SELECT openid FROM tasks UNION
               SELECT openid FROM mistakes UNION
               SELECT openid FROM family_settings"""
        ) as cur:
            return [str(row[0]) for row in await cur.fetchall() if row[0]]


async def purge_expired(db_path: str, openid: str, retention_days: int) -> dict:
    """按账号清理超期错题台账。

    范围（已与用户确认）：只清理「错题台账（mistakes）」及其关联的
    「订正/复测事件（question_events）」。任务、附件、Git 归档一律不动。
    判定口径：last_event_at（无值回落 created_at）早于保留期截止线。
    """
    cutoff = time.time() - max(int(retention_days or 0), 0) * 86400.0
    removed = {"mistakes": 0, "question_events": 0}
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT id, question_uid FROM mistakes
               WHERE openid=? AND COALESCE(NULLIF(last_event_at, 0), created_at) < ?""",
            (openid, cutoff),
        ) as cur:
            stale = [(int(r[0]), str(r[1] or "")) for r in await cur.fetchall()]
        if not stale:
            return removed
        uids = sorted({uid for _, uid in stale if uid})
        if uids:
            placeholders = ",".join("?" for _ in uids)
            async with db.execute(
                f"DELETE FROM question_events WHERE openid=? AND question_uid IN ({placeholders})",
                (openid, *uids),
            ) as cur:
                removed["question_events"] = cur.rowcount or 0
        ids = [i for i, _ in stale]
        placeholders = ",".join("?" for _ in ids)
        async with db.execute(
            f"DELETE FROM mistakes WHERE id IN ({placeholders})", (*ids,)
        ) as cur:
            removed["mistakes"] = cur.rowcount or 0
        await db.commit()
    return removed


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
