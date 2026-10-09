"""SQLite 版本化增量迁移。

原则：
- 迁移前若库中已有数据则先备份（`<db>.bak-<时间戳>`），旧任务原样保留。
- 只做加表、加列、建索引；不删除、不改写历史数据。
- 每个版本只应用一次，`schema_version` 表记录已应用版本。
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

# 版本 1：原始基础表（与初版一致，保证旧库可直接升级）
V1_DDL = """
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
  status TEXT NOT NULL,
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

# 版本 2：会话、附件、执行轮次、幂等、配额预留、成果索引
V2_DDL = """
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY,
  openid TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  last_seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_openid ON sessions(openid);

CREATE TABLE IF NOT EXISTS assets(
  id TEXT PRIMARY KEY,
  openid TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  mime TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  path TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_openid ON assets(openid, created_at);

CREATE TABLE IF NOT EXISTS task_assets(
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  asset_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  PRIMARY KEY(task_id, run_id, asset_id)
);

CREATE TABLE IF NOT EXISTS task_runs(
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_no INTEGER NOT NULL,
  kind TEXT NOT NULL,
  input_text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  hermes_session_id TEXT NOT NULL DEFAULT '',
  started_at REAL,
  finished_at REAL,
  error TEXT NOT NULL DEFAULT '',
  result_json TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id, run_no);

CREATE TABLE IF NOT EXISTS idempotency(
  key TEXT PRIMARY KEY,
  openid TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  task_id TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_reservations(
  id TEXT PRIMARY KEY,
  openid TEXT NOT NULL,
  task_id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'daily',
  state TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resv_task ON quota_reservations(task_id);

CREATE TABLE IF NOT EXISTS artifacts(
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, kind);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_openid ON tasks(openid, created_at);
"""

# 版本 3：家庭配置、错题台账升级、订正与复测事件、Git 同步日志
# 说明：mistakes 的扩展列与索引分开执行（索引依赖 ALTER 之后才存在的列）。
V3_DDL = """
CREATE TABLE IF NOT EXISTS family_settings(
  openid TEXT PRIMARY KEY,
  grade_level TEXT NOT NULL DEFAULT '',
  subjects TEXT NOT NULL DEFAULT '',
  term_start_date TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS question_events(
  id TEXT PRIMARY KEY,
  openid TEXT NOT NULL,
  question_uid TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  event_type TEXT NOT NULL,
  result TEXT NOT NULL DEFAULT '',
  occurred_date TEXT NOT NULL DEFAULT '',
  student_answer TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  source_task_id TEXT NOT NULL DEFAULT '',
  archive_path TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_question_events_uid
  ON question_events(openid, question_uid, occurred_date);

CREATE TABLE IF NOT EXISTS git_sync_log(
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  committed INTEGER NOT NULL DEFAULT 0,
  pushed INTEGER NOT NULL DEFAULT 0,
  commit_hash TEXT NOT NULL DEFAULT '',
  paths TEXT NOT NULL DEFAULT '',
  conflict_record TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_git_sync_task ON git_sync_log(task_id, created_at);
"""

# mistakes 台账扩展列（列名 → 列定义）：保留旧数据，只加列
V3_MISTAKE_COLUMNS = {
    "subject": "TEXT NOT NULL DEFAULT ''",
    "source": "TEXT NOT NULL DEFAULT ''",
    "page": "TEXT NOT NULL DEFAULT ''",
    "question_uid": "TEXT NOT NULL DEFAULT ''",
    "stem": "TEXT NOT NULL DEFAULT ''",
    "student_answer": "TEXT NOT NULL DEFAULT ''",
    "correct_answer": "TEXT NOT NULL DEFAULT ''",
    "error_rule": "TEXT NOT NULL DEFAULT ''",
    "status": "TEXT NOT NULL DEFAULT 'wrong'",
    "remediation_state": "TEXT NOT NULL DEFAULT 'pending_correction'",
    "last_event_at": "REAL NOT NULL DEFAULT 0",
    "archive_path": "TEXT NOT NULL DEFAULT ''",
}

