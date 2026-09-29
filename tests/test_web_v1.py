"""网页版 v1 测试：白名单登录、新 IA 接口、两年保留、异议标记、人工记入台账。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import aiosqlite
import httpx
from fastapi import FastAPI

from app import api, auth, db
from app.auth import hash_password, web_openid
from app.config import Settings
from app.main import create_app


def make_settings(tmp: str, **overrides) -> Settings:
    data = {
        "engine": {"mode": "hermes"},
        "hermes": {"base_url": "", "api_key": ""},
        "data_dir": str(Path(tmp) / "data"),
        "workspace": {"dir": str(Path(tmp) / "ws"), "init_readme": False},
        "web": {
            "enabled": True,
            "title": "学习助手",
            "users": [
                {"username": "kid1", "display_name": "老大",
                 "password_hash": hash_password("pw-one")},
                {"username": "kid2", "display_name": "老二",
                 "password_hash": hash_password("pw-two")},
            ],
        },
    }
    data.update(overrides)
    return Settings.model_validate(data)


def make_app(settings: Settings) -> FastAPI:
    api.settings = settings
    app = FastAPI()
    app.include_router(api.router)
    return app


async def seed_task(db_path: str, openid: str, task_id: str, created_at: float,
                    result: dict | None = None) -> None:
    """直接写库造任务+执行轮次（绕过配额 machinery，只测统计口径）。"""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO tasks(id, openid, subject, grade_level, image_path, status,
                                 task_type, input_text, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (task_id, openid, "数学", "", "", "done", "grading", "", created_at, created_at),
        )
        await conn.execute(
            """INSERT INTO task_runs(id, task_id, run_no, kind, input_text, status,
                                     hermes_session_id, created_at, finished_at, result_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (f"run-{task_id}", task_id, 1, "grade", "", "finished", "",
             created_at, created_at + 5, json.dumps(result or {})),
        )
        await conn.commit()


def grading_result(statuses: list[str]) -> dict:
    return {"questions": [
        {"id": f"q{i}", "uid": f"u{i}", "no": str(i + 1), "status": st}
        for i, st in enumerate(statuses)
    ]}


async def seed_mistake(db_path: str, openid: str, uid: str, created_at: float,
                       last_event_at: float = 0.0, **overrides) -> int:
    row = {
        "openid": openid, "task_id": "t1", "question_no": "3",
        "knowledge_point": "一元一次方程", "note": "", "created_at": created_at,
        "subject": "数学", "source": "", "page": "", "question_uid": uid,
        "stem": "解方程", "student_answer": "x=5", "correct_answer": "x=4",
        "error_rule": "移项未变号", "status": "wrong",
        "remediation_state": "pending_correction", "last_event_at": last_event_at,
        "archive_path": "",
    }
    row.update(overrides)
    cols = ",".join(row.keys())
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            f"INSERT INTO mistakes({cols}) VALUES({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
        await conn.commit()
        return cur.lastrowid


async def seed_event(db_path: str, openid: str, uid: str) -> str:
    return await db.add_question_event(db_path, openid, {
        "question_uid": uid, "subject": "数学", "event_type": "retest",
        "result": "corrected", "occurred_date": "2024-01-01",
        "student_answer": "", "note": "", "source_task_id": "t1", "archive_path": "",
    })


