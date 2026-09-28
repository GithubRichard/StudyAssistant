"""分阶段批改流水线测试：全部使用 FakeProvider，不产生外部调用。"""
from __future__ import annotations

import copy
import json
import unittest

from app import staged
from app.config import Settings
from app.providers import GradeOutcome, ProviderError
from app.hermes import validate_result
from app.staged import StageError, normalize_answer


def make_settings(*names: str) -> Settings:
    providers = {
        n: {"base_url": "http://fake", "api_key": "k", "model": f"fake-{n}",
            "price_input_per_1m": 1.0, "price_output_per_1m": 2.0}
        for n in names
    }
    return Settings(llm={"default_provider": names[0], "providers": providers})


class FakeProvider:
    """按剧本返回各阶段 JSON；script[stage] 是响应/异常队列。"""

    def __init__(self, name, script, calls):
        self.name = name
        self._script = script
        self.calls = calls

    def _respond(self, stage, system, user, n_images=0):
        self.calls.append({"stage": stage, "system": system, "user": user,
                           "n_images": n_images, "provider": self.name})
        queue = self._script.get(stage, [])
        if not queue:
            raise AssertionError(f"fake provider {self.name} 没有 {stage} 的剧本")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return GradeOutcome(text=item, input_tokens=10, output_tokens=20,
                            provider=self.name, model=f"fake-{self.name}")

    async def grade_multi(self, images, system, user, max_tokens=8000):
        return self._respond("extract", system, user, len(images))

    async def complete_text(self, system, user, max_tokens=4000):
        if "解题专家" in system:
            stage = "solve"
        elif "等价性裁判" in system:
            stage = "compare"
        elif "错因诊断专家" in system:
            stage = "diagnose"
        else:
            raise AssertionError(f"无法识别阶段: {system[:40]}")
        return self._respond(stage, system, user)


def factory_for(scripts: dict, calls: list):
    def factory(name, cfg):
        return FakeProvider(name, scripts[name], calls)
    return factory


EXTRACT_OK = json.dumps({"questions": [
    {"no": "1", "stem": "解方程 2x+1=9", "student_answer": "x=4",
     "page": "1", "handwriting_uncertain": False, "uncertain_note": ""},
    {"no": "2", "stem": "This amazing ___ (creative) technology",
     "student_answer": "creatively", "page": "1",
     "handwriting_uncertain": False, "uncertain_note": ""},
    {"no": "3", "stem": "化简 (a+b)²", "student_answer": "",
     "page": "1", "handwriting_uncertain": True, "uncertain_note": "笔迹潦草无法辨认"},
]}, ensure_ascii=False)

SOLVE_OK = json.dumps({"solutions": [
    {"no": "1", "correct_answer": "x=4", "steps": ["2x=8", "x=4"]},
    {"no": "2", "correct_answer": "creative", "steps": ["形容词修饰名词 technology"]},
    {"no": "3", "correct_answer": "a²+2ab+b²", "steps": ["完全平方公式展开"]},
]}, ensure_ascii=False)

COMPARE_OK = json.dumps({"judgments": [
    {"no": "2", "equivalent": False},
]}, ensure_ascii=False)

DIAGNOSE_OK = json.dumps({"diagnoses": [
    {"no": "2", "error_rule": "把 creative 误写成副词形式 creatively，混淆形容词与副词的修饰对象",
     "knowledge_point": "形容词与副词词形辨析",
     "explanation": ["technology 是名词，前面要用形容词修饰", "creatively 是副词，不能修饰名词"],
     "correct_answer": "creative"},
]}, ensure_ascii=False)


class NormalizeTest(unittest.IsolatedAsyncioTestCase):
    async def test_normalize(self):
        self.assertEqual(normalize_answer("ｘ＝４"), "x=4")
        self.assertEqual(normalize_answer("−8p² −12q²"), "-8p2-12q2")  # NFKC: ²→2，两侧一致即可
        self.assertEqual(normalize_answer("  Creative "), "creative")


class InitialPipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.scripts = {"fake": {
            "extract": [EXTRACT_OK], "solve": [SOLVE_OK],
            "compare": [COMPARE_OK], "diagnose": [DIAGNOSE_OK]}}
        self.settings = make_settings("fake")

    async def _grade(self):
        return await staged.grade_staged(
            [(b"img", "image/jpeg")], "数学", "七年级", "", self.settings,
            provider_factory=factory_for(self.scripts, self.calls))

    async def test_statuses(self):
        outcome = await self._grade()
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        # q1 确定性比对直接判对（compare 剧本里没有 q1）
        self.assertEqual(by_no["1"]["status"], "correct")
        self.assertEqual(by_no["2"]["status"], "wrong")
        self.assertEqual(by_no["3"]["status"], "uncertain")
        # 错题有具体错因与知识点
        self.assertIn("副词", by_no["2"]["error_rule"])
        self.assertEqual(by_no["2"]["knowledge_point"], "形容词与副词词形辨析")
        # v3 校验已通过：overview 计数一致
        ov = outcome.result["overview"]
        self.assertEqual(ov["checked_questions"], 3)

    async def test_solve_prompt_excludes_student_answers(self):
        await self._grade()
        solve_calls = [c for c in self.calls if c["stage"] == "solve"]
        self.assertEqual(len(solve_calls), 1)
        user = solve_calls[0]["user"]
        # 求解阶段只能看到题干，看不到学生答案（防锚定）
        self.assertIn("解方程 2x+1=9", user)
        self.assertNotIn("creatively", user)
        self.assertNotIn("x=4", user.replace("解方程 2x+1=9", ""))

    async def test_extract_sees_all_images(self):
        await staged.grade_staged(
            [(b"a", "image/jpeg"), (b"b", "image/jpeg")], "数学", "七年级", "",
            self.settings, provider_factory=factory_for(self.scripts, self.calls))
        extract_calls = [c for c in self.calls if c["stage"] == "extract"]
        self.assertEqual(extract_calls[0]["n_images"], 2)

    async def test_stage_callback_order(self):
        seen = []

        async def recorder(name, data):
            seen.append(name)
        await staged.grade_staged(
            [(b"img", "image/jpeg")], "数学", "七年级", "", self.settings,
            provider_factory=factory_for(self.scripts, self.calls),
            on_stage=recorder)
        self.assertEqual(seen, ["extract", "solve", "compare", "diagnose", "assembled"])

    async def test_usage_and_cost_tracked(self):
        outcome = await self._grade()
        # 4 个阶段各 10/20 tokens
        self.assertEqual(outcome.input_tokens, 40)
        self.assertEqual(outcome.output_tokens, 80)
        self.assertGreater(outcome.cost, 0)


class CompareEquivalenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_math_reordered_terms_judged_equivalent(self):
        calls = []
        scripts = {"fake": {
            "extract": [json.dumps({"questions": [
                {"no": "1", "stem": "化简", "student_answer": "-12q²-8p²",
                 "page": "1", "handwriting_uncertain": False, "uncertain_note": ""}]})],
            "solve": [json.dumps({"solutions": [
                {"no": "1", "correct_answer": "-8p²-12q²", "steps": []}]})],
            "compare": [json.dumps({"judgments": [{"no": "1", "equivalent": True}]})],
            "diagnose": [],
        }}
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "数学", "七年级", "", make_settings("fake"),
            provider_factory=factory_for(scripts, calls))
        q = outcome.result["questions"][0]
        self.assertEqual(q["status"], "correct")
        # 诊断阶段没有被调用（无错题）
        self.assertFalse([c for c in calls if c["stage"] == "diagnose"])


class ProviderFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_extract_falls_back_to_second_provider(self):
        calls = []
        scripts = {
            "p1": {"extract": [ProviderError("boom")],
                   "solve": [ProviderError("boom")],
                   "compare": [ProviderError("boom")],
                   "diagnose": [ProviderError("boom")]},
            "p2": {"extract": [EXTRACT_OK], "solve": [SOLVE_OK],
                   "compare": [COMPARE_OK], "diagnose": [DIAGNOSE_OK]},
        }
        settings = make_settings("p1", "p2")
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "数学", "七年级", "", settings,
            chain=["p1", "p2"], provider_factory=factory_for(scripts, calls))
        self.assertEqual(len(outcome.result["questions"]), 3)
        self.assertIn("p2", outcome.model)
        self.assertNotIn("p1/", outcome.model)

    async def test_all_providers_fail_raises_stage_error(self):
        calls = []
        scripts = {"p1": {"extract": [ProviderError("x")], "solve": [],
                          "compare": [], "diagnose": []}}
        with self.assertRaises(StageError) as ctx:
            await staged.grade_staged(
                [(b"img", "image/jpeg")], "数学", "七年级", "",
                make_settings("p1"),
                provider_factory=factory_for(scripts, calls))
        self.assertEqual(ctx.exception.stage, "extract")

    async def test_vague_error_rule_triggers_fallback(self):
        calls = []
        vague = json.dumps({"diagnoses": [
            {"no": "2", "error_rule": "粗心", "knowledge_point": "k",
             "explanation": ["e"], "correct_answer": "creative"}]})
        scripts = {
            "p1": {"extract": [EXTRACT_OK], "solve": [SOLVE_OK],
                   "compare": [COMPARE_OK], "diagnose": [vague]},
            "p2": {"extract": [], "solve": [], "compare": [],
                   "diagnose": [DIAGNOSE_OK]},
        }
        settings = make_settings("p1", "p2")
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "数学", "七年级", "", settings,
            chain=["p1", "p2"], provider_factory=factory_for(scripts, calls))
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertNotEqual(by_no["2"]["error_rule"], "粗心")


def make_prev_result():
    return {
        "schema_version": 3, "task_type": "grading", "subject": "数学",
        "grade_level": "七年级",
        "overview": {"checked_questions": 3, "summary": "旧"},
        "questions": [
            {"id": "q1", "uid": "uid-1", "no": "1", "stem": "解方程 2x+1=9",
             "student_answer": "x=5", "status": "wrong", "correct_answer": "x=4",
             "steps": ["2x=8", "x=4"], "error_rule": "移项未变号",
             "knowledge_point": "一元一次方程",
             "review": {"state": "not_applicable", "note": "", "basis": ""},
             "final_decision": "kept_wrong", "final_decision_basis": "",
             "remediation": {"state": "pending_correction", "updated_date": "",
                             "linked_training": "", "note": ""}},
            {"id": "q2", "uid": "uid-2", "no": "2", "stem": "填空 creative",
             "student_answer": "creatively", "status": "wrong",
             "correct_answer": "creative", "steps": [], "error_rule": "词形错误",
             "knowledge_point": "形容词副词",
             "review": {"state": "not_applicable", "note": "", "basis": ""},
             "final_decision": "kept_wrong", "final_decision_basis": "",
             "remediation": {"state": "pending_correction", "updated_date": "",
                             "linked_training": "", "note": ""}},
            {"id": "q3", "uid": "uid-3", "no": "3", "stem": "化简",
             "student_answer": "a²+2ab+b²", "status": "correct",
             "correct_answer": "a²+2ab+b²", "steps": [],
             "error_rule": "", "knowledge_point": "",
             "review": {"state": "not_applicable", "note": "", "basis": ""},
             "final_decision": "kept_correct", "final_decision_basis": "",
             "remediation": {"state": "not_applicable", "updated_date": "",
                             "linked_training": "", "note": ""}},
        ],
        "retests": [], "sections": [], "missing_info": [], "parent_tips": [],
        "review_summary": {"state": "not_run", "scope": 0, "disagreed": 0,
                           "unverified": 0, "note": ""},
        "archive": {"action": "none"},
        "delivery": {},
    }


class FollowupPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_untouched_questions_pass_through_verbatim(self):
        calls = []
        extract_fu = json.dumps({
            "new_questions": [],
            "revisions": [{"prev_no": "2", "student_answer": "creative",
                            "note": "补充说明澄清作答为 creative"}],
        }, ensure_ascii=False)
        scripts = {"fake": {
            "extract": [extract_fu],
            "solve": [json.dumps({"solutions": [
                {"no": "2", "correct_answer": "creative", "steps": []}]})],
            "compare": [],  # 归一化后相等，走确定性路径，不调模型
            "diagnose": [],
        }}
        prev = make_prev_result()
        prev_copy = copy.deepcopy(prev)
        expected = validate_result(copy.deepcopy(prev))["questions"]
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "数学", "七年级", "补充说明", make_settings("fake"),
            prev_result=prev, followup_no=2,
            provider_factory=factory_for(scripts, calls))
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        # 未受影响的 q1/q3 逐字段原样透传（以 v3 归一化后为准）
        exp_by_no = {q["no"]: q for q in expected}
        self.assertEqual(by_no["1"], exp_by_no["1"])
        self.assertEqual(by_no["3"], exp_by_no["3"])
        # 受影响的 q2 被订正为对
        self.assertEqual(by_no["2"]["status"], "correct")
        self.assertEqual(by_no["2"]["final_decision"], "corrected_to_correct")
        self.assertEqual(by_no["2"]["student_answer"], "creative")
        self.assertEqual(by_no["2"]["uid"], "uid-2")  # uid 保留
        # compare 模型未被调用（确定性比对命中）
        self.assertFalse([c for c in calls if c["stage"] == "compare"])

    async def test_followup_new_questions_appended(self):
        calls = []
        extract_fu = json.dumps({
            "new_questions": [
                {"no": "4", "stem": "新题", "student_answer": "42",
                 "page": "1", "handwriting_uncertain": False, "uncertain_note": ""}],
            "revisions": [],
        }, ensure_ascii=False)
        scripts = {"fake": {
            "extract": [extract_fu],
            "solve": [json.dumps({"solutions": [
                {"no": "4", "correct_answer": "42", "steps": []}]})],
            "compare": [], "diagnose": [],
        }}
        prev = make_prev_result()
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "数学", "七年级", "", make_settings("fake"),
            prev_result=prev, followup_no=2,
            provider_factory=factory_for(scripts, calls))
        self.assertEqual(len(outcome.result["questions"]), 4)
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(by_no["4"]["status"], "correct")
        # 旧题 uid 全保留（_finish 的覆盖校验能通过）
        uids = [q.get("uid") for q in outcome.result["questions"]]
        self.assertIn("uid-1", uids)
        self.assertIn("uid-3", uids)

    async def test_followup_extract_with_no_affected_fails(self):
        calls = []
        scripts = {"fake": {
            "extract": [json.dumps({"new_questions": [], "revisions": []})],
            "solve": [], "compare": [], "diagnose": [],
        }}
        with self.assertRaises(StageError):
            await staged.grade_staged(
                [(b"img", "image/jpeg")], "数学", "七年级", "",
                make_settings("fake"),
                prev_result=make_prev_result(), followup_no=2,
                provider_factory=factory_for(scripts, calls))


if __name__ == "__main__":
    unittest.main()


class StagedExecutorIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """execute() 走 _run_staged：hermes 不被调用、阶段落库、provider 标记为 staged。"""

    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path

        from app import db, tasks
        from tests.test_tasks import (FakeClient, make_settings as task_settings,
                                      run_executor, seed_asset, seed_user)
        from tests.test_workspace import png_bytes

        self._mods = (db, tasks, FakeClient, task_settings, run_executor,
                      seed_asset, seed_user, Path)
        self.tmp = tempfile.TemporaryDirectory()
        data_overrides = {
            "llm": {"default_provider": "fake", "providers": {
                "fake": {"base_url": "http://fake", "api_key": "k",
                         "model": "fake-m"}}},
            "staged_grading": {"enabled": True},
        }
        self.settings = task_settings(self.tmp.name, **data_overrides)
        await db.init_db(self.settings.db_path)
        await seed_user(self.settings, "u1")
        await seed_asset(self.settings, "u1", "a1")
        path = Path(self.settings.upload_dir) / "a1.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes())

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_execute_routes_to_staged_and_persists_stages(self):
        from unittest.mock import patch

        from app import db, tasks, staged as staged_mod
        from app.staged import StagedOutcome
        from tests.test_tasks import FakeClient, run_executor

        raw = {
            "schema_version": 3, "task_type": "grading", "subject": "数学",
            "grade_level": "七年级",
            "overview": {"checked_questions": 1, "summary": "全对"},
            "questions": [{
                "id": "q1", "no": "1", "stem": "1+1=?", "student_answer": "2",
                "status": "correct", "correct_answer": "2", "steps": [],
                "error_rule": "", "knowledge_point": "",
                "review": {"state": "not_applicable", "note": "", "basis": ""},
                "final_decision": "kept_correct",
                "remediation": {"state": "not_applicable", "updated_date": "",
                                "linked_training": "", "note": ""},
            }],
            "review_summary": {"state": "not_run", "scope": 0, "disagreed": 0,
                               "unverified": 0, "note": ""},
            "archive": {"action": "none"},
        }
        seen = {}

        async def fake_grade_staged(images, subject, grade_level, input_text,
                                    settings, chain=None, prev_result=None,
                                    followup_no=0, on_stage=None,
                                    provider_factory=None):
            seen["chain"] = chain
            seen["n_images"] = len(images)
            for name in ("extract", "solve", "compare", "diagnose"):
                await on_stage(name, {"ok": True})
            return StagedOutcome(result=validate_result(raw), model="fake/m",
                                 input_tokens=1, output_tokens=1, cost=0.01,
                                 stages={})

        created = await tasks.create_study_task(
            self.settings, "u1",
            {"task_type": "grading", "subject": "数学", "text": "",
             "asset_ids": ["a1"]}, "")
        client = FakeClient()
        with patch.object(staged_mod, "grade_staged", fake_grade_staged):
            await run_executor(self.settings, client)

        # hermes 完全没被调用，走的是分阶段路径
        self.assertEqual(client.calls, 0)
        self.assertEqual(seen["chain"], ["fake"])
        self.assertEqual(seen["n_images"], 1)

        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["provider"], "staged")

        runs = await db.list_runs(self.settings.db_path, created["task_id"])
        self.assertEqual(runs[0]["stage"], "done")
        stages = json.loads(runs[0]["stages_json"])
        self.assertEqual(sorted(stages.keys()),
                         ["compare", "diagnose", "extract", "solve"])

    async def test_staged_disabled_falls_back_to_hermes(self):
        from unittest.mock import patch

        from app import db, tasks, staged as staged_mod
        from tests.test_tasks import FakeClient, run_executor

        async def should_not_run(*a, **kw):
            raise AssertionError("staged 不该被调用")

        self.settings.staged_grading.enabled = False
        created = await tasks.create_study_task(
            self.settings, "u1",
            {"task_type": "grading", "subject": "数学", "text": "",
             "asset_ids": ["a1"]}, "")
        client = FakeClient()
        with patch.object(staged_mod, "grade_staged", should_not_run):
            await run_executor(self.settings, client)
        self.assertEqual(client.calls, 1, "关闭后应回落到 hermes 路径")
        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(task["provider"], "hermes")


if __name__ == "__main__":
    unittest.main()


