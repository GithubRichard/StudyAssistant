"""SQLite 数据层（零配置）。日活上万后再考虑换 Postgres。"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  openid TEXT PRIMARY KEY,
  bonus_quota INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_usage(
  openid TEXT NOT NULL,
  day TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(openid, day)
);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY,
  openid TEXT NOT NULL,
  subject TEXT NOT NULL,
  grade_level TEXT NOT NULL,
  image_path TEXT NOT NULL,
  status TEXT NOT NULL,              -- pending/grading/done/failed
  result_json TEXT,
  provider TEXT,
  model TEXT,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  cost_cny REAL DEFAULT 0,
  error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mistakes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  openid TEXT NOT NULL,
  task_id TEXT NOT NULL,
  question_no TEXT NOT NULL,
  knowledge_point TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_cost(
  day TEXT PRIMARY KEY,
  cost REAL NOT NULL DEFAULT 0
);
"""


async def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


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
    """扣 1 次，优先扣赠送次数。返回是否成功。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT bonus_quota FROM users WHERE openid=?", (openid,)) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        if row[0] > 0:
            await db.execute(
                "UPDATE users SET bonus_quota=bonus_quota-1 WHERE openid=?", (openid,)
            )
        else:
            await db.execute(
                """INSERT INTO quota_usage(openid, day, used) VALUES(?,?,1)
                   ON CONFLICT(openid, day) DO UPDATE SET used=used+1""",
                (openid, _today()),
            )
        await db.commit()
        return True


# ---------- 任务 ----------

async def create_task(db_path: str, task: dict) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO tasks(id, openid, subject, grade_level, image_path, status,
                                 created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (task["id"], task["openid"], task["subject"], task["grade_level"],
             task["image_path"], "pending", task["created_at"], task["created_at"]),
        )
        await db.commit()


async def get_task(db_path: str, task_id: str) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def update_task(db_path: str, task_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    keys = ", ".join(f"{k}=?" for k in fields)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"UPDATE tasks SET {keys} WHERE id=?", (*fields.values(), task_id))
        await db.commit()


async def list_tasks(db_path: str, openid: str, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE openid=? ORDER BY created_at DESC LIMIT ?",
            (openid, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


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


async def list_mistakes(db_path: str, openid: str, limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM mistakes WHERE openid=? ORDER BY created_at DESC LIMIT ?",
            (openid, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
