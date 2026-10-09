"""绘图→保存→任务读取回归；所有模型调用均为离线替身。"""
import json
import tempfile
import time
import unittest
from unittest import mock

import httpx

from app import api, auth, db, diagram, schemas, tasks
from tests.test_diagram import RECT_JSON, SequenceProvider, outcome
from tests.test_web_v1 import make_app, make_settings, seed_task


def result(stem="正方形 ABCD，边长为 5"):
    return {"schema_version": 3, "task_type": "grading", "subject": "数学",
            "questions": [{"id": "q1", "no": "20(1)", "stem": stem, "status": "correct"}],
            "archive": {"action": "none"}}


class DiagramSchemaTest(unittest.TestCase):
    def test_preserves_svg_and_metadata_through_normalization(self):
        raw = result()
        svg = diagram.shapes_to_svg(json.loads(RECT_JSON))
        diagram.apply_diagram_result(raw["questions"][0], diagram.DiagramResult(
            "generated", svg=svg, provider="ds", attempts=2))
        for version in (2, 3):
            raw["schema_version"] = version
            normalized = schemas.normalize_result(json.dumps(raw))
            self.assertIsNotNone(normalized)
            self.assertEqual(normalized["questions"][0]["diagram_svg"], svg)
            self.assertEqual(normalized["questions"][0]["diagram"]["attempts"], 2)

    def test_old_result_without_svg_still_valid(self):
        normalized = schemas.normalize_result(result())
        self.assertEqual(normalized["questions"][0]["diagram_svg"], "")

    def test_unsafe_svg_removed_without_invalidating_grading(self):
        raw = result()
        raw["questions"][0]["diagram_svg"] = '<svg><script>alert(1)</script></svg>'
        normalized = schemas.normalize_result(raw)
        self.assertEqual(normalized["questions"][0]["diagram_svg"], "")
        self.assertEqual(normalized["questions"][0]["status"], "correct")


class DiagramApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name, llm={"default_provider": "ds",
            "providers": {"ds": {"base_url": "http://model.invalid", "model": "ds", "api_key": "fake"}}})
        self.previous_settings = api.settings
        await db.init_db(self.settings.db_path)
        self.app = make_app(self.settings)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://test")
        await db.get_or_create_user(self.settings.db_path, "u1", 10)
        session = await auth.issue_session(self.settings.db_path, "u1", [])
        self.headers = {"Authorization": "Bearer " + session["token"]}
        await seed_task(self.settings.db_path, "u1", "t1", time.time())
        await db.update_task(self.settings.db_path, "t1", result_json=json.dumps(result()))

    async def asyncTearDown(self):
        await self.client.aclose()
        api.settings = self.previous_settings
        self.tmp.cleanup()

    async def generate(self, provider):
        with mock.patch("app.providers.make_provider", return_value=provider):
            response = await self.client.post("/api/tasks/t1/diagrams", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def displayed_question(self):
        response = await self.client.get("/api/tasks/t1", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]["questions"][0]

    async def test_ds_retry_svg_persists_and_is_returned_without_ledger(self):
        provider = SequenceProvider([outcome(finish="length"), outcome(RECT_JSON)])
        response = await self.generate(provider)
        self.assertEqual(response["generated"], 1)
        self.assertEqual(response["failures"], [])
        q = await self.displayed_question()
        self.assertTrue(q["diagram_svg"].startswith("<svg"))
        self.assertEqual(q["diagram"]["attempts"], 2)
        self.assertEqual(await db.list_ledger_by_task(self.settings.db_path, "u1", "t1"), [])
        # 二次点击不再调用模型：旧 bug 会在归一化后丢图并重复生成。
        second = await self.generate(SequenceProvider([]))
        self.assertEqual(second["total"], 0)
        self.assertEqual((await self.displayed_question())["diagram_svg"], q["diagram_svg"])

    async def test_truncation_failure_is_reported_and_persists(self):
        response = await self.generate(SequenceProvider([
            outcome(finish="length"), outcome(finish="length")]))
        self.assertEqual(response["generated"], 0)
        self.assertIn("截断", response["failures"][0])
        self.assertEqual(response["results"][0]["reason"], "truncated")
        q = await self.displayed_question()
        self.assertEqual(q["diagram"]["status"], "failed")
        self.assertEqual(q["diagram_svg"], "")

    async def test_failed_task_diagram_can_be_retried(self):
        await self.generate(SequenceProvider([outcome("not JSON")]))
        response = await self.generate(SequenceProvider([outcome(RECT_JSON)]))
        self.assertEqual(response["generated"], 1)
        self.assertEqual((await self.displayed_question())["diagram"]["status"], "generated")

    async def test_no_geometry_is_skipped_not_failed(self):
        await db.update_task(self.settings.db_path, "t1",
                             result_json=json.dumps(result("解方程 2x+1=9")))
        response = await self.generate(SequenceProvider([outcome("[]")]))
        self.assertEqual(response["failures"], [])
        self.assertTrue(response["skipped"])
        self.assertIn("无需绘图", response["message"])
        self.assertEqual((await self.displayed_question())["diagram"]["status"], "skipped")

    async def test_missing_geometry_not_misreported_as_no_need(self):
        response = await self.generate(SequenceProvider([outcome("[]")]))
        self.assertEqual(response["results"][0]["reason"], "insufficient_geometry")
        self.assertTrue(response["failures"])
        self.assertNotIn("无需", response["message"])

    async def test_other_users_cannot_generate_diagram(self):
        await db.get_or_create_user(self.settings.db_path, "u2", 10)
        session = await auth.issue_session(self.settings.db_path, "u2", [])
        with mock.patch("app.providers.make_provider") as make:
            response = await self.client.post("/api/tasks/t1/diagrams",
                headers={"Authorization": "Bearer " + session["token"]})
        self.assertEqual(response.status_code, 404)
        make.assert_not_called()


class AutoDiagramTest(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_finish_keeps_diagram_after_task_read(self):
        from tests.test_tasks import FakeClient, make_settings as task_settings, run_executor, seed_user
        with tempfile.TemporaryDirectory() as tmp:
            settings = task_settings(tmp, llm={"default_provider": "ds", "providers": {
                "ds": {"base_url": "http://model.invalid", "model": "ds", "api_key": "fake"}}})
            await db.init_db(settings.db_path)
            await seed_user(settings, "u1")
            created = await tasks.create_study_task(settings, "u1", {
                "task_type": "qa", "subject": "数学", "text": "正方形怎么画", "asset_ids": []}, "")
            raw = result()
            raw["task_type"] = "qa"
            with mock.patch("app.tasks.make_provider", return_value=SequenceProvider([
                outcome(finish="length"), outcome(RECT_JSON)])):
                await run_executor(settings, FakeClient(result=raw))
            task = await db.get_task(settings.db_path, created["task_id"])
            self.assertEqual(task["status"], "done", task.get("error"))
            view = await tasks.build_task_view(settings, task)
            q = view["result"]["questions"][0]
            self.assertTrue(q["diagram_svg"].startswith("<svg"))
            self.assertEqual(q["diagram"]["attempts"], 2)