class WebV1Test(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        await db.init_db(self.settings.db_path)
        self.app = make_app(self.settings)
        api._web_failures.clear()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        api._web_failures.clear()
        self.tmp.cleanup()

    async def login(self, username: str, password: str):
        return await self.client.post(
            "/api/web/login", data={"username": username, "password": password})

    async def authed(self, username: str = "kid1", password: str = "pw-one"):
        res = await self.login(username, password)
        self.assertEqual(res.status_code, 200, res.text)
        token = res.json()["token"]
        return {"Authorization": f"Bearer {token}"}

    # ---------- 登录 ----------

    async def test_login_success(self):
        res = await self.login("kid1", "pw-one")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["openid"], web_openid("kid1"))
        self.assertEqual(body["username"], "kid1")
        self.assertEqual(body["display_name"], "老大")
        self.assertTrue(body["token"])

    async def test_login_wrong_password(self):
        res = await self.login("kid1", "nope")
        self.assertEqual(res.status_code, 401)
        self.assertIn("用户名或密码不正确", res.json()["detail"])

    async def test_login_ignores_surrounding_spaces(self):
        """生成脚本对密码做了 strip，校验侧必须一致，否则会永久登录失败。"""
        res = await self.login("kid1", "  pw-one  ")
        self.assertEqual(res.status_code, 200, res.text)

    async def test_login_unknown_user_same_message(self):
        res = await self.login("ghost", "whatever")
        self.assertEqual(res.status_code, 401)
        self.assertIn("用户名或密码不正确", res.json()["detail"])

    async def test_login_unconfigured(self):
        settings = make_settings(self.tmp.name, web={"enabled": True, "users": []})
        await db.init_db(settings.db_path)
        app = make_app(settings)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            res = await c.post("/api/web/login", data={"username": "kid1", "password": "pw-one"})
        self.assertEqual(res.status_code, 403)

    async def test_meta_marks_user_required_and_hides_users(self):
        res = await self.client.get("/api/web/meta")
        body = res.json()
        self.assertTrue(body["user_required"])
        self.assertTrue(body["configured"])
        self.assertNotIn("kid1", res.text)

    # ---------- 今日学习台 ----------

    async def test_overview_empty(self):
        h = await self.authed()
        res = await self.client.get("/api/web/overview", headers=h)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["todos"]["pending_correction"], 0)
        self.assertIsNone(body["week_accuracy"])  # 无数据不断言 100%
        self.assertEqual(body["top_causes"], [])

    async def test_overview_with_data(self):
        h = await self.authed()
        openid = web_openid("kid1")
        now = time.time()
        await seed_task(self.settings.db_path, openid, "t-cur", now - 86400,
                        grading_result(["correct", "correct", "wrong", "uncertain"]))
        await seed_task(self.settings.db_path, openid, "t-prev", now - 10 * 86400,
                        grading_result(["correct", "wrong", "wrong", "wrong"]))
        await seed_mistake(self.settings.db_path, openid, "m1", now - 86400,
                           error_rule="移项未变号")
        await seed_mistake(self.settings.db_path, openid, "m2", now - 86400,
                           error_rule="移项未变号")
        await seed_mistake(self.settings.db_path, openid, "m3", now - 86400,
                           error_rule="计算粗心")
        res = await self.client.get("/api/web/overview", headers=h)
        body = res.json()
        acc = body["week_accuracy"]
        self.assertEqual(acc["checked"], 3)  # uncertain 不计入
        self.assertAlmostEqual(acc["rate"], 2 / 3, places=3)
        self.assertAlmostEqual(acc["delta"], 2 / 3 - 1 / 4, places=3)
        causes = body["top_causes"]
        self.assertEqual(causes[0]["cause"], "移项未变号")
        self.assertEqual(causes[0]["count"], 2)

    # ---------- 设置（去年级） ----------

    async def test_settings_has_no_grade(self):
        h = await self.authed()
        res = await self.client.put("/api/settings", headers=h, json={
            "subjects": ["数学", "英语"], "term_start_date": "2026-09-01",
            "grade_level": "七年级",  # 旧字段应被忽略
        })
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertNotIn("grade_level", body)
        self.assertEqual(body["subjects"], ["数学", "英语"])
        res2 = await self.client.get("/api/settings", headers=h)
        self.assertNotIn("grade_level", res2.json())

    # ---------- 异议标记 ----------

    async def test_disputed_event_withdraws_from_ledger(self):
        """用户点"我觉得判错了"：记异议事件，条目置为 withdrawn 从台账撤回。"""
        h = await self.authed()
        openid = web_openid("kid1")
        mid = await seed_mistake(self.settings.db_path, openid, "dq1", time.time())
        res = await self.client.post(f"/api/ledger/{mid}/events", headers=h, json={
            "result": "disputed", "note": "学生认为判分有误"})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["result"], "disputed")
        self.assertEqual(res.json()["remediation_state"], "withdrawn")
        row = await db.get_ledger_entry(self.settings.db_path, openid, mid)
        self.assertEqual(row["remediation_state"], "withdrawn")
        # 默认台账视图不再含该条目
        rows = await db.list_ledger(self.settings.db_path, openid)
        self.assertEqual(rows, [])

    # ---------- 两年保留（只清错题，不动任务/附件） ----------

    async def test_purge_expired_removes_only_stale_mistakes(self):
        openid = web_openid("kid1")
        old = time.time() - 800 * 86400  # 超过 2 年
        mid = await seed_mistake(self.settings.db_path, openid, "old1", old)
        await seed_event(self.settings.db_path, openid, "old1")
        await seed_task(self.settings.db_path, openid, "t-old", old,
                        grading_result(["wrong"]))
        removed = await db.purge_expired(self.settings.db_path, openid, 730)
        self.assertEqual(removed["mistakes"], 1)
        self.assertEqual(removed["question_events"], 1)
        # 任务与执行轮次不受影响
        task = await db.get_task(self.settings.db_path, "t-old")
        self.assertIsNotNone(task)
        run = await db.get_run(self.settings.db_path, "run-t-old")
        self.assertIsNotNone(run)
        # 错题确实没了
        self.assertIsNone(await db.get_ledger_entry(self.settings.db_path, openid, mid))

    async def test_purge_keeps_recent_and_active(self):
        openid = web_openid("kid1")
        old = time.time() - 800 * 86400
        now = time.time()
        recent = await seed_mistake(self.settings.db_path, openid, "new1", old,
                                   last_event_at=now)  # 很久以前创建，但最近有事件
        fresh = await seed_mistake(self.settings.db_path, openid, "new2", now)
        removed = await db.purge_expired(self.settings.db_path, openid, 730)
        self.assertEqual(removed["mistakes"], 0)
        self.assertIsNotNone(await db.get_ledger_entry(self.settings.db_path, openid, recent))
        self.assertIsNotNone(await db.get_ledger_entry(self.settings.db_path, openid, fresh))

    async def test_purge_other_user_untouched(self):
        me, other = web_openid("kid1"), web_openid("kid2")
        old = time.time() - 800 * 86400
        other_mid = await seed_mistake(self.settings.db_path, other, "o1", old)
        removed = await db.purge_expired(self.settings.db_path, me, 730)
        self.assertEqual(removed["mistakes"], 0)
        self.assertIsNotNone(await db.get_ledger_entry(self.settings.db_path, other, other_mid))

    # ---------- 人工记入台账 ----------

    async def test_manual_ledger_entry(self):
        h = await self.authed()
        openid = web_openid("kid1")
        payload = {"subject": "数学", "question_no": "5", "stem": "解方程 3x=12",
                   "correct_answer": "x=4", "question_uid": "manual-u1",
                   "source_task_id": "t1"}
        res = await self.client.post("/api/ledger/manual", headers=h, json=payload)
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.json()["created"])
        res2 = await self.client.post("/api/ledger/manual", headers=h, json=payload)
        self.assertFalse(res2.json()["created"])  # 去重，不产生重复条目
        rows = await db.list_ledger(self.settings.db_path, openid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["remediation_state"], "pending_correction")

    # ---------- 账号隔离 ----------

    async def test_accounts_are_isolated(self):
        h1 = await self.authed("kid1", "pw-one")
        h2 = await self.authed("kid2", "pw-two")
        await seed_mistake(self.settings.db_path, web_openid("kid1"), "k1", time.time())
        res = await self.client.get("/api/ledger", headers=h2)
        self.assertEqual(res.json()["entries"], [])
        res = await self.client.get("/api/ledger", headers=h1)
        self.assertEqual(len(res.json()["entries"]), 1)

    async def test_list_user_openids(self):
        me, other = web_openid("kid1"), web_openid("kid2")
        await seed_mistake(self.settings.db_path, me, "k1", time.time())
        await seed_task(self.settings.db_path, other, "t2", time.time())
        openids = await db.list_user_openids(self.settings.db_path)
        self.assertIn(me, openids)
        self.assertIn(other, openids)


