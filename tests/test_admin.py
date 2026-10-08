"""管理员 API 测试：鉴权、删除任务、日志。"""
import unittest
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

from app import auth, db
from app.config import Settings


def make_settings(tmpdir: str, admin_users=None) -> Settings:
    s = Settings()
    s.data_dir = str(Path(tmpdir) / "data")
    s.auth.admin_users = admin_users or ["boss"]
    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    return s


async def init_test_db(settings: Settings):
    await db.init_db(settings.db_path)


class AdminApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from fastapi import FastAPI
        from app import admin_api
        import app.api as api_mod

        self.settings = make_settings(self.tmp.name)
        # admin_api 用 from .api import get_settings，直接 patch 它的引用
        admin_api.get_settings = lambda: self.settings

        async def fake_session():
            return {"openid": self._openid}

        app = FastAPI()
        app.include_router(admin_api.router)
        app.dependency_overrides[api_mod.require_session] = fake_session
        self.client = TestClient(app, raise_server_exceptions=False)
        self._openid = "web:boss"

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_admin_forbidden(self):
        self._openid = "web:kid"
        r = self.client.get("/admin/tasks")
        self.assertEqual(r.status_code, 403)

    def test_admin_can_list_tasks(self):
        import asyncio
        asyncio.get_event_loop().run_until_complete(init_test_db(self.settings))
        r = self.client.get("/admin/tasks")
        self.assertEqual(r.status_code, 200)
        self.assertIn("tasks", r.json())

    def test_is_admin_helper(self):
        self.assertTrue(auth.is_admin("web:boss", ["boss"]))
        self.assertFalse(auth.is_admin("web:kid", ["boss"]))
        self.assertFalse(auth.is_admin("wx123", ["boss"]))
        self.assertFalse(auth.is_admin("web:boss", []))

    def test_log_path_traversal_rejected(self):
        r = self.client.get("/admin/logs/tail?file=../../etc/passwd")
        self.assertIn(r.status_code, (400, 404))

    def test_tail_missing_log(self):
        r = self.client.get("/admin/logs/tail?file=thinking.log")
        # 日志文件不存在时 404（data/logs 未建）
        self.assertEqual(r.status_code, 404)

    def test_tail_log_works(self):
        logdir = Path(self.settings.data_dir) / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        (logdir / "thinking.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
        r = self.client.get("/admin/logs/tail?file=thinking.log&lines=2")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["lines"], ["line2", "line3"])

    def test_list_logs(self):
        r = self.client.get("/admin/logs")
        self.assertEqual(r.status_code, 200)
        names = [l["name"] for l in r.json()["logs"]]
        self.assertIn("thinking.log", names)
        self.assertIn("grader.log", names)


if __name__ == "__main__":
    unittest.main()