class StagedReviewGateTest(unittest.IsolatedAsyncioTestCase):
    """分阶段 + hermes 引擎时复查门的"可疑才查"行为。

    - 全对（无错题/存疑题）→ 跳过复查，不调模型，如实标 not_required
    - 有错题 → 正常走复查（重点核查），调模型
    """

    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path

        from app import db, tasks
        from tests.test_tasks import (make_settings as task_settings,
                                      seed_asset, seed_user)
        from tests.test_workspace import png_bytes
        from tests.test_review import REVIEW_SETTINGS

        self._db, self._tasks = db, tasks
        self.tmp = tempfile.TemporaryDirectory()
        overrides = {
            "llm": {"default_provider": "fake", "providers": {
                "fake": {"base_url": "http://fake", "api_key": "k",
                         "model": "fake-m"}}},
            "staged_grading": {"enabled": True},
            **REVIEW_SETTINGS,
        }
        self.settings = task_settings(self.tmp.name, **overrides)
        await db.init_db(self.settings.db_path)
        await seed_user(self.settings, "u1")
        await seed_asset(self.settings, "u1", "a1")
        path = Path(self.settings.upload_dir) / "a1.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes())

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _raw(self, status):
        q = {
            "id": "q1", "no": "1", "stem": "2x=8，x=?", "student_answer": "4",
            "status": "correct", "correct_answer": "4", "steps": ["2x=8", "x=4"],
            "error_rule": "", "knowledge_point": "一元一次方程",
            "review": {"state": "not_applicable", "note": "", "basis": ""},
            "final_decision": "kept_correct",
            "remediation": {"state": "not_applicable", "updated_date": "",
                            "linked_training": "", "note": ""},
        }
        if status == "wrong":
            q.update({
                "student_answer": "5", "status": "wrong",
                "error_rule": "两边同除时算错", "final_decision": "kept_wrong",
                "remediation": {"state": "pending_correction", "updated_date": "",
                                "linked_training": "", "note": ""},
            })
        return {
            "schema_version": 3, "task_type": "grading", "subject": "数学",
            "grade_level": "七年级",
            "overview": {"checked_questions": 1, "summary": "一批改"},
            "questions": [q],
            "review_summary": {"state": "not_run", "scope": 0, "disagreed": 0,
                               "unverified": 0, "note": ""},
            "archive": {"action": "none"},
        }

    async def _run_with_staged(self, status, client):
        from unittest.mock import patch

        from app import db, tasks, staged as staged_mod
        from app.staged import StagedOutcome
        from tests.test_tasks import run_executor

        raw = self._raw(status)

        async def fake_grade_staged(images, subject, grade_level, input_text,
                                    settings, chain=None, prev_result=None,
                                    followup_no=0, on_stage=None,
                                    provider_factory=None):
            for name in ("extract", "solve", "compare", "diagnose"):
                await on_stage(name, {"ok": True})
            return StagedOutcome(result=validate_result(raw), model="fake/m",
                                 input_tokens=1, output_tokens=1, cost=0.01,
                                 stages={})

        created = await tasks.create_study_task(
            self.settings, "u1",
            {"task_type": "grading", "subject": "数学", "text": "",
             "asset_ids": ["a1"]}, "")
        with patch.object(staged_mod, "grade_staged", fake_grade_staged):
            await run_executor(self.settings, client)
        task = await db.get_task(self.settings.db_path, created["task_id"])
        view = await tasks.build_task_view(self.settings, task)
        return task, view["result"]

    async def test_all_correct_skips_review_with_staged_note(self):
        from tests.test_review import ReviewFakeClient

        client = ReviewFakeClient()
        task, result = await self._run_with_staged("correct", client)
        self.assertEqual(client.review_calls, 0, "无可疑题不应调用复查模型")
        summary = result["review_summary"]
        self.assertEqual(summary["state"], "not_required")
        self.assertIn("分阶段", summary["note"])
        self.assertEqual(task["status"], "done")

    async def test_wrong_question_triggers_review(self):
        from tests.test_review import ReviewFakeClient

        review_payload = {
            "reviews": [{"id": "q1", "state": "agreed", "note": "核查无异议",
                         "basis": ""}],
            "model_requested": "glm", "reported_model": "glm-5.3",
            "reported_provider": "zai",
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            "raw_excerpt": "",
        }
        client = ReviewFakeClient(review_payload=review_payload)
        task, result = await self._run_with_staged("wrong", client)
        self.assertEqual(client.review_calls, 1, "有错题应触发复查做重点核查")
        summary = result["review_summary"]
        self.assertEqual(summary["state"], "completed")
        self.assertEqual(summary["scope"], 1)
        self.assertEqual(result["questions"][0]["review"]["state"], "agreed")
        # 首轮判定不受影响
        self.assertEqual(result["questions"][0]["status"], "wrong")


if __name__ == "__main__":
    unittest.main()
