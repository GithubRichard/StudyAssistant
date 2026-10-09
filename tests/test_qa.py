"""问问题聊天 API 测试：会话 CRUD、消息发送（SSE）、openid 隔离。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, migrations
from app import qa_api
import app.api as api_mod
from app.config import Settings


def make_settings(tmpdir: str) -> Settings:
    s = Settings()
    s.data_dir = str(Path(tmpdir) / "data")
    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    # 配一个假 provider（测试里 mock 掉实际调用）
    from app.config import ProviderConfig
    s.llm.providers = {
        "fake": ProviderConfig(api_key="x", base_url="http://127.0.0.1:1",
                               model="m", enabled=True),
    }
    s.llm.default_provider = "fake"
    s.llm.fallback_order = []
    return s


class FakeProvider:
    name = "fake"

    def __init__(self, chunks=None, exc=None):
        self._chunks = chunks or ["你好", "呀"]
        self.exc = exc

    async def stream_text(self, system_prompt, user_prompt, max_tokens=4000,
                          timeout=None):
        if self.exc:
            raise self.exc
        for c in self._chunks:
            yield c

    async def grade_multi(self, images, system_prompt, user_prompt,
                          max_tokens=8000):
        from types import SimpleNamespace
        if self.exc:
            raise self.exc
        return SimpleNamespace(text="".join(self._chunks))


class QaApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        qa_api.get_settings = lambda: self.settings
        # mock provider_chain / make_provider
        self._fake = FakeProvider()
        qa_api.provider_chain = lambda s: ["fake"]
        qa_api.make_provider = lambda name, cfg: self._fake

        async def fake_session():
            return {"openid": self._openid}

        app = FastAPI()
        app.include_router(qa_api.router)
        app.dependency_overrides[api_mod.require_session] = fake_session
        self.client = TestClient(app, raise_server_exceptions=False)
        self._openid = "web:kid1"
        asyncio.get_event_loop().run_until_complete(
            db.init_db(self.settings.db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_v7_tables(self):
        async def check():
            return await migrations.current_version(self.settings.db_path)
        v = asyncio.get_event_loop().run_until_complete(check())
        self.assertGreaterEqual(v, 7)

    def test_session_crud(self):
        r = self.client.post("/api/qa/sessions", json={})
        self.assertEqual(r.status_code, 200)
        sid = r.json()["id"]
        # 列表
        r = self.client.get("/api/qa/sessions")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["sessions"]), 1)
        # 消息列表（空）
        r = self.client.get(f"/api/qa/sessions/{sid}/messages")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["messages"], [])
        # 删除
        r = self.client.delete(f"/api/qa/sessions/{sid}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/qa/sessions")
        self.assertEqual(r.json()["sessions"], [])

    def test_send_message_sse(self):
        sid = self.client.post("/api/qa/sessions", json={}).json()["id"]
        with self.client.stream("POST", f"/api/qa/sessions/{sid}/messages",
                                json={"content": "勾股定理是什么"}) as r:
            self.assertEqual(r.status_code, 200)
            events = []
            for line in r.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        kinds = [e["t"] for e in events]
        self.assertIn("delta", kinds)
        self.assertIn("done", kinds)
        text = "".join(e["text"] for e in events if e["t"] == "delta")
        self.assertEqual(text, "你好呀")
        # 落库：user + assistant
        r = self.client.get(f"/api/qa/sessions/{sid}/messages")
        msgs = r.json()["messages"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["content"], "你好呀")
        # 首条消息自动生成标题
        r = self.client.get("/api/qa/sessions")
        self.assertTrue(r.json()["sessions"][0]["title"].startswith("勾股定理"))

    def test_send_empty_rejected(self):
        sid = self.client.post("/api/qa/sessions", json={}).json()["id"]
        r = self.client.post(f"/api/qa/sessions/{sid}/messages",
                             json={"content": "  "})
        self.assertEqual(r.status_code, 400)

    def test_provider_error_sse(self):
        self._fake.exc = RuntimeError("boom")
        sid = self.client.post("/api/qa/sessions", json={}).json()["id"]
        with self.client.stream("POST", f"/api/qa/sessions/{sid}/messages",
                                json={"content": "hi"}) as r:
            events = [json.loads(l[5:].strip()) for l in r.iter_lines()
                      if l.startswith("data:")]
        self.assertTrue(any(e["t"] == "error" for e in events))

    def test_openid_isolation(self):
        sid = self.client.post("/api/qa/sessions", json={}).json()["id"]
        # 换用户：看不到、打不开、删不掉
        self._openid = "web:kid2"
        r = self.client.get("/api/qa/sessions")
        self.assertEqual(r.json()["sessions"], [])
        r = self.client.get(f"/api/qa/sessions/{sid}/messages")
        self.assertEqual(r.status_code, 404)
        r = self.client.delete(f"/api/qa/sessions/{sid}")
        self.assertEqual(r.status_code, 404)
        r = self.client.post(f"/api/qa/sessions/{sid}/messages",
                             json={"content": "hi"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
