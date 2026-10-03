import asyncio
import base64
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from fastapi import FastAPI
import httpx
from PIL import Image

from app import api, db, image_prep, orientation, orientation_tasks, providers, staged, tasks, thinking, workspace
from app.schemas import OrientationConfirm
from app.providers import GradeOutcome
from scripts.split_thinking import split_log
from tests.test_tasks import make_settings, seed_user, FakeClient
from tests.mock_hermes import LEARNING_RESULT


def picture():
    buf = io.BytesIO()
    Image.new("RGB", (160, 120), "white").save(buf, "JPEG")
    return buf.getvalue()


LOW = {"status": "uncertain", "rotation": 270, "confidence": 0.75, "error": "low"}


class VisualProvider:
    def __init__(self, response, calls):
        self.response, self.calls = response, calls

    async def grade_multi(self, images, system, user, max_tokens):
        self.calls.append(images)
        if isinstance(self.response, Exception):
            raise self.response
        return GradeOutcome(text=json.dumps(self.response), provider="v", model="vision",
                            input_tokens=100, output_tokens=10)


class OrientationTest(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return make_settings("/tmp", llm={"default_provider": "v", "providers": {
            "v": {"base_url": "http://fake", "model": "vision", "price_input_per_1m": 1.0}}})

    async def test_visual_success_rotation_and_metadata(self):
        calls, updates = [], []
        async def record(data): updates.append(copy.deepcopy(data))
        factory = lambda n, c: VisualProvider({"rotation": 90, "certain": True, "readable": True, "cue": "标题"}, calls)
        with patch.object(image_prep, "_detect_text_rotation", return_value=LOW):
            images, data = await orientation.prepare_pages([(picture(), "image/jpeg")], self.settings(), ["v"], on_update=record, provider_factory=factory)
        self.assertEqual(len(calls), 1)
        self.assertEqual(Image.open(io.BytesIO(images[0][0])).size, (1536, 2048))
        self.assertEqual(data["pages"][0]["rotation"], 90)
        self.assertEqual(data["pages"][0]["source"], "visual")
        self.assertTrue(updates[0]["pages"][0]["visual_attempted"])
        self.assertFalse(updates[0]["pages"][0]["confirmed"])

    async def test_uncertain_or_failed_visual_waits_once_then_manual_resume(self):
        for response in [{"rotation": 270, "certain": False, "readable": True, "cue": "不确定"}, providers.ProviderError("unavailable"), {"rotation": 45}]:
            calls, saved = [], {}
            async def record(data): saved.update(copy.deepcopy(data))
            factory = lambda n, c: VisualProvider(response, calls)
            raw = picture()
            with patch.object(image_prep, "_detect_text_rotation", return_value=LOW):
                for _ in range(2):
                    with self.assertRaises(orientation.ConfirmationRequired):
                        await orientation.prepare_pages([(raw, "image/jpeg")], self.settings(), ["v"], saved, record, factory)
                self.assertEqual(len(calls), 1)
                saved["pages"][0].update(confirmed=True, rotation=90, source="manual")
                _, result = await orientation.prepare_pages([(raw, "image/jpeg")], self.settings(), ["v"], saved, record, factory)
                self.assertEqual(result["pages"][0]["source"], "manual")
                self.assertEqual(len(calls), 1)

    async def test_text_only_provider_is_not_used_for_orientation(self):
        settings = self.settings()
        settings.llm.providers["v"].supports_vision = False
        factory = lambda *a: self.fail("text-only provider must not be called")
        with patch.object(image_prep, "_detect_text_rotation", return_value=LOW):
            with self.assertRaises(orientation.ConfirmationRequired):
                await orientation.prepare_pages([(picture(), "image/jpeg")], settings, ["v"], provider_factory=factory)


class OrientationApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name, llm={"default_provider": "v", "providers": {
            "v": {"base_url": "http://fake", "model": "vision", "api_key": "test"}}},
            staged_grading={"orientation_visual_fallback": False})
        await db.init_db(self.settings.db_path)
        await seed_user(self.settings, "u1")
        asset = workspace.store_asset(self.settings, "u1", picture(), "page.jpg")
        await db.create_asset(self.settings.db_path, asset)
        self.asset = asset
        created = await tasks.create_study_task(self.settings, "u1", {"asset_ids": [asset["id"]]})
        self.task_id = created["task_id"]
        self.runner = tasks.TaskRunner(self.settings, FakeClient())
        task = await db.claim_next_task(self.settings.db_path, "test")
        with patch.object(image_prep, "_detect_text_rotation", return_value=LOW):
            await self.runner.execute(task)
        self.run = (await db.list_runs(self.settings.db_path, self.task_id))[-1]
        self.old_api_settings = api.settings
        api.settings = self.settings
        self.app = FastAPI()
        self.app.include_router(api.router)
        self.app.dependency_overrides[api.require_session] = lambda: {"openid": "u1"}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        api.settings = self.old_api_settings
        self.tmp.cleanup()

    async def test_preview_confirm_and_resume_same_run(self):
        url = f"/api/tasks/{self.task_id}"
        view = (await self.client.get(url)).json()
        self.assertEqual(view["status"], "waiting_input")
        self.assertIsNotNone(view["orientation"])
        self.assertIsNone(view["result"])
        preview = await self.client.get(url + "/orientation/1", params={"run_id": self.run["id"]})
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.json()["preview"].startswith("data:image/jpeg;base64,"))
        body = {"run_id": self.run["id"], "rotations": [{"page": 1, "rotation": 90}]}
        self.assertEqual((await self.client.post(url + "/orientation", json=body)).status_code, 200)
        self.assertEqual((await self.client.post(url + "/orientation", json=body)).status_code, 409)
        self.assertEqual(len(await db.list_runs(self.settings.db_path, self.task_id)), 1)
        task = await db.claim_next_task(self.settings.db_path, "test")
        outcome = staged.StagedOutcome(result=copy.deepcopy(LEARNING_RESULT), model="fake")
        with patch.object(staged, "grade_staged", new=AsyncMock(return_value=outcome)) as grade:
            await self.runner.execute(task)
        self.assertTrue(grade.call_args.kwargs["images_prepared"])
        images = grade.call_args.args[0]
        self.assertEqual(Image.open(io.BytesIO(images[0][0])).size, (1536, 2048))
        self.assertIn((await db.get_task(self.settings.db_path, self.task_id))["status"], ["done", "waiting_input"])

    async def test_owner_stale_run_and_invalid_angles(self):
        url = f"/api/tasks/{self.task_id}/orientation"
        body = {"run_id": self.run["id"], "rotations": [{"page": 1, "rotation": 90}]}
        for rotations in [[{"page": 1, "rotation": 45}], [{"page": 1, "rotation": 90.5}], []]:
            self.assertEqual((await self.client.post(url, json={**body, "rotations": rotations})).status_code, 422)
        for rotations in [[{"page": 2, "rotation": 90}], [{"page": 1, "rotation": 90}] * 2]:
            self.assertEqual((await self.client.post(url, json={**body, "rotations": rotations})).status_code, 400)
        self.assertEqual((await self.client.post(url, json={**body, "run_id": "stale"})).status_code, 409)
        self.app.dependency_overrides[api.require_session] = lambda: {"openid": "u2"}
        self.assertEqual((await self.client.post(url, json=body)).status_code, 404)
        self.assertEqual((await self.client.get(url + "/1", params={"run_id": self.run["id"]})).status_code, 404)


class ContextLogTest(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_task_logs_and_split_stage_only_records(self):
        with self.assertLogs(thinking.log, level="INFO") as logs, patch.dict("os.environ", {"SA_DEBUG_THINKING": "1"}):
            async def emit(task):
                with thinking.task_context(task, 1):
                    await asyncio.sleep(0)
                    thinking.log_event("orientation", {"page": 1, "marker": task})
            await asyncio.gather(emit("taskA"), emit("taskB"))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "thinking.log"
            # Handler normally prefixes timestamps; records here use the captured message only.
            source.write_text("\n".join(logs.output).replace("INFO:studyassistant.thinking:", "") + "\n")
            result = split_log(source, Path(tmp) / "split")
            self.assertEqual(result, (2, 0, 0))
            for path in (Path(tmp) / "split").iterdir():
                text = path.read_text()
                self.assertFalse("taskA" in text and "taskB" in text)
