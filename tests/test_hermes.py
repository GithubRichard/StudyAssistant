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

    async def test_health_5xx_does_not_mean_unreachable(self):
        """/health 返回 500 但网关可用时，不能误报「连不上」（真实遇到过的情形）。"""
        self.mock.uninstall()
        self.mock = MockHermes(fail_mode="health_500")
        self.mock.install()
        client = HermesClient(make_cfg())
        state = (await client.readiness()).as_dict()
        self.assertTrue(state["reachable"])
        self.assertEqual(state["state"], "ready")
        self.assertIn("500", state["detail"])
        await client.aclose()

    async def test_invalid_key_reports_auth_failed(self):
        self.mock.fail_mode = "auth"
        client = HermesClient(make_cfg())
        state = (await client.readiness()).as_dict()
        self.assertTrue(state["reachable"])
        self.assertEqual(state["state"], "auth_failed")
        self.assertFalse(state["auth_ok"])
        await client.aclose()

    async def test_skill_list_failure_does_not_mean_unreachable(self):
        """真实案例：/v1/skills 内部 500，但网关可用，不能误报成 unreachable。"""
        self.mock.uninstall()
        self.mock = MockHermes(fail_mode="skills_500")
        self.mock.install()
        client = HermesClient(make_cfg())
        state = (await client.readiness()).as_dict()
        self.assertEqual(state["state"], "skill_unknown")
        self.assertTrue(state["reachable"])
        self.assertIsNone(state["skill_installed"])
        self.assertIn("不可用", state["detail"])
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
        self.assertEqual(payload["result"]["schema_version"], 3)
        self.assertEqual(payload["result"]["questions"][0]["status"], "wrong")
        self.assertEqual(
            payload["result"]["questions"][0]["remediation"]["state"], "pending_correction")
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
        self.assertEqual(data["overview"]["remediation"]["pending_correction"], 1)
        self.assertIn("错误率", data["overview"]["error_rate_basis"])

    def test_v2_result_is_read_only_compatible(self):
        from app.schemas import normalize_result

        v2 = {
            "schema_version": 2,
            "task_type": "grading",
            "subject": "数学",
            "overview": {},
            "questions": [{
                "id": "q1", "no": "1", "status": "wrong", "correct_answer": "x=4",
                "error_rule": "移项未变号", "final_decision": "kept_wrong"}],
            "review_summary": {"state": "not_run"},
        }
        data = normalize_result(v2)
        self.assertIsNotNone(data)
        self.assertEqual(data["legacy_schema"], 2)
        self.assertEqual(data["questions"][0]["remediation"]["state"], "not_applicable")
        self.assertIn("未记录订正与复测状态", data["questions"][0]["remediation"]["note"])

    def test_v3_archive_must_match_task_type(self):
        broken = dict(LEARNING_RESULT)
        broken["task_type"] = "weekly_report"
        broken["archive"] = {"suggested_path": "数学/错题解析/2026-09-26.md",
                             "action": "append", "content_markdown": "x"}
        with self.assertRaises(HermesResultInvalid):
            validate_result(broken)

    def test_wrong_question_requires_remediation_state(self):
        broken = dict(LEARNING_RESULT)
        questions = [dict(q) for q in broken["questions"]]
        questions[0] = dict(questions[0], remediation={"state": "not_applicable"})
        broken["questions"] = questions
        with self.assertRaises(HermesResultInvalid):
            validate_result(broken)

    def test_null_text_fields_are_treated_as_missing(self):
        """模型写 null（如 remediation.updated_date=null）不应废掉整卷结果。"""
        data = dict(LEARNING_RESULT)
        questions = [dict(q) for q in data["questions"]]
        questions[1] = dict(questions[1], remediation={
            "state": "not_applicable", "updated_date": None,
            "linked_training": None, "note": None})
        questions[0] = dict(questions[0], steps=["2x=8", None])
        data["questions"] = questions
        result = validate_result(data)
        self.assertEqual(result["questions"][1]["remediation"]["updated_date"], "")
        self.assertEqual(result["questions"][1]["remediation"]["note"], "")
        self.assertEqual(result["questions"][0]["steps"], ["2x=8"])

    def test_null_does_not_excuse_missing_required_field(self):
        """null 只表示「未提供」：必填字段仍然要如实报缺失。"""
        broken = dict(LEARNING_RESULT)
        questions = [dict(q) for q in broken["questions"]]
        questions[0] = dict(questions[0], status=None)
        broken["questions"] = questions
        broken["overview"] = {}
        with self.assertRaises(HermesResultInvalid):
            validate_result(broken)

    def test_review_summary_text_in_count_field_is_coerced(self):
        """把说明文字写进 review_summary.scope 时按 0 处理，并在 note 留痕。"""
        data = dict(LEARNING_RESULT)
        data["review_summary"] = {
            "state": "not_run",
            "scope": "已判错题的二次核查（本次 16 题均未判定为错题）",
            "disagreed": 0, "unverified": 1,
            "note": "",
        }
        result = validate_result(data)
        summary = result["review_summary"]
        self.assertEqual(summary["scope"], 0)
        self.assertEqual(summary["unverified"], 1)
        self.assertIn("scope 原文为", summary["note"])

    def test_coerced_scope_cannot_bypass_completed_review_check(self):
        """scope 归零后不能绕过「核查完成且存在错题时 scope 不能为 0」。"""
        data = dict(LEARNING_RESULT)
        data["review_summary"] = {
            "state": "completed", "scope": "送核查的错题数：1",
            "disagreed": 0, "unverified": 0, "note": "",
        }
        with self.assertRaises(HermesResultInvalid):
            validate_result(data)

    def test_overview_string_counts_are_coerced(self):
        """overview 的计数写成数字字符串时能解析，最终口径仍由逐题数据重算。"""
        data = dict(LEARNING_RESULT)
        data["overview"] = {"checked_questions": "16", "summary": "全对"}
        result = validate_result(data)
        self.assertEqual(result["overview"]["checked_questions"], 16)
        self.assertEqual(result["overview"]["wrong"], 1)


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

    def test_followup_includes_prev_result_and_revision_rules(self):
        settings = self._settings()
        task = {"id": "t1", "openid": "u1", "task_type": "grading", "subject": "数学",
                "input_text": "原始说明"}
        run = {"run_no": 2, "kind": "followup", "output_dir": "/tmp/out",
               "input_text": "补充：第三题漏拍"}
        prev = {"subject": "数学",
                "questions": [{"uid": "数学|作业|2026-09-28||3", "no": "3",
                               "status": "wrong"}],
                "missing_info": ["第3题缺图"],
                "archive": {"suggested_path": "数学/错题解析/2026-09-28.md",
                            "content_markdown": "很长很长的归档正文不应进上下文"}}
        messages = build_messages(settings, task, run, [], prev)
        text = messages[1]["content"][0]["text"]
        self.assertIn("增量修订", text)
        self.assertIn("补充：第三题漏拍", text)
        self.assertIn("数学|作业|2026-09-28||3", text)
        self.assertIn("每个 uid 都必须出现", text)
        self.assertIn("原始提交说明", text)
        # 上一轮归档长文本不应进上下文（省 token）
        self.assertNotIn("很长很长的归档正文不应进上下文", text)

    def test_followup_without_prev_result_has_no_revision_section(self):
        settings = self._settings()
        task = {"id": "t1", "task_type": "grading", "input_text": ""}
        run = {"run_no": 2, "kind": "followup", "output_dir": "/tmp/out",
               "input_text": "补充说明"}
        messages = build_messages(settings, task, run, [])
        text = messages[1]["content"][0]["text"]
        self.assertIn("补充说明", text)
        self.assertNotIn("增量修订", text)

    def test_initial_run_uses_task_text(self):
        settings = self._settings()
        task = {"id": "t1", "task_type": "grading", "input_text": "只看第 3 题"}
        run = {"run_no": 1, "kind": "initial", "output_dir": "/tmp/out",
               "input_text": "只看第 3 题"}
        messages = build_messages(settings, task, run, [])
        text = messages[1]["content"][0]["text"]
        self.assertIn("用户文字说明", text)
        self.assertIn("只看第 3 题", text)
        self.assertNotIn("增量修订", text)


if __name__ == "__main__":
    unittest.main()
