"""Hermes 适配层测试：协议解析、错误分类、不自动重试、结果协议校验。"""
from __future__ import annotations

import json
import unittest

import httpx

from app.config import HermesConfig
from app.hermes import (HermesAuthError, HermesClient, HermesNotConfigured,
                        HermesResultInvalid, HermesUnavailable, HermesUncertain,
                        build_messages, extract_result_json, validate_result)
from tests.mock_hermes import LEARNING_RESULT, MockHermes


def make_cfg(**kwargs) -> HermesConfig:
    base = {"base_url": "http://hermes.local", "api_key": "test-key"}
    base.update(kwargs)
    return HermesConfig(**base)


class ReadinessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock = MockHermes()
        self.mock.install()
        self.addCleanup(self.mock.uninstall)

    async def test_ready_when_skill_installed(self):
        client = HermesClient(make_cfg())
        state = (await client.readiness()).as_dict()
        self.assertEqual(state["state"], "ready")
        self.assertTrue(state["skill_installed"])
        await client.aclose()

    async def test_skill_missing(self):
        self.mock.uninstall()
        self.mock = MockHermes(skills=["other-skill"])
        self.mock.install()
        client = HermesClient(make_cfg())
        state = (await client.readiness()).as_dict()
        self.assertEqual(state["state"], "skill_missing")
        await client.aclose()

    async def test_not_configured(self):
        client = HermesClient(make_cfg(base_url="", api_key=""))
        state = (await client.readiness()).as_dict()
        self.assertEqual(state["state"], "not_configured")
        await client.aclose()

    async def test_unreachable(self):
        self.mock.uninstall()
        self.mock = MockHermes(fail_mode="unreachable")
        self.mock.install()
        client = HermesClient(make_cfg())
        state = (await client.readiness()).as_dict()
        self.assertEqual(state["state"], "unreachable")
        await client.aclose()

    async def test_readiness_is_cached(self):
        client = HermesClient(make_cfg())
        await client.readiness()
        calls = len([r for r in self.mock.requests if r.url.path == "/v1/skills"])
        await client.readiness()
        self.assertEqual(calls, 1, "就绪检查应命中缓存")
        await client.readiness(force=True)
        self.assertEqual(len([r for r in self.mock.requests if r.url.path == "/v1/skills"]), 2)
        await client.aclose()


class RunTaskTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock = MockHermes()
        self.mock.install()
        self.addCleanup(self.mock.uninstall)

    async def test_success_returns_validated_result(self):
        client = HermesClient(make_cfg())
        payload = await client.run_task([{"role": "user", "content": "x"}], "sess-1")
        self.assertEqual(payload["result"]["schema_version"], 2)
        self.assertEqual(payload["result"]["questions"][0]["status"], "wrong")
        self.assertEqual(payload["model"], "hermes-agent")
        sent = [r for r in self.mock.requests if r.url.path == "/v1/chat/completions"][0]
        self.assertEqual(sent.headers.get("x-hermes-session-id"), "sess-1")
        await client.aclose()

    async def test_auth_error_is_certain_not_executed(self):
        self.mock.fail_mode = "auth"
        client = HermesClient(make_cfg())
        with self.assertRaises(HermesAuthError) as ctx:
            await client.run_task([{"role": "user", "content": "x"}], "s")
        self.assertTrue(ctx.exception.certain_not_executed)
        await client.aclose()

    async def test_timeout_is_uncertain_and_not_retried(self):
        self.mock.fail_mode = "timeout"
        client = HermesClient(make_cfg())
        with self.assertRaises(HermesUncertain) as ctx:
            await client.run_task([{"role": "user", "content": "x"}], "s")
        self.assertFalse(ctx.exception.certain_not_executed,
                         "结果未确认时不允许自动重发")
        self.assertEqual(self.mock.send_count, 1)
        await client.aclose()

    async def test_server_error_is_unavailable(self):
        self.mock.fail_mode = "server_error"
        client = HermesClient(make_cfg())
        with self.assertRaises(HermesUnavailable):
            await client.run_task([{"role": "user", "content": "x"}], "s")
        await client.aclose()

    async def test_output_without_json_is_invalid(self):
        self.mock.fail_mode = "bad_json"
        client = HermesClient(make_cfg())
        with self.assertRaises(HermesResultInvalid):
            await client.run_task([{"role": "user", "content": "x"}], "s")
        await client.aclose()

    async def test_vague_error_rule_is_rejected(self):
        self.mock.fail_mode = "invalid_result"
        client = HermesClient(make_cfg())
        with self.assertRaises(HermesResultInvalid) as ctx:
            await client.run_task([{"role": "user", "content": "x"}], "s")
        self.assertIn("粗心", str(ctx.exception))
        await client.aclose()

    async def test_not_configured_client_never_calls(self):
        client = HermesClient(make_cfg(base_url="", api_key=""))
        with self.assertRaises(HermesNotConfigured):
            await client.run_task([{"role": "user", "content": "x"}], "s")
        self.assertEqual(self.mock.send_count, 0)
        await client.aclose()


