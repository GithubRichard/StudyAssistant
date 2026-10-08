"""失败任务删除：DELETE /api/tasks/{id}（仅失败/中断任务可删）。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import aiosqlite
import httpx
from fastapi import FastAPI

from app import api, db
from app.auth import web_openid
from tests.test_web_v1 import make_settings


async def seed_raw_task(db_path: str, openid: str, task_id: str,
                        status: str, image_path: str = "") -> None:
    now = time.time()
    async with aiosqlite.connect(db_path) as d:
        await d.execute(
            """INSERT INTO tasks(id, openid, subject, grade_level, image_path,
                                 status, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (task_id, openid, "数学", "", image_path, status, now, now))
        await d.commit()


async def seed_task_extras(db_path: str, openid: str, task_id: str,
                           asset_id: str) -> None:
    """给任务挂上轮次、附件关联、错题、事件、归档日志（模拟失败任务残留）。"""
    now = time.time()
    async with aiosqlite.connect(db_path) as d:
        await d.execute(
            """INSERT INTO task_runs(id, task_id, run_no, kind, status, created_at)
               VALUES(?,?,?,?,?,?)""",
            (f"run-{task_id}", task_id, 1, "initial", "failed", now))
        await d.execute(
            """INSERT INTO task_assets(task_id, run_id, asset_id, position, created_at)
               VALUES(?,?,?,?,?)""",
            (task_id, f"run-{task_id}", asset_id, 0, now))
        await d.execute(
            """INSERT INTO mistakes(openid, task_id, question_uid, question_no,
                                    created_at)
               VALUES(?,?,?,?,?)""",
            (openid, task_id, f"uid-{task_id}", "1", now))
        await d.execute(
            """INSERT INTO question_events(id, openid, question_uid, subject,
                                           event_type, source_task_id, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (f"ev-{task_id}", openid, f"uid-{task_id}", "数学",
             "corrected", task_id, now))
        await d.execute(
            "INSERT INTO git_sync_log(id, task_id, status, created_at) VALUES(?,?,?,?)",
            (f"git-{task_id}", task_id, "failed", now))
        await d.execute(
            """INSERT INTO artifacts(id, task_id, kind, path, created_at)
               VALUES(?,?,?,?,?)""",
            (f"art-{task_id}", task_id, "archive_md",
             f"workspace/{task_id}/archive.md", now))
        await d.commit()


def make_asset_file(data_dir: str, name: str) -> str:
    p = Path(data_dir) / "assets" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake-image-bytes")
    return str(p)


async def seed_asset(db_path: str, openid: str, asset_id: str, path: str) -> None:
    async with aiosqlite.connect(db_path) as d:
        await d.execute(
            """INSERT INTO assets(id, openid, sha256, mime, bytes, path, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (asset_id, openid, f"sha-{asset_id}", "image/jpeg", 16, path,
             time.time()))
        await d.commit()


async def link_extra_asset(db_path: str, task_id: str, asset_id: str) -> None:
    async with aiosqlite.connect(db_path) as d:
        await d.execute(
            """INSERT OR IGNORE INTO task_assets(task_id, run_id, asset_id, position,
                                                 created_at)
               VALUES(?,?,?,?,?)""",
            (task_id, f"run-{task_id}", asset_id, 1, time.time()))
        await d.commit()


async def count(db_path: str, table: str, where: str = "", args=()) -> int:
    async with aiosqlite.connect(db_path) as d:
        async with d.execute(f"SELECT COUNT(*) FROM {table} " + where, args) as cur:
            return (await cur.fetchone())[0]


class TaskDeleteDbTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        self.db_path = self.settings.db_path
        await db.init_db(self.db_path)
        self.openid = web_openid("kid1")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_delete_task_cleans_everything(self):
        # t1 独占 asset-a；t2 与 t1 共享 asset-shared
        await seed_raw_task(self.db_path, self.openid, "t1", "failed")
        await seed_raw_task(self.db_path, self.openid, "t2", "failed")
        pa = make_asset_file(self.settings.data_dir, "a.jpg")
        ps = make_asset_file(self.settings.data_dir, "shared.jpg")
        await seed_asset(self.db_path, self.openid, "asset-a", pa)
        await seed_asset(self.db_path, self.openid, "asset-shared", ps)
        await seed_task_extras(self.db_path, self.openid, "t1", "asset-a")
        await link_extra_asset(self.db_path, "t1", "asset-shared")
        await seed_task_extras(self.db_path, self.openid, "t2", "asset-shared")

        orphans = await db.delete_task(self.db_path, "t1")

        # t1 的一切关联数据都没了
        self.assertIsNone(await db.get_task(self.db_path, "t1"))
        for table, col in [("task_runs", "task_id"), ("task_assets", "task_id"),
                           ("artifacts", "task_id"), ("mistakes", "task_id"),
                           ("question_events", "source_task_id"),
                           ("git_sync_log", "task_id")]:
            self.assertEqual(
                await count(self.db_path, table, f"WHERE {col}=?", ("t1",)), 0,
                table)
        # t2 不受影响
        self.assertIsNotNone(await db.get_task(self.db_path, "t2"))
        self.assertEqual(
            await count(self.db_path, "task_assets", "WHERE task_id=?", ("t2",)), 1)
        # 独占附件可删，共享附件保留
        self.assertIn(pa, orphans)
        self.assertNotIn(ps, orphans)
        self.assertEqual(await count(self.db_path, "assets", "WHERE id=?",
                                    ("asset-shared",)), 1)
        self.assertEqual(await count(self.db_path, "assets", "WHERE id=?",
                                    ("asset-a",)), 0)


class TaskDeleteApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        self.db_path = self.settings.db_path
        await db.init_db(self.db_path)
        api.settings = self.settings
        app = FastAPI()
        app.include_router(api.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test")
        res = await self.client.post("/api/web/login",
                                     data={"username": "kid1", "password": "pw-one"})
        self.assertEqual(res.status_code, 200)
        self.headers = {"Authorization": f"Bearer {res.json()['token']}"}
        self.openid = web_openid("kid1")
        self.other_openid = web_openid("kid2")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_delete_failed_task(self):
        await seed_raw_task(self.db_path, self.openid, "t1", "failed")
        p = make_asset_file(self.settings.data_dir, "del.jpg")
        await seed_asset(self.db_path, self.openid, "asset-1", p)
        await seed_task_extras(self.db_path, self.openid, "t1", "asset-1")
        self.assertTrue(Path(p).exists())

        res = await self.client.delete("/api/tasks/t1", headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsNone(await db.get_task(self.db_path, "t1"))
        self.assertFalse(Path(p).exists(), "附件文件应一并删除")
        self.assertEqual(res.json()["removed_files"], 1)

    async def test_delete_done_task_rejected(self):
        await seed_raw_task(self.db_path, self.openid, "t9", "done")
        res = await self.client.delete("/api/tasks/t9", headers=self.headers)
        self.assertEqual(res.status_code, 400)
        self.assertIsNotNone(await db.get_task(self.db_path, "t9"))

    async def test_delete_other_user_task_forbidden(self):
        await seed_raw_task(self.db_path, self.other_openid, "tx", "failed")
        res = await self.client.delete("/api/tasks/tx", headers=self.headers)
        self.assertIn(res.status_code, (401, 403, 404))
        self.assertIsNotNone(await db.get_task(self.db_path, "tx"))

    async def test_delete_nonexistent(self):
        res = await self.client.delete("/api/tasks/nope", headers=self.headers)
        self.assertEqual(res.status_code, 404)

    async def test_delete_unauthorized(self):
        res = await self.client.delete("/api/tasks/t1")
        self.assertIn(res.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()


async def seed_run(db_path: str, task_id: str, stage: str, run_no: int = 1) -> None:
    now = time.time()
    async with aiosqlite.connect(db_path) as d:
        await d.execute(
            """INSERT INTO task_runs(id, task_id, run_no, kind, status, stage, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (f"run-{task_id}-{run_no}", task_id, run_no, "initial",
             "waiting_input", stage, now))
        await d.commit()


class OrientationWaitDeleteTest(unittest.IsolatedAsyncioTestCase):
    """方向待确认的 waiting_input 任务可删除（无批改结果、无台账）；
    补充材料阶段的 waiting_input 仍不可删。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        self.db_path = self.settings.db_path
        await db.init_db(self.db_path)
        api.settings = self.settings
        app = FastAPI()
        app.include_router(api.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test")
        res = await self.client.post("/api/web/login",
                                     data={"username": "kid1", "password": "pw-one"})
        self.assertEqual(res.status_code, 200)
        self.headers = {"Authorization": f"Bearer {res.json()['token']}"}
        self.openid = web_openid("kid1")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_delete_orientation_waiting_input_allowed(self):
        await seed_raw_task(self.db_path, self.openid, "tw1", "waiting_input")
        await seed_run(self.db_path, "tw1", "orientation")
        res = await self.client.delete("/api/tasks/tw1", headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsNone(await db.get_task(self.db_path, "tw1"))

    async def test_delete_missing_info_waiting_input_rejected(self):
        await seed_raw_task(self.db_path, self.openid, "tw2", "waiting_input")
        await seed_run(self.db_path, "tw2", "staged")
        res = await self.client.delete("/api/tasks/tw2", headers=self.headers)
        self.assertEqual(res.status_code, 400)
        self.assertIsNotNone(await db.get_task(self.db_path, "tw2"))

    async def test_list_marks_orientation_pending(self):
        await seed_raw_task(self.db_path, self.openid, "tw3", "waiting_input")
        await seed_run(self.db_path, "tw3", "orientation")
        await seed_raw_task(self.db_path, self.openid, "tw4", "waiting_input")
        await seed_run(self.db_path, "tw4", "staged")
        res = await self.client.get("/api/tasks?limit=50", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        items = {t["id"]: t for t in res.json()}
        self.assertTrue(items["tw3"]["orientation_pending"])
        self.assertFalse(items["tw4"]["orientation_pending"])


class ExpireOrientationWaitTest(unittest.IsolatedAsyncioTestCase):
    """超期未确认方向的任务自动转 interrupted（可删除）；补充材料的不动。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        self.db_path = self.settings.db_path
        await db.init_db(self.db_path)
        self.openid = web_openid("kid1")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _seed_wait(self, task_id: str, stage: str, days_ago: float) -> None:
        await seed_raw_task(self.db_path, self.openid, task_id, "waiting_input")
        await seed_run(self.db_path, task_id, stage)
        old = time.time() - days_ago * 86400.0
        async with aiosqlite.connect(self.db_path) as d:
            await d.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old, task_id))
            await d.commit()

    async def test_expires_stale_orientation_wait(self):
        await self._seed_wait("old1", "orientation", 8)
        await self._seed_wait("fresh1", "orientation", 1)
        await self._seed_wait("old2", "staged", 8)
        n = await db.expire_stale_orientation_waits(self.db_path, 7)
        self.assertEqual(n, 1)
        self.assertEqual((await db.get_task(self.db_path, "old1"))["status"], "interrupted")
        self.assertEqual((await db.get_task(self.db_path, "fresh1"))["status"], "waiting_input")
        self.assertEqual((await db.get_task(self.db_path, "old2"))["status"], "waiting_input")

    async def test_disabled_when_zero(self):
        await self._seed_wait("old3", "orientation", 30)
        self.assertEqual(await db.expire_stale_orientation_waits(self.db_path, 0), 0)
        self.assertEqual((await db.get_task(self.db_path, "old3"))["status"], "waiting_input")