# 依赖 V3 新列的索引与条件唯一索引（question_uid 为空的历史数据不参与去重）
V3_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_mistakes_ledger
  ON mistakes(openid, subject, remediation_state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mistakes_uid
  ON mistakes(openid, question_uid) WHERE question_uid <> '';
"""

# tasks 表新增列（列名 → 列定义）
V3_TASK_COLUMNS = {
    "exam_scope": "TEXT NOT NULL DEFAULT ''",
    "training_kind": "TEXT NOT NULL DEFAULT ''",
    "scope_start": "TEXT NOT NULL DEFAULT ''",
    "scope_end": "TEXT NOT NULL DEFAULT ''",
    "git_status": "TEXT NOT NULL DEFAULT ''",
}

# tasks 表新增列（列名 → 列定义）
V2_TASK_COLUMNS = {
    "task_type": "TEXT NOT NULL DEFAULT 'grading'",
    "input_text": "TEXT NOT NULL DEFAULT ''",
    "run_count": "INTEGER NOT NULL DEFAULT 0",
    "claim_owner": "TEXT NOT NULL DEFAULT ''",
    "claim_expires_at": "REAL NOT NULL DEFAULT 0",
    "idempotency_key": "TEXT NOT NULL DEFAULT ''",
    "archive_path": "TEXT NOT NULL DEFAULT ''",
}

LATEST_VERSION = 5


async def _column_names(db: aiosqlite.Connection, table: str) -> set:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return {r[1] for r in rows}


async def _applied_versions(db: aiosqlite.Connection) -> set:
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ) as cur:
        if not await cur.fetchone():
            return set()
    async with db.execute("SELECT version FROM schema_version") as cur:
        return {int(r[0]) for r in await cur.fetchall()}


async def _has_existing_data(db: aiosqlite.Connection) -> bool:
    for table in ("tasks", "users", "mistakes"):
        try:
            async with db.execute(f"SELECT 1 FROM {table} LIMIT 1") as cur:
                if await cur.fetchone():
                    return True
        except Exception:  # noqa: BLE001 - 表不存在视为无数据
            continue
    return False


def backup_database(path: Path) -> str:
    """迁移前备份，返回备份路径；失败时抛出，由调用方决定是否继续。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return str(target)


async def run_migrations(path: str) -> dict:
    """执行迁移，返回 {applied:[...], backup: str|None}。"""
    db_file = Path(path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    backup: str | None = None
    applied: list = []

    async with aiosqlite.connect(path) as db:
        versions = await _applied_versions(db)
        if 1 not in versions and await _has_existing_data(db):
            # 旧库（无版本表但有数据）：先备份再升级
            try:
                backup = backup_database(db_file)
                log.info("迁移前已备份数据库: %s", backup)
            except OSError as e:
                raise RuntimeError(f"数据库备份失败，已中止迁移: {e}") from e

        await db.executescript(V1_DDL)
        if 1 not in versions:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            await db.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(1, ?)",
                             (time.time(),))
            applied.append(1)

        if 2 not in versions:
            await db.executescript(V2_DDL)
            existing = await _column_names(db, "tasks")
            for name, ddl in V2_TASK_COLUMNS.items():
                if name not in existing:
                    await db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            await db.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(2, ?)",
                             (time.time(),))
            applied.append(2)

        if 3 not in versions:
            await db.executescript(V3_DDL)
            task_columns = await _column_names(db, "tasks")
            for name, ddl in V3_TASK_COLUMNS.items():
                if name not in task_columns:
                    await db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
            mistake_columns = await _column_names(db, "mistakes")
            for name, ddl in V3_MISTAKE_COLUMNS.items():
                if name not in mistake_columns:
                    await db.execute(f"ALTER TABLE mistakes ADD COLUMN {name} {ddl}")
            await db.executescript(V3_INDEX_DDL)
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            await db.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(3, ?)",
                             (time.time(),))
            applied.append(3)

        if 4 not in versions:
            # v4：分阶段批改的阶段落库（stage=当前阶段名，stages_json=各阶段产出）
            run_columns = await _column_names(db, "task_runs")
            for name, ddl in (("stage", "TEXT NOT NULL DEFAULT ''"),
                              ("stages_json", "TEXT NOT NULL DEFAULT ''")):
                if name not in run_columns:
                    await db.execute(f"ALTER TABLE task_runs ADD COLUMN {name} {ddl}")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            await db.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(4, ?)",
                             (time.time(),))
            applied.append(4)

        if 5 not in versions:
            # v5：周总结（每周日凌晨按账号×科目生成，幂等覆盖）
            await db.executescript(
                """CREATE TABLE IF NOT EXISTS weekly_summaries(
                     openid TEXT NOT NULL,
                     week_start TEXT NOT NULL,
                     subject TEXT NOT NULL,
                     summary_json TEXT NOT NULL,
                     created_at REAL NOT NULL,
                     PRIMARY KEY(openid, week_start, subject)
                   )""")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            await db.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(5, ?)",
                             (time.time(),))
            applied.append(5)

        if 6 not in versions:
            # v6：错题台账加 AI 重绘示意图（SVG 矢量图，数学几何题用）
            mistake_columns = await _column_names(db, "mistakes")
            if "diagram_svg" not in mistake_columns:
                await db.execute(
                    "ALTER TABLE mistakes ADD COLUMN diagram_svg TEXT NOT NULL DEFAULT ''")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            await db.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(6, ?)",
                             (time.time(),))
            applied.append(6)

        if 7 not in versions:
            # v7：问问题聊天（#/learn?tab=qa）：会话 + 多轮消息，按 openid 隔离
            await db.executescript(
                """CREATE TABLE IF NOT EXISTS qa_sessions(
                     id TEXT PRIMARY KEY,
                     openid TEXT NOT NULL,
                     title TEXT NOT NULL DEFAULT '',
                     created_at REAL NOT NULL,
                     updated_at REAL NOT NULL
                   );
                   CREATE INDEX IF NOT EXISTS idx_qa_sessions_openid
                     ON qa_sessions(openid, updated_at DESC);
                   CREATE TABLE IF NOT EXISTS qa_messages(
                     id TEXT PRIMARY KEY,
                     session_id TEXT NOT NULL,
                     role TEXT NOT NULL,
                     content TEXT NOT NULL DEFAULT '',
                     images_json TEXT NOT NULL DEFAULT '[]',
                     created_at REAL NOT NULL
                   );
                   CREATE INDEX IF NOT EXISTS idx_qa_messages_session
                     ON qa_messages(session_id, created_at);""")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            await db.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(7, ?)",
                             (time.time(),))
            applied.append(7)

        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.commit()

    return {"applied": applied, "backup": backup}


async def current_version(path: str) -> int:
    async with aiosqlite.connect(path) as db:
        versions = await _applied_versions(db)
    return max(versions) if versions else 0