class ResultContractTest(unittest.TestCase):
    def test_extract_from_json_block(self):
        text = "说明\n```json\n{\"a\": 1}\n```\n结尾"
        self.assertEqual(extract_result_json(text), {"a": 1})

    def test_extract_bare_json(self):
        payload = {"schema_version": 2, "task_type": "qa", "questions": []}
        self.assertEqual(extract_result_json(json.dumps(payload)), payload)

    def test_extract_without_json_raises(self):
        with self.assertRaises(HermesResultInvalid):
            extract_result_json("没有 JSON")

    def test_duplicate_question_ids_rejected(self):
        broken = dict(LEARNING_RESULT)
        broken["questions"] = [dict(LEARNING_RESULT["questions"][0]),
                              dict(LEARNING_RESULT["questions"][0])]
        broken["overview"] = {}
        with self.assertRaises(HermesResultInvalid):
            validate_result(broken)

    def test_overview_mismatch_rejected(self):
        broken = dict(LEARNING_RESULT)
        broken["overview"] = {"wrong": 5}
        with self.assertRaises(HermesResultInvalid):
            validate_result(broken)

    def test_unanswered_cannot_be_kept_wrong(self):
        broken = dict(LEARNING_RESULT)
        questions = [dict(q) for q in broken["questions"]]
        questions[1] = dict(questions[1], final_decision="kept_wrong")
        broken["questions"] = questions
        broken["overview"] = {}
        with self.assertRaises(HermesResultInvalid):
            validate_result(broken)

    def test_valid_result_passes(self):
        data = validate_result(LEARNING_RESULT)
        self.assertEqual(data["overview"]["wrong"], 1)
        self.assertEqual(data["overview"]["unanswered"], 1)


class MessageTest(unittest.TestCase):
    def _settings(self):
        from app.config import Settings

        return Settings.model_validate({
            "engine": {"mode": "hermes"},
            "hermes": {"base_url": "http://127.0.0.1:8642", "api_key": "secret-key"},
            "workspace": {"dir": "/tmp/ws"},
        })

    def test_message_has_no_secret_and_has_skill(self):
        settings = self._settings()
        task = {"id": "t1", "task_type": "grading", "subject": "数学",
                "grade_level": "七年级", "input_text": "只看第 3 题"}
        run = {"run_no": 1, "kind": "initial", "output_dir": "/tmp/out"}
        messages = build_messages(settings, task, run, [])
        text = json.dumps(messages, ensure_ascii=False)
        self.assertIn("leo-study-assistant", text)
        self.assertIn("/tmp/ws", text)
        self.assertNotIn("secret-key", text)
        self.assertNotIn("Authorization", text)

    def test_images_are_inlined_not_as_paths(self):
        settings = self._settings()
        task = {"id": "t1", "task_type": "grading", "input_text": ""}
        run = {"run_no": 1, "kind": "initial", "output_dir": "/tmp/out"}
        messages = build_messages(settings, task, run, [
            {"id": "a1", "data_url": "data:image/jpeg;base64,AAAA"},
        ])
        content = messages[1]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg"))


if __name__ == "__main__":
    unittest.main()
