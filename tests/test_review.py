"""服务端二次复查测试：纯逻辑（候选/基线/身份/对账/合并）与 TaskRunner 编排闭环。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from app import db, review, tasks
from app.config import Settings
from app.hermes import validate_result
from tests.mock_hermes import LEARNING_RESULT
from tests.test_tasks import seed_asset, seed_user


def make_settings(tmp: str, **overrides) -> Settings:
    data = {
        "engine": {"mode": "hermes"},
        "hermes": {"base_url": "http://hermes.local", "api_key": "k"},
        "data_dir": str(Path(tmp) / "data"),
        "workspace": {"dir": str(Path(tmp) / "workspace")},
        "limits": {"worker_enabled": False, "worker_poll_seconds": 0.05},
    }
    data.update(overrides)
    return Settings.model_validate(data)


REVIEW_SETTINGS = {
    "hermes": {
        "base_url": "http://hermes.local", "api_key": "k",
        "review_model": "glm",
        "review_expected_model": "glm-5.3",
        "review_expected_provider": "zai",
    },
}


class ReviewFakeClient:
    """同时实现首轮与复查调用的假客户端：可控复查身份与输出。"""

    def __init__(self, result=None, review_payload=None, review_error=None) -> None:
        self.result = result or copy.deepcopy(LEARNING_RESULT)
        self.review_payload = review_payload
        self.review_error = review_error
        self.run_calls = 0
        self.review_calls = 0
        self.last_review_messages = None

    async def run_task(self, messages, session_id):
        self.run_calls += 1
        return {"result": copy.deepcopy(self.result), "model": "hermes-agent",
                "reported_model": "hermes-agent", "reported_provider": "",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    async def review_questions(self, messages, session_id, timeout=None):
        self.review_calls += 1
        self.last_review_messages = messages
        if self.review_error:
            raise self.review_error
        if self.review_payload is not None:
            return self.review_payload
        return {
            "reviews": [{"id": "sim-p12-q1", "state": "agreed",
                         "note": "复核由 2x=8 得 x=4，未发现异议", "basis": ""}],
            "model_requested": "glm", "reported_model": "glm-5.3",
            "reported_provider": "zai",
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            "raw_excerpt": "",
        }


def _q(qid: str, status: str) -> dict:
    question = {"id": qid, "no": qid, "status": status,
                "final_decision": "pending"}
    if status == "wrong":
        question.update({
            "correct_answer": "x=4", "steps": ["2x=8", "x=4"],
            "error_rule": "移项时忘记变号",
            "remediation": {"state": "pending_correction"},
        })
    return question


class SelectTargetsTest(unittest.TestCase):
    def test_wrong_and_uncertain_selected_in_order(self):
        questions = [_q("a", "correct"), _q("b", "wrong"), _q("c", "uncertain"),
                     _q("d", "unanswered"), _q("e", "wrong")]
        sent, overflow = review.select_review_targets(questions, 30)
        self.assertEqual([q["id"] for q in sent], ["b", "c", "e"])
        self.assertEqual(overflow, [])

    def test_overflow_prefers_wrong(self):
        questions = ([_q(f"w{i}", "wrong") for i in range(3)]
                     + [_q(f"u{i}", "uncertain") for i in range(3)])
        sent, overflow = review.select_review_targets(questions, 4)
        self.assertEqual([q["id"] for q in sent], ["w0", "w1", "w2", "u0"])
        self.assertEqual(overflow, ["u1", "u2"])


class NormalizeTest(unittest.TestCase):
    def test_not_configured_wipes_self_claimed_review(self):
        """首轮自称「已核查通过」的字段必须被服务端重写为未执行。"""
        data = copy.deepcopy(LEARNING_RESULT)
        result = review.apply_not_configured(data)
        summary = result["review_summary"]
        self.assertEqual(summary["state"], "not_run")
        self.assertEqual(summary["target_count"], 1)   # 一道判错题
        self.assertEqual(summary["scope"], 0)
        self.assertEqual(summary["unprocessed"], 1)
        wrong_q = result["questions"][0]
        self.assertEqual(wrong_q["review"]["state"], "unprocessed")
        self.assertEqual(wrong_q["review"]["note"], "")
        # 非候选题（unanswered）不参与
        self.assertEqual(result["questions"][1]["review"]["state"], "not_applicable")
        # 学业判定不被改写
        self.assertEqual(wrong_q["status"], "wrong")
        self.assertEqual(wrong_q["remediation"]["state"], "pending_correction")
        validate_result(result)

    def test_not_required_when_no_candidates(self):
        data = copy.deepcopy(LEARNING_RESULT)
        data["questions"] = [_q("a", "correct"), _q("b", "unanswered")]
        data["review_summary"] = {"state": "completed", "scope": 1}
        result = review.apply_not_required(data)
        self.assertEqual(result["review_summary"]["state"], "not_required")
        self.assertEqual(result["review_summary"]["target_count"], 0)
        validate_result(result)


class IdentityTest(unittest.TestCase):
    def _first(self, model="hermes-agent"):
        return {"reported_model": model, "reported_provider": ""}

    def test_confirmed_when_matches_expected(self):
        payload = {"reported_model": "glm-5.3", "reported_provider": "zai"}
        identity, _ = review.check_model_identity(
            payload, self._first(), "glm-5.3", "zai")
        self.assertEqual(identity, review.IDENTITY_CONFIRMED)

    def test_mismatch_when_same_as_first_round(self):
        payload = {"reported_model": "hermes-agent", "reported_provider": ""}
        identity, note = review.check_model_identity(
            payload, self._first(), "glm-5.3", "zai")
        self.assertEqual(identity, review.IDENTITY_MISMATCH)
        self.assertIn("同一模型", note)

    def test_unknown_when_gateway_silent(self):
        identity, note = review.check_model_identity(
            {"reported_model": "", "reported_provider": ""},
            self._first(), "glm-5.3", "zai")
        self.assertEqual(identity, review.IDENTITY_UNKNOWN)
        self.assertIn("未报告", note)

    def test_unknown_when_expected_not_configured(self):
        payload = {"reported_model": "glm-5.3", "reported_provider": "zai"}
        identity, note = review.check_model_identity(payload, self._first(), "", "")
        self.assertEqual(identity, review.IDENTITY_UNKNOWN)
        self.assertIn("review_expected_model", note)

    def test_mismatch_when_differs_from_expected(self):
        payload = {"reported_model": "deepseek-v4-pro", "reported_provider": "deepseek"}
        identity, note = review.check_model_identity(
            payload, self._first(), "glm-5.3", "zai")
        self.assertEqual(identity, review.IDENTITY_MISMATCH)

    def test_unknown_when_provider_unreported(self):
        payload = {"reported_model": "glm-5.3", "reported_provider": ""}
        identity, _ = review.check_model_identity(payload, self._first(), "glm-5.3", "zai")
        self.assertEqual(identity, review.IDENTITY_UNKNOWN)


class ReconcileTest(unittest.TestCase):
    def test_full_coverage_ok(self):
        sent = [_q("a", "wrong"), _q("b", "uncertain")]
        items = [{"id": "a", "state": "agreed"}, {"id": "b", "state": "disagreed",
                                                  "basis": "依据"}]
        by_id, problems = review.reconcile_reviews(sent, items)
        self.assertEqual(problems, [])
        self.assertEqual(set(by_id), {"a", "b"})

    def test_missing_and_unknown_ids_reported(self):
        sent = [_q("a", "wrong"), _q("b", "wrong")]
        items = [{"id": "a", "state": "agreed"}, {"id": "zzz", "state": "agreed"}]
        by_id, problems = review.reconcile_reviews(sent, items)
        self.assertIn("未送审的题 zzz", "；".join(problems))
        self.assertIn("未返回复查结论", "；".join(problems))
        self.assertEqual(set(by_id), {"a"})


class ApplyReviewResultTest(unittest.TestCase):
    def test_disagreed_keeps_judgment_and_marks_basis(self):
        data = copy.deepcopy(LEARNING_RESULT)
        sent = [data["questions"][0]]
        items = [{"id": "sim-p12-q1", "state": "disagreed",
                  "note": "学生作答实际正确", "basis": "2x+1=9 → x=4 与作答一致"}]
        by_id, problems = review.reconcile_reviews(sent, items)
        self.assertEqual(problems, [])
        result = review.apply_review_result(data, sent, by_id, [], {
            "model_requested": "glm", "model_reported": "glm-5.3",
            "model_identity": "confirmed", "coverage": "full_images"})
        q = result["questions"][0]
        # 只提异议：不改判、不改订正状态
        self.assertEqual(q["status"], "wrong")
        self.assertEqual(q["final_decision"], "kept_wrong")
        self.assertEqual(q["remediation"]["state"], "pending_correction")
        self.assertEqual(q["review"]["state"], "disagreed")
        self.assertIn("尚未重新裁决", q["final_decision_basis"])
        self.assertIn("首轮依据", q["final_decision_basis"])
        summary = result["review_summary"]
        self.assertEqual(summary["state"], "completed")
        self.assertEqual(summary["disagreed"], 1)
        self.assertEqual(summary["scope"], 1)
        validate_result(result)

    def test_overflow_and_text_only_make_partial(self):
        data = copy.deepcopy(LEARNING_RESULT)
        data["questions"].append(_q("extra-1", "wrong"))
        sent, overflow = review.select_review_targets(data["questions"], 1)
        self.assertEqual(overflow, ["extra-1"])
        by_id, _ = review.reconcile_reviews(
            sent, [{"id": sent[0]["id"], "state": "agreed"}])
        result = review.apply_review_result(data, sent, by_id, overflow, {
            "coverage": "text_only", "model_identity": "confirmed",
            "model_requested": "glm", "model_reported": "glm-5.3"})
        summary = result["review_summary"]
        self.assertEqual(summary["state"], "partial")
        self.assertEqual(summary["unprocessed"], 1)
        self.assertIn("未随附原图", summary["note"])
        validate_result(result)

    def test_failed_marks_sent_unverified(self):
        data = copy.deepcopy(LEARNING_RESULT)
        sent = [data["questions"][0]]
        result = review.apply_failed(data, sent, [], "复查调用失败：超时")
        q = result["questions"][0]
        self.assertEqual(q["review"]["state"], "unverified")
        self.assertEqual(result["review_summary"]["state"], "failed")
        self.assertEqual(result["review_summary"]["unverified"], 1)
        validate_result(result)


class ReviewMarkdownTest(unittest.TestCase):
    def test_not_required_yields_empty(self):
        data = copy.deepcopy(LEARNING_RESULT)
        result = review.apply_not_required(
            {**data, "questions": [_q("a", "correct")]})
        self.assertEqual(review.build_review_markdown(result), "")

    def test_completed_summary_written(self):
        data = copy.deepcopy(LEARNING_RESULT)
        sent = [data["questions"][0]]
        by_id, _ = review.reconcile_reviews(
            sent, [{"id": "sim-p12-q1", "state": "agreed", "note": "无异议"}])
        result = review.apply_review_result(data, sent, by_id, [], {
            "model_requested": "glm", "model_reported": "glm-5.3",
            "model_identity": "confirmed", "coverage": "full_images"})
        md = review.build_review_markdown(result)
        self.assertIn("服务端二次复查记录", md)
        self.assertIn("状态：已完成", md)
        self.assertIn("glm-5.3", md)
        self.assertIn("未发现异议", md)

    def test_failed_summary_written(self):
        data = copy.deepcopy(LEARNING_RESULT)
        result = review.apply_not_configured(data)
        md = review.build_review_markdown(result)
        self.assertIn("状态：未执行", md)
        self.assertIn("未配置复查模型", md)


class ReviewPipelineTest(unittest.IsolatedAsyncioTestCase):
    """TaskRunner 编排：复查各失败路径都不吞首轮成果。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name, **REVIEW_SETTINGS)
        await db.init_db(self.settings.db_path)
        await seed_user(self.settings, "u1")
        await seed_asset(self.settings, "u1", "a1")
        path = Path(self.settings.upload_dir) / "a1.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        from tests.test_workspace import png_bytes
        path.write_bytes(png_bytes())

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _create(self):
        return await tasks.create_study_task(
            self.settings, "u1",
            {"task_type": "grading", "subject": "数学", "text": "", "asset_ids": ["a1"]}, "")

    async def _run(self, client):
        runner = tasks.TaskRunner(self.settings, client)
        task = await db.claim_next_task(self.settings.db_path, "tester", 60)
        self.assertIsNotNone(task)
        await runner.execute(task)

    async def test_configured_review_completes_and_sums_usage(self):
        created = await self._create()
        client = ReviewFakeClient()
        await self._run(client)

        self.assertEqual(client.run_calls, 1)
        self.assertEqual(client.review_calls, 1)
        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(task["status"], "waiting_input")  # missing_info 保留
        # usage = 首轮 + 复查
        self.assertEqual(task["input_tokens"], 17)
        self.assertEqual(task["output_tokens"], 8)

        view = await tasks.build_task_view(self.settings, task)
        result = view["result"]
        summary = result["review_summary"]
        self.assertEqual(summary["state"], "completed")
        self.assertEqual(summary["scope"], 1)
        self.assertEqual(summary["model_reported"], "glm-5.3")
        self.assertEqual(summary["model_identity"], "confirmed")
        q = result["questions"][0]
        self.assertEqual(q["review"]["state"], "agreed")
        self.assertEqual(q["status"], "wrong")

    async def test_identity_unknown_fails_review_but_keeps_task(self):
        created = await self._create()
        payload = {
            "reviews": [{"id": "sim-p12-q1", "state": "agreed"}],
            "model_requested": "glm", "reported_model": "",
            "reported_provider": "", "usage": {}, "raw_excerpt": "",
        }
        await self._run(ReviewFakeClient(review_payload=payload))
        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(task["status"], "waiting_input")
        view = await tasks.build_task_view(self.settings, task)
        summary = view["result"]["review_summary"]
        self.assertEqual(summary["state"], "failed")
        self.assertIn("身份", summary["note"])
        q = view["result"]["questions"][0]
        self.assertEqual(q["review"]["state"], "unverified")

    async def test_misrouted_review_fails_and_first_round_claims_wiped(self):
        """误路由回首轮模型 + 首轮自述「已核查」都不会进入最终结果。"""
        created = await self._create()
        payload = {
            "reviews": [{"id": "sim-p12-q1", "state": "agreed"}],
            "model_requested": "glm", "reported_model": "hermes-agent",
            "reported_provider": "", "usage": {}, "raw_excerpt": "",
        }
        await self._run(ReviewFakeClient(review_payload=payload))
        task = await db.get_task(self.settings.db_path, created["task_id"])
        view = await tasks.build_task_view(self.settings, task)
        summary = view["result"]["review_summary"]
        self.assertEqual(summary["state"], "failed")
        self.assertIn("同一模型", summary["note"])
        q = view["result"]["questions"][0]
        self.assertEqual(q["review"]["state"], "unverified")

    async def test_review_call_error_keeps_first_round_result(self):
        created = await self._create()
        await self._run(ReviewFakeClient(review_error=RuntimeError("网络中断")))
        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(task["status"], "waiting_input")
        view = await tasks.build_task_view(self.settings, task)
        summary = view["result"]["review_summary"]
        self.assertEqual(summary["state"], "failed")
        self.assertIn("复查调用失败", summary["note"])
        # 首轮判定完整保留
        self.assertEqual(view["result"]["questions"][0]["status"], "wrong")

    async def test_missing_review_output_rejected(self):
        created = await self._create()
        payload = {
            "reviews": [],  # 空列表：协议非法
            "model_requested": "glm", "reported_model": "glm-5.3",
            "reported_provider": "zai", "usage": {}, "raw_excerpt": "",
        }
        await self._run(ReviewFakeClient(review_payload=payload))
        task = await db.get_task(self.settings.db_path, created["task_id"])
        view = await tasks.build_task_view(self.settings, task)
        self.assertEqual(view["result"]["review_summary"]["state"], "failed")

    async def test_not_configured_marks_not_run(self):
        self.settings = make_settings(self.tmp.name)  # 无复查配置
        created = await self._create()
        await self._run(ReviewFakeClient())
        task = await db.get_task(self.settings.db_path, created["task_id"])
        view = await tasks.build_task_view(self.settings, task)
        summary = view["result"]["review_summary"]
        self.assertEqual(summary["state"], "not_run")
        self.assertEqual(summary["target_count"], 1)
        # 首轮自述的 completed/agreed 被清除
        self.assertEqual(view["result"]["questions"][0]["review"]["state"], "unprocessed")

    async def test_no_candidates_marks_not_required(self):
        no_wrong = copy.deepcopy(LEARNING_RESULT)
        no_wrong["questions"] = [{"id": "q-ok", "no": "1", "status": "correct",
                                  "final_decision": "kept_correct"},
                                 {"id": "q-skip", "no": "2", "status": "unanswered",
                                  "final_decision": "pending"}]
        no_wrong["overview"] = {}
        no_wrong["review_summary"] = {"state": "not_run", "scope": 0}
        created = await self._create()
        await self._run(ReviewFakeClient(result=no_wrong))
        task = await db.get_task(self.settings.db_path, created["task_id"])
        view = await tasks.build_task_view(self.settings, task)
        self.assertEqual(view["result"]["review_summary"]["state"], "not_required")

    async def test_budget_exhausted_marks_not_run(self):
        """预算在复查派发前耗尽：如实标 not_run，不伪装成已派发失败。"""
        import time as _time

        runner = tasks.TaskRunner(self.settings, ReviewFakeClient())
        result = copy.deepcopy(LEARNING_RESULT)
        expired = _time.monotonic() - 1
        merged = await runner._run_review(
            {"id": "t1", "subject": "数学"}, {"run_no": 1, "kind": "initial"},
            result, expired, {"reported_model": "hermes-agent"})
        self.assertEqual(merged["review_summary"]["state"], "not_run")
        self.assertIn("预算已耗尽", merged["review_summary"]["note"])
        self.assertEqual(merged["questions"][0]["review"]["state"], "unprocessed")

    async def test_followup_with_new_image_reruns_review(self):
        """补充图片后重新复查当前全部候选题（首版不做跨轮复用）。"""
        created = await self._create()
        client = ReviewFakeClient()
        await self._run(client)
        self.assertEqual(client.review_calls, 1)

        await seed_asset(self.settings, "u1", "a2")
        path = Path(self.settings.upload_dir) / "a2.jpg"
        from tests.test_workspace import png_bytes
        path.write_bytes(png_bytes())
        await tasks.add_followup(self.settings, "u1", created["task_id"],
                                 {"text": "补充第 3 页图片", "asset_ids": ["a2"]})
        await self._run(client)
        self.assertEqual(client.run_calls, 2)
        self.assertEqual(client.review_calls, 2)
        # 复查消息带了完整材料（首轮 + 补充），不能只有本轮新增图片
        content = client.last_review_messages[1]["content"]
        self.assertEqual(len([c for c in content if c["type"] == "image_url"]), 2)

    async def test_disagreed_review_does_not_change_ledger(self):
        """复查异议只记录，不改台账判定与订正状态，也不产生复测事件。"""
        created = await self._create()
        payload = {
            "reviews": [{"id": "sim-p12-q1", "state": "disagreed",
                         "note": "疑似误判", "basis": "2x+1=9 → x=4，学生作答一致"}],
            "model_requested": "glm", "reported_model": "glm-5.3",
            "reported_provider": "zai", "usage": {}, "raw_excerpt": "",
        }
        await self._run(ReviewFakeClient(review_payload=payload))
        task = await db.get_task(self.settings.db_path, created["task_id"])
        view = await tasks.build_task_view(self.settings, task)
        q = view["result"]["questions"][0]
        self.assertEqual(q["status"], "wrong")
        self.assertEqual(q["remediation"]["state"], "pending_correction")
        self.assertEqual(view["result"]["review_summary"]["disagreed"], 1)

        ledger = await db.list_ledger_by_task(self.settings.db_path, "u1",
                                              created["task_id"])
        by_no = {r["question_no"]: r for r in ledger}
        self.assertEqual(by_no["1"]["status"], "wrong")
        self.assertEqual(by_no["1"]["remediation_state"], "pending_correction")
        events = await db.list_question_events(self.settings.db_path, "u1")
        review_events = [e for e in events if e["event_type"] == "correction"
                         or e["result"] == "retest_passed"]
        self.assertEqual(review_events, [], "复查异议不得制造订正/复测事件")

    async def test_review_postscript_written_to_archive_once(self):
        """复查附记随本轮归档一次写入，且与结果 JSON 同源一致。"""
        created = await self._create()
        await self._run(ReviewFakeClient())
        task = await db.get_task(self.settings.db_path, created["task_id"])
        archive = (Path(self.settings.workspace_dir) / "wx-u1" / "数学" / "错题解析"
                   / "2026-09-26.md")
        text = archive.read_text(encoding="utf-8")
        self.assertIn("### 服务端二次复查记录", text)
        self.assertIn("glm-5.3", text)
        self.assertIn("未发现异议", text)
        # 一次执行只写一份附记（轮次幂等）
        self.assertEqual(text.count("### 服务端二次复查记录"), 1)
        # JSON 的 archive.content_markdown 与落盘正文同源
        result = json.loads(task["result_json"])
        self.assertIn("### 服务端二次复查记录",
                      result["archive"]["content_markdown"])

    async def test_not_configured_postscript_honest(self):
        """未配置复查模型时，归档如实写「未执行」，不保留首轮自述。"""
        self.settings = make_settings(self.tmp.name)  # 无复查配置
        created = await self._create()
        await self._run(ReviewFakeClient())
        archive = (Path(self.settings.workspace_dir) / "wx-u1" / "数学" / "错题解析"
                   / "2026-09-26.md")
        text = archive.read_text(encoding="utf-8")
        self.assertIn("状态：未执行", text)
        self.assertIn("未配置复查模型", text)
        # 首轮正文照常保留
        self.assertIn("移项未变号", text)


if __name__ == "__main__":
    unittest.main()
