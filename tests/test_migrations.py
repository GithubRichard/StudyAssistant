"""迁移测试：旧库升级先备份、旧任务保留、重复执行幂等。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from app import db, migrations


async def build_legacy_db(path: str) -> None:
    """构造一个「没有版本表但有数据」的旧库。"""
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(migrations.V1_DDL)
        await conn.execute("INSERT INTO users(openid, bonus_quota, created_at) VALUES(?,?,?)",
                           ("legacy-user", 5, 1.0))
        await conn.execute(
            """INSERT INTO tasks(id, openid, subject, grade_level, image_path, status,
                                 created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("old-task", "legacy-user", "数学", "七年级", "old.jpg", "done", 1.0, 1.0))
        await conn.execute(
            "UPDATE tasks SET result_json=? WHERE id=?",
            ('{"total_questions":1,"correct_count":1,"questions":[],"summary":"旧结果"}',
             "old-task"))
        await conn.commit()


class MigrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "data" / "app.db")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_legacy_db_is_backed_up_and_upgraded(self):
        await build_legacy_db(self.db_path)
        result = await migrations.run_migrations(self.db_path)

        self.assertEqual(sorted(result["applied"]), [1, 2, 3])
        self.assertIsNotNone(result["backup"], "旧库升级前必须备份")
        self.assertTrue(Path(result["backup"]).exists())
        self.assertEqual(await migrations.current_version(self.db_path), 3)

        # 旧任务保留且可读
        task = await db.get_task(self.db_path, "old-task")
        self.assertEqual(task["openid"], "legacy-user")
        self.assertEqual(task["task_type"], "grading")  # 新列默认值
        self.assertEqual(task["exam_scope"], "")
        self.assertEqual(task["training_kind"], "")
        self.assertIn("旧结果", task["result_json"])

    async def test_second_run_is_noop_without_backup(self):
        await build_legacy_db(self.db_path)
        await migrations.run_migrations(self.db_path)
        again = await migrations.run_migrations(self.db_path)
        self.assertEqual(again["applied"], [])
        self.assertIsNone(again["backup"])

    async def test_fresh_db_has_no_backup(self):
        result = await migrations.run_migrations(self.db_path)
        self.assertEqual(sorted(result["applied"]), [1, 2, 3])
        self.assertIsNone(result["backup"])

    async def test_new_tables_exist(self):
        await db.init_db(self.db_path)
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                names = {r[0] for r in await cur.fetchall()}
        for table in ("sessions", "assets", "task_assets", "task_runs",
                      "idempotency", "quota_reservations", "artifacts",
                      "family_settings", "question_events", "git_sync_log"):
            self.assertIn(table, names)


if __name__ == "__main__":
    unittest.main()