class WebStaticCacheTest(unittest.IsolatedAsyncioTestCase):
    """网页前端"部署后立即生效"：静态资源回源校验 + 版本探针。

    `web/` 三个文件既没有版本号也没有指纹，一旦浏览器复用旧 app.js，
    单页应用又只改 hash 不整页刷新，用户就会一直看到改动前的界面。
    这里锁住两条防线：/web 静态资源必须每次回源校验（no-cache + ETag），
    /api/web/meta 必须带上当前前端版本号且自身不可缓存。
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.web_dir = Path(self.tmp.name) / "web"
        self.web_dir.mkdir()
        self.write_assets("console.log('v1');")
        # settings.web_dir 读的是环境变量 WEB_DIR，用它把静态目录指到临时目录，
        # 避免测试依赖（或污染）仓库里真实的 web/
        self._env = mock.patch.dict(os.environ, {"WEB_DIR": str(self.web_dir)})
        self._env.start()
        self.settings = make_settings(self.tmp.name)
        self.app = create_app(self.settings)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self._env.stop()
        self.tmp.cleanup()

    def write_assets(self, js: str) -> None:
        (self.web_dir / "app.js").write_text(js, encoding="utf-8")
        (self.web_dir / "styles.css").write_text("body { margin: 0; }\n", encoding="utf-8")
        (self.web_dir / "index.html").write_text(
            "<!doctype html><script src=\"app.js\"></script>", encoding="utf-8")

    async def test_static_assets_are_not_silently_cached(self):
        res = await self.client.get("/web/app.js")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["cache-control"], "no-cache, must-revalidate")
        # 保留 ETag / Last-Modified，内容没变时仍走 304，不重复传正文
        self.assertTrue(res.headers.get("etag"))

        res = await self.client.get("/web/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["cache-control"], "no-cache, must-revalidate")

    async def test_revalidate_returns_304_when_unchanged(self):
        first = await self.client.get("/web/app.js")
        res = await self.client.get(
            "/web/app.js", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(res.status_code, 304)

    async def test_meta_carries_version_and_is_not_cached(self):
        res = await self.client.get("/api/web/meta")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["cache-control"], "no-store")
        version = res.json()["asset_version"]
        self.assertTrue(version)
        # 版本号必须随前端内容变化，否则探针永远发现不了新前端
        self.write_assets("console.log('v2 with longer source');")
        res2 = await self.client.get("/api/web/meta")
        self.assertNotEqual(res2.json()["asset_version"], version)

    async def test_meta_version_is_empty_when_assets_missing(self):
        (self.web_dir / "app.js").unlink()
        res = await self.client.get("/api/web/meta")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["asset_version"], "")


if __name__ == "__main__":
    unittest.main()
