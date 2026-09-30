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
    """按剧本返回各阶段 JSON；script[stage] 是响应/异常队列。

    与真实 provider 一致地保存 cfg（分阶段按它读取厂商输出上限）。
    """

    def __init__(self, name, script, calls, cfg=None):
        self.name = name
        self.cfg = cfg
        self._script = script
        self.calls = calls
        # 上一次 extract 实际返回的题号：number_verify 无剧本时默认"复核与转写一致"
        self._last_extract_numbers: list = []

    def _respond(self, stage, system, user, n_images=0, max_tokens=0):
        self.calls.append({"stage": stage, "system": system, "user": user,
                           "n_images": n_images, "max_tokens": max_tokens,
                           "provider": self.name})
        queue = self._script.get(stage, [])
        if not queue:
            if stage == "extract_zoom":
                # 没有专门剧本时：复核无新发现（保持首轮转写不变）
                item = json.dumps({"reread": []}, ensure_ascii=False)
            elif stage == "number_verify":
                # 没有专门剧本时：复核与转写一致
                item = json.dumps({"numbers": self._last_extract_numbers},
                                  ensure_ascii=False)
            else:
                raise AssertionError(f"fake provider {self.name} 没有 {stage} 的剧本")
        else:
            item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, GradeOutcome):
            # 允许剧本直接给出 outcome（例如 finish_reason=length 的截断响应）
            return item
        if stage == "extract":
            try:
                data = json.loads(item)
                qs = data.get("questions") or data.get("new_questions") or []
                self._last_extract_numbers = [str(q.get("no", "")) for q in qs]
            except Exception:
                pass
        return GradeOutcome(text=item, input_tokens=10, output_tokens=20,
                            provider=self.name, model=f"fake-{self.name}")

    async def grade_multi(self, images, system, user, max_tokens=8000):
        if "单题复核员" in system:
            stage = "per_question_verify"
        elif "题号核对员" in system:
            stage = "number_verify"
        elif "复核员" in system:
            stage = "extract_zoom"
        else:
            stage = "extract"
        return self._respond(stage, system, user, len(images), max_tokens)

    async def complete_text(self, system, user, max_tokens=4000):
        if "解题专家" in system:
            stage = "solve"
        elif "等价性裁判" in system:
            stage = "compare"
        elif "错因诊断专家" in system:
            stage = "diagnose"
        else:
            raise AssertionError(f"无法识别阶段: {system[:40]}")
        return self._respond(stage, system, user, 0, max_tokens)


def factory_for(scripts: dict, calls: list):
    instances = {}

    def factory(name, cfg):
        # 同一 provider 复用实例：number_verify 的默认 echo 需要读到
        # 同一 fake 在 extract 阶段实际返回的题号
        if name not in instances:
            instances[name] = FakeProvider(name, scripts[name], calls, cfg)
        return instances[name]
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
        # 5 次调用各 10/20 tokens：extract + 题号复核 + solve + compare + diagnose
        #（extract_zoom 在测试里因图片非法被跳过）
        self.assertEqual(outcome.input_tokens, 50)
        self.assertEqual(outcome.output_tokens, 100)
        self.assertGreater(outcome.cost, 0)


class LenientStageOutputTest(unittest.IsolatedAsyncioTestCase):
    """模型把阶段输出字段写歪形状（页码写成数字、steps 写成单字符串）不该废掉整阶段。

    真实事故：extract 阶段 12 道题的 "page" 全部返回整数 1，
    严格模式报 12 个 validation errors，首轮失败并切备胎、备胎同样写法则整单失败。
    """

    def test_int_no_and_page_accepted(self):
        q = staged.ExtractedQuestion.model_validate(
            {"no": 1, "stem": "解方程", "student_answer": "x=4", "page": 1})
        self.assertEqual(q.no, "1")
        self.assertEqual(q.page, "1")

    def test_float_page_without_decimal_point(self):
        q = staged.ExtractedQuestion.model_validate({"no": "1", "page": 2.0})
        self.assertEqual(q.page, "2")   # 不能变成 "2.0"

    def test_string_steps_wrapped_into_list(self):
        item = staged.SolutionItem.model_validate({"no": 1, "steps": "2x=8；x=4"})
        self.assertEqual(item.no, "1")
        self.assertEqual(item.steps, ["2x=8；x=4"])

    def test_diagnosis_explanation_accepts_single_string(self):
        item = staged.DiagnosisItem.model_validate(
            {"no": 2, "error_rule": "混淆形容词与副词", "explanation": "technology 是名词"})
        self.assertEqual(item.explanation, ["technology 是名词"])

    def test_extract_json_tolerates_second_object(self):
        """结果对象后面又跟了一段 JSON：旧实现报 Extra data，整阶段失败。"""
        from app.grading import extract_json

        text = '{"questions": [{"no": "1"}]}\n{"note": "题外话"}'
        self.assertEqual(extract_json(text)["questions"][0]["no"], "1")

    def test_extract_json_tolerates_trailing_prose_in_fence(self):
        from app.grading import extract_json

        text = '转写结果：\n```json\n{"questions": []}\n```\n以上。'
        self.assertEqual(extract_json(text), {"questions": []})

    def test_extract_json_without_object_raises(self):
        from app.grading import extract_json

        with self.assertRaises(ValueError):
            extract_json("这里没有任何 JSON")

    async def test_int_page_extract_completes_pipeline(self):
        """页码写成数字的转写结果可以走完流水线，不必回落备胎。"""
        calls = []
        extract = json.dumps({"questions": [
            {"no": 1, "stem": "解方程 2x+1=9", "student_answer": "x=4", "page": 1},
        ]}, ensure_ascii=False)
        solve = json.dumps({"solutions": [
            {"no": 1, "correct_answer": "x=4", "steps": "2x=8；x=4"}]},
            ensure_ascii=False)
        scripts = {"fake": {"extract": [extract], "solve": [solve],
                            "compare": [], "diagnose": []}}
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "数学", "七年级", "", make_settings("fake"),
            provider_factory=factory_for(scripts, calls))
        q = outcome.result["questions"][0]
        # 类型写歪也能走完流水线（没有类型容错时这里会抛 StageError）
        self.assertEqual(q["no"], "1")
        self.assertEqual(q["status"], "correct")
        self.assertEqual(q["steps"], ["2x=8；x=4"])
        self.assertEqual([c["stage"] for c in calls].count("extract"), 1)


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
                   "number_verify": [ProviderError("boom")],
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
        from tests.test_tasks import FakeClient, run_executor

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


class ExtractionLogTest(unittest.TestCase):
    """format_extraction_log：转写日志人类可读，供核对 AI 是否读对。"""

    def test_first_round_renders_questions_answers_and_uncertain(self):
        from app.staged import format_extraction_log

        data = {"questions": [
            {"no": "1", "stem": "2x=8，x=?", "student_answer": "5",
             "handwriting_uncertain": False, "uncertain_note": ""},
            {"no": "2", "stem": "填空", "student_answer": "",
             "handwriting_uncertain": True, "uncertain_note": "笔迹潦草无法辨认"},
        ]}
        text = format_extraction_log(data)
        self.assertIn("共提取 2 题", text)
        self.assertIn("题1", text)
        self.assertIn("学生答案：5", text)
        self.assertIn("【字迹存疑】", text)
        self.assertIn("笔迹潦草无法辨认", text)
        self.assertIn("（未作答/空白）", text)

    def test_followup_renders_revisions_and_new_questions(self):
        from app.staged import format_extraction_log

        data = {"revisions": [{"prev_no": "3", "student_answer": "7",
                               "note": "改后答案"}],
                "new_questions": [{"no": "4", "stem": "新题",
                                   "student_answer": "A"}]}
        text = format_extraction_log(data)
        self.assertIn("订正 1 题", text)
        self.assertIn("新增 1 题", text)
        self.assertIn("题3", text)
        self.assertIn("题4", text)

    def test_empty_extraction(self):
        from app.staged import format_extraction_log

        self.assertIn("空转写", format_extraction_log({"questions": []}))


class StagedStagesViewTest(unittest.IsolatedAsyncioTestCase):
    """任务视图暴露各阶段产出：runs[].stages.extract 可查 AI 转写。"""

    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path

        from app import db
        from tests.test_tasks import make_settings as task_settings
        from tests.test_tasks import seed_asset, seed_user
        from tests.test_workspace import png_bytes

        self.tmp = tempfile.TemporaryDirectory()
        overrides = {
            "llm": {"default_provider": "fake", "providers": {
                "fake": {"base_url": "http://fake", "api_key": "k",
                         "model": "fake-m"}}},
            "staged_grading": {"enabled": True},
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

    async def test_view_exposes_extract_transcription_and_logs_it(self):
        import copy
        from unittest.mock import patch

        from app import db, tasks, staged as staged_mod
        from app.staged import StagedOutcome
        from tests.test_tasks import FakeClient, run_executor

        raw = {
            "schema_version": 3, "task_type": "grading", "subject": "数学",
            "grade_level": "七年级",
            "overview": {"checked_questions": 1, "summary": "一批改"},
            "questions": [{
                "id": "q1", "no": "1", "stem": "2x=8，x=?",
                "student_answer": "4", "status": "correct",
                "correct_answer": "4", "steps": ["2x=8", "x=4"],
                "error_rule": "", "knowledge_point": "一元一次方程",
                "review": {"state": "not_applicable", "note": "", "basis": ""},
                "final_decision": "kept_correct",
                "remediation": {"state": "not_applicable", "updated_date": "",
                                "linked_training": "", "note": ""},
            }],
            "review_summary": {"state": "not_run", "scope": 0, "disagreed": 0,
                               "unverified": 0, "note": ""},
            "archive": {"action": "none"},
        }

        async def fake_grade_staged(images, subject, grade_level, input_text,
                                    settings, chain=None, prev_result=None,
                                    followup_no=0, on_stage=None,
                                    provider_factory=None):
            # 模拟真实提取产出经 on_stage 落库
            await on_stage("extract", {"questions": [
                {"no": "1", "stem": "2x=8，x=?", "student_answer": "5",
                 "page": "1", "handwriting_uncertain": False,
                 "uncertain_note": ""}]})
            for name in ("solve", "compare", "diagnose"):
                await on_stage(name, {"ok": True})
            return StagedOutcome(result=validate_result(copy.deepcopy(raw)),
                                 model="fake/m", input_tokens=1,
                                 output_tokens=1, cost=0.01, stages={})

        created = await tasks.create_study_task(
            self.settings, "u1",
            {"task_type": "grading", "subject": "数学", "text": "",
             "asset_ids": ["a1"]}, "")
        with patch.object(staged_mod, "grade_staged", fake_grade_staged):
            with self.assertLogs("app.tasks", level="INFO") as logs:
                await run_executor(self.settings, FakeClient())
        transcript_logs = [m for m in logs.output if "提取阶段转写" in m]
        self.assertTrue(transcript_logs, "提取阶段应输出转写日志")
        self.assertIn("学生答案：5", transcript_logs[0])

        task = await db.get_task(self.settings.db_path, created["task_id"])
        view = await tasks.build_task_view(self.settings, task)
        run_view = view["runs"][0]
        self.assertEqual(run_view["stage"], "done")
        extract = run_view["stages"]["extract"]
        self.assertEqual(extract["questions"][0]["student_answer"], "5")
        self.assertEqual(extract["questions"][0]["stem"], "2x=8，x=?")


def _tiny_jpeg(w=400, h=300, color=(255, 255, 255)) -> bytes:
    from PIL import Image, ImageDraw
    import io
    image = Image.new("RGB", (w, h), color)
    draw = ImageDraw.Draw(image)
    # Orientation OSD needs multiple text lines; a blank JPEG is intentionally
    # rejected by the new fail-closed direction gate.
    for row in range(8):
        draw.text((8, 8 + row * 20), f"Question {row + 1}: sample text",
                  fill=(0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


ZOOM_REREAD_FOUND = json.dumps({"reread": [
    {"no": "3", "student_answer": "a²+2ab+b²",
     "handwriting_uncertain": False, "uncertain_note": ""},
]}, ensure_ascii=False)


class ExtractZoomTest(unittest.IsolatedAsyncioTestCase):
    """提取阶段局部放大复核：触发条件、合并逻辑、跳过条件。"""

    def setUp(self):
        self.calls = []
        self.img = [(_tiny_jpeg(), "image/jpeg")]

    def _settings(self, **over):
        s = make_settings("fake")
        for k, v in over.items():
            setattr(s.staged_grading, k, v)
        return s

    def _scripts(self, **over):
        scripts = {"fake": {"extract": [EXTRACT_OK]}}
        scripts["fake"].update(over)
        return scripts

    async def _extract(self, scripts, settings):
        return await staged.extract_stage(
            self.img, "数学", "七年级", "", settings, ["fake"],
            provider_factory=factory_for(scripts, self.calls))

    async def test_zoom_merges_reread_into_transcript(self):
        # EXTRACT_OK 的第 3 题字迹存疑 → 复核找回答案 → 合并后不再存疑
        scripts = self._scripts(extract_zoom=[ZOOM_REREAD_FOUND])
        parsed, calls, _ = await self._extract(scripts, self._settings())
        stages = [c["stage"] for c in self.calls]
        self.assertIn("extract_zoom", stages)
        q3 = {q.no: q for q in parsed.questions}["3"]
        self.assertEqual(q3.student_answer, "a²+2ab+b²")
        self.assertFalse(q3.handwriting_uncertain)
        # 复核调用确实带了局部图（原图 1 张 + 2x2=4 张局部）
        zoom_call = [c for c in self.calls if c["stage"] == "extract_zoom"][0]
        self.assertEqual(zoom_call["n_images"], 5)

    async def test_zoom_skipped_when_disabled(self):
        scripts = self._scripts()
        parsed, calls, _ = await self._extract(
            scripts, self._settings(extract_zoom_reread=False))
        stages = [c["stage"] for c in self.calls]
        self.assertNotIn("extract_zoom", stages)
        # 未复核：第 3 题保持存疑原样
        q3 = {q.no: q for q in parsed.questions}["3"]
        self.assertTrue(q3.handwriting_uncertain)

    async def test_zoom_skipped_when_nothing_uncertain(self):
        certain = json.dumps({"questions": [
            {"no": "1", "stem": "1+1=?", "student_answer": "2",
             "page": "1", "handwriting_uncertain": False, "uncertain_note": ""},
        ]}, ensure_ascii=False)
        scripts = self._scripts(extract=[certain])
        await self._extract(scripts, self._settings())
        stages = [c["stage"] for c in self.calls]
        self.assertNotIn("extract_zoom", stages)

    async def test_zoom_skipped_when_too_many_images(self):
        scripts = self._scripts()
        imgs = self.img * 3
        await staged.extract_stage(
            imgs, "数学", "七年级", "", self._settings(), ["fake"],
            provider_factory=factory_for(scripts, self.calls))
        stages = [c["stage"] for c in self.calls]
        self.assertNotIn("extract_zoom", stages)

    async def test_zoom_keeps_uncertain_when_still_illegible(self):
        # 复核依然看不清 → 保持存疑，不编答案
        scripts = self._scripts()  # 默认空 reread
        parsed, calls, _ = await self._extract(scripts, self._settings())
        q3 = {q.no: q for q in parsed.questions}["3"]
        self.assertTrue(q3.handwriting_uncertain)
        self.assertEqual(q3.student_answer, "")

    async def test_preprocessed_image_sent_to_model(self):
        # 预处理不改变题数；图片仍是有效 JPEG（fail-open 不阻断）
        scripts = self._scripts()
        parsed, calls, _ = await self._extract(scripts, self._settings())
        self.assertEqual(len(parsed.questions), 3)

    async def test_zoom_accepts_prefixed_no(self):
        """模型把题号写成「题3」这类包装时，唯一映射仍能合并，不再整阶段失败。"""
        for raw_no in ("题3", "第3题", "No.3"):
            with self.subTest(no=raw_no):
                self.calls.clear()
                item = json.dumps({"reread": [
                    {"no": raw_no, "student_answer": "a²+2ab+b²",
                     "handwriting_uncertain": False, "uncertain_note": ""}]},
                    ensure_ascii=False)
                scripts = self._scripts(extract_zoom=[item])
                parsed, calls, _ = await self._extract(scripts, self._settings())
                q3 = {q.no: q for q in parsed.questions}["3"]
                self.assertEqual(q3.student_answer, "a²+2ab+b²")
                self.assertFalse(q3.handwriting_uncertain)
                self.assertNotIn("放大复核", parsed.zoom_note)

    async def test_zoom_ignores_unknown_no_without_failing(self):
        """复核返回未送审的题号：忽略并记录，不阻断首轮结果。"""
        item = json.dumps({"reread": [
            {"no": "题9", "student_answer": "x", "handwriting_uncertain": False,
             "uncertain_note": ""},
            {"no": "题3", "student_answer": "a²+2ab+b²", "handwriting_uncertain": False,
             "uncertain_note": ""}]}, ensure_ascii=False)
        scripts = self._scripts(extract_zoom=[item])
        parsed, calls, _ = await self._extract(scripts, self._settings())
        q3 = {q.no: q for q in parsed.questions}["3"]
        # 合法的那条照样合并
        self.assertEqual(q3.student_answer, "a²+2ab+b²")
        self.assertIn("无法匹配", parsed.zoom_note)
        self.assertIn("题9", parsed.zoom_note)

    async def test_zoom_duplicate_no_keeps_first(self):
        """同一题号重复返回：只取首次，后者不覆盖。"""
        item = json.dumps({"reread": [
            {"no": "3", "student_answer": "首次", "handwriting_uncertain": False,
             "uncertain_note": ""},
            {"no": "题3", "student_answer": "第二次", "handwriting_uncertain": False,
             "uncertain_note": ""}]}, ensure_ascii=False)
        scripts = self._scripts(extract_zoom=[item])
        parsed, calls, _ = await self._extract(scripts, self._settings())
        q3 = {q.no: q for q in parsed.questions}["3"]
        self.assertEqual(q3.student_answer, "首次")
        self.assertIn("重复题号", parsed.zoom_note)

    async def test_zoom_failure_keeps_transcript_and_marks_blank_uncertain(self):
        """复核耗尽备胎：不抛错、保留首轮转写；判空题降级为存疑并写明原因。"""
        blank_first = json.dumps({"questions": [
            {"no": "1", "stem": "题一", "student_answer": "", "page": "1",
             "handwriting_uncertain": False, "uncertain_note": ""},
            {"no": "2", "stem": "题二", "student_answer": "b", "page": "1",
             "handwriting_uncertain": True, "uncertain_note": "字迹潦草"},
        ]}, ensure_ascii=False)
        scripts = {"fake": {"extract": [blank_first],
                            "extract_zoom": [ProviderError("boom")]}}
        parsed, calls, _ = await self._extract(scripts, self._settings())
        by_no = {q.no: q for q in parsed.questions}
        # 判空题：复核没跑成 → 不能据此认定"学生没作答"，降级为存疑
        self.assertTrue(by_no["1"].handwriting_uncertain)
        self.assertIn("无法确认是未作答还是漏读", by_no["1"].uncertain_note)
        # 原本存疑的题保持原样
        self.assertTrue(by_no["2"].handwriting_uncertain)
        self.assertEqual(by_no["2"].uncertain_note, "字迹潦草")
        # 阶段记录里如实说明，不只留一行服务器日志
        self.assertIn("放大复核未完成", parsed.zoom_note)
        # 复核没有成功产出：不产生可计费的阶段调用记录
        #（题号复核在 extract 与 zoom 之间，默认 echo 与转写一致）
        self.assertEqual([c["stage"] for c in self.calls],
                         ["extract", "number_verify", "extract_zoom"])


class StageProviderCapTest(unittest.IsolatedAsyncioTestCase):
    """厂商输出上限与截断识别：避免备胎一调用就被 max_tokens 400 打死。"""

    def setUp(self):
        self.calls = []
        self.img = [(_tiny_jpeg(), "image/jpeg")]

    async def test_provider_cap_limits_stage_max_tokens(self):
        settings = make_settings("fake")
        settings.llm.providers["fake"].max_output_tokens = 1024
        scripts = {"fake": {"extract": [EXTRACT_OK], "solve": [SOLVE_OK],
                            "compare": [COMPARE_OK], "diagnose": [DIAGNOSE_OK]}}
        await staged.grade_staged(
            self.img, "数学", "七年级", "", settings,
            provider_factory=factory_for(scripts, self.calls))
        extract_call = [c for c in self.calls if c["stage"] == "extract"][0]
        solve_call = [c for c in self.calls if c["stage"] == "solve"][0]
        # 阶段上限 8000/6000 都被压到厂商上限 1024
        self.assertEqual(extract_call["max_tokens"], 1024)
        self.assertEqual(solve_call["max_tokens"], 1024)

    async def test_stage_max_tokens_without_cap_uses_stage_limit(self):
        settings = make_settings("fake")
        scripts = {"fake": {"extract": [EXTRACT_OK], "solve": [SOLVE_OK],
                            "compare": [COMPARE_OK], "diagnose": [DIAGNOSE_OK]}}
        await staged.grade_staged(
            self.img, "数学", "七年级", "", settings,
            provider_factory=factory_for(scripts, self.calls))
        extract_call = [c for c in self.calls if c["stage"] == "extract"][0]
        self.assertEqual(extract_call["max_tokens"],
                         settings.staged_grading.extract_max_tokens)

    async def test_truncated_output_switches_to_next_provider(self):
        """finish_reason=length 不能当成功：换备胎重试。"""
        settings = make_settings("fake", "backup")
        truncated = GradeOutcome(text="{", input_tokens=5, output_tokens=1024,
                                 provider="fake", model="fake-fake",
                                 finish_reason="length")
        scripts = {"fake": {"extract": [truncated]},
                   "backup": {"extract": [EXTRACT_OK]}}

        async def call(provider):
            return await provider.grade_multi([(b"x", "image/jpeg")], "s", "u")

        parsed, outcome, cost = await staged._run_stage(
            "extract", staged.ExtractionResult, ["fake", "backup"], settings,
            call, None, factory_for(scripts, self.calls))
        self.assertEqual(outcome.provider, "backup")
        self.assertEqual(len(parsed.questions), 3)

    async def test_truncation_retries_same_provider_with_boost_first(self):
        """截断后先在同一模型放大额度重试一次，而不是直接切备胎。"""
        settings = make_settings("fake", "backup")
        truncated = GradeOutcome(text="{", input_tokens=5, output_tokens=8000,
                                 provider="fake", model="fake-fake",
                                 finish_reason="length")
        scripts = {"fake": {"extract": [truncated, EXTRACT_OK]},
                   "backup": {"extract": [EXTRACT_OK]}}

        async def call(provider, max_tokens_want=0):
            return await provider.grade_multi(
                [(b"x", "image/jpeg")], "s", "u",
                max_tokens=max_tokens_want or 8000)

        parsed, outcome, cost = await staged._run_stage(
            "extract", staged.ExtractionResult, ["fake", "backup"], settings,
            call, None, factory_for(scripts, self.calls),
            base_max_tokens=8000)
        self.assertEqual(outcome.provider, "fake")
        self.assertEqual(len(parsed.questions), 3)
        fake_calls = [c for c in self.calls if c["provider"] == "fake"]
        self.assertEqual(len(fake_calls), 2)
        self.assertEqual(fake_calls[0]["max_tokens"], 8000)
        self.assertEqual(fake_calls[1]["max_tokens"], 16000)  # 2.0 倍
        self.assertFalse([c for c in self.calls if c["provider"] == "backup"])

    async def test_truncation_retry_still_truncated_switches_backup(self):
        """放大重试后依然截断：才切备胎，且同一模型只重试一次。"""
        settings = make_settings("fake", "backup")
        truncated = GradeOutcome(text="{", input_tokens=5, output_tokens=8000,
                                 provider="fake", model="fake-fake",
                                 finish_reason="length")
        scripts = {"fake": {"extract": [truncated, truncated]},
                   "backup": {"extract": [EXTRACT_OK]}}

        async def call(provider, max_tokens_want=0):
            return await provider.grade_multi(
                [(b"x", "image/jpeg")], "s", "u",
                max_tokens=max_tokens_want or 8000)

        parsed, outcome, cost = await staged._run_stage(
            "extract", staged.ExtractionResult, ["fake", "backup"], settings,
            call, None, factory_for(scripts, self.calls),
            base_max_tokens=8000)
        self.assertEqual(outcome.provider, "backup")
        fake_calls = [c for c in self.calls if c["provider"] == "fake"]
        self.assertEqual(len(fake_calls), 2)  # 首试 + 一次放大重试，不多试

    async def test_truncation_retry_disabled_falls_back_directly(self):
        """truncation_retry_multiplier<=1 时关闭放大重试，保持旧行为。"""
        settings = make_settings("fake", "backup")
        settings.staged_grading.truncation_retry_multiplier = 0.0
        truncated = GradeOutcome(text="{", input_tokens=5, output_tokens=8000,
                                 provider="fake", model="fake-fake",
                                 finish_reason="length")
        scripts = {"fake": {"extract": [truncated, EXTRACT_OK]},
                   "backup": {"extract": [EXTRACT_OK]}}

        async def call(provider, max_tokens_want=0):
            return await provider.grade_multi(
                [(b"x", "image/jpeg")], "s", "u",
                max_tokens=max_tokens_want or 8000)

        parsed, outcome, cost = await staged._run_stage(
            "extract", staged.ExtractionResult, ["fake", "backup"], settings,
            call, None, factory_for(scripts, self.calls),
            base_max_tokens=8000)
        self.assertEqual(outcome.provider, "backup")
        self.assertEqual(len([c for c in self.calls if c["provider"] == "fake"]), 1)


class BlankSplitTest(unittest.TestCase):
    def test_split_numbered_with_separator(self):
        self.assertEqual(staged.split_answer_blanks("1.B 2.C 3.A"),
                         [("1", "B"), ("2", "C"), ("3", "A")])

    def test_split_numbered_multiline(self):
        self.assertEqual(staged.split_answer_blanks("1. F\n2. B\n3. C"),
                         [("1", "F"), ("2", "B"), ("3", "C")])

    def test_split_compact(self):
        self.assertEqual(staged.split_answer_blanks("11F 12B 13C"),
                         [("11", "F"), ("12", "B"), ("13", "C")])

    def test_split_strips_trailing_note(self):
        self.assertEqual(staged.split_answer_blanks("1.F 2.B 3.C 4.E 5.A（D多余）"),
                         [("1", "F"), ("2", "B"), ("3", "C"), ("4", "E"), ("5", "A")])

    def test_split_single_answer_returns_empty(self):
        self.assertEqual(staged.split_answer_blanks("x=4"), [])
        self.assertEqual(staged.split_answer_blanks("creatively"), [])
        self.assertEqual(staged.split_answer_blanks(""), [])

    def test_pair_aligned_numbers(self):
        self.assertEqual(staged.pair_blanks("B\nC\nA", "1.B 2.C 3.D"),
                         [("1", "B", "B"), ("2", "C", "C"), ("3", "A", "D")])

    def test_pair_unanswered_expands_by_correct(self):
        self.assertEqual(staged.pair_blanks("", "1.B 2.C"),
                         [("1", "", "B"), ("2", "", "C")])

    def test_pair_unsolvable_expands_by_student(self):
        self.assertEqual(staged.pair_blanks("11F 12B", ""),
                         [("11", "F", ""), ("12", "B", "")])

    def test_pair_mismatched_numbers_returns_none(self):
        self.assertIsNone(staged.pair_blanks("1.F 2.B", "11.F 12.B 13.C"))

    def test_pair_single_line_student_not_guessed(self):
        # 单行空格分隔的学生答案不做位置猜测：回退整题
        self.assertIsNone(staged.pair_blanks("B C A", "1.B 2.C 3.D"))


class UndeterminableTest(unittest.IsolatedAsyncioTestCase):
    """修1：solve 判无法求解 → 整题 uncertain，不进 compare 模型裁决、不进诊断。"""

    def setUp(self):
        self.calls = []
        extract = json.dumps({"questions": [
            {"no": "1", "stem": "解方程 2x+1=9", "student_answer": "x=4",
             "page": "1", "handwriting_uncertain": False, "uncertain_note": ""},
            {"no": "2", "stem": "（题目内容未在图片中显示）",
             "student_answer": "A", "page": "1",
             "handwriting_uncertain": False, "uncertain_note": ""},
        ]}, ensure_ascii=False)
        solve = json.dumps({"solutions": [
            {"no": "1", "correct_answer": "x=4", "steps": ["2x=8"]},
            {"no": "2", "correct_answer": "", "undeterminable": True,
             "steps": ["题干未在图片中显示，无法求解"]},
        ]}, ensure_ascii=False)
        self.scripts = {"fake": {"extract": [extract], "solve": [solve],
                                 "compare": [], "diagnose": []}}
        self.settings = make_settings("fake")

    async def _grade(self):
        return await staged.grade_staged(
            [(b"img", "image/jpeg")], "数学", "七年级", "", self.settings,
            provider_factory=factory_for(self.scripts, self.calls))

    async def test_undeterminable_is_uncertain_not_wrong(self):
        outcome = await self._grade()
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(by_no["1"]["status"], "correct")
        self.assertEqual(by_no["2"]["status"], "uncertain")
        self.assertNotEqual(by_no["2"]["status"], "wrong")
        # 未进诊断：没有错因
        self.assertEqual(by_no["2"]["error_rule"], "")
        self.assertEqual(by_no["2"]["correct_answer"], "")

    async def test_compare_model_not_called_for_undeterminable(self):
        await self._grade()
        compare_calls = [c for c in self.calls if c["stage"] == "compare"]
        self.assertEqual(compare_calls, [])

    async def test_missing_info_asks_for_stem(self):
        outcome = await self._grade()
        missing = outcome.result["missing_info"]
        self.assertTrue(any("「2」" in m and "未批改" in m for m in missing),
                        f"missing_info={missing}")

    async def test_summary_counts_unsolvable(self):
        outcome = await self._grade()
        summary = outcome.result["overview"]["summary"]
        self.assertIn("共检查 2 题", summary)
        self.assertIn("题干缺失未判定 1 题", summary)


class PerBlankTest(unittest.IsolatedAsyncioTestCase):
    """修4：多空题按小题展开，统计/诊断/台账键都按小题。"""

    def setUp(self):
        self.calls = []
        extract = json.dumps({"questions": [
            {"no": "1", "stem": "完形填空 passage…", "student_answer": "B\nC\nA",
             "page": "1", "handwriting_uncertain": False, "uncertain_note": ""},
        ]}, ensure_ascii=False)
        solve = json.dumps({"solutions": [
            {"no": "1", "correct_answer": "1.B 2.C 3.D", "steps": ["第3空…"]},
        ]}, ensure_ascii=False)
        compare = json.dumps({"judgments": [{"no": "1-3", "equivalent": False}]},
                             ensure_ascii=False)
        diagnose = json.dumps({"diagnoses": [
            {"no": "1-3", "error_rule": "第3空误选A",
             "knowledge_point": "KP", "explanation": ["应选D"],
             "correct_answer": "D"},
        ]}, ensure_ascii=False)
        self.scripts = {"fake": {"extract": [extract], "solve": [solve],
                                 "compare": [compare], "diagnose": [diagnose]}}
        self.settings = make_settings("fake")

    async def test_sub_questions_and_counts(self):
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "英语", "七年级", "", self.settings,
            provider_factory=factory_for(self.scripts, self.calls))
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(set(by_no), {"1-1", "1-2", "1-3"})
        self.assertEqual(by_no["1-1"]["status"], "correct")
        self.assertEqual(by_no["1-2"]["status"], "correct")
        self.assertEqual(by_no["1-3"]["status"], "wrong")
        # 诊断挂在错的小题上
        self.assertIn("第3空", by_no["1-3"]["error_rule"])
        self.assertEqual(by_no["1-3"]["student_answer"], "A")
        self.assertEqual(by_no["1-3"]["correct_answer"], "D")
        # 统计按小题
        ov = outcome.result["overview"]
        self.assertEqual(ov["checked_questions"], 3)
        self.assertIn("共检查 3 题，答对 2 题，答错 1 题", ov["summary"])
        # id 按小题区分；uid 由下游 fill_question_uids 按题号回填
        ids = [q["id"] for q in outcome.result["questions"]]
        self.assertEqual(len(set(ids)), 3)
        from app.schemas import fill_question_uids
        filled = fill_question_uids(outcome.result, "2026-09-29")
        uids = [q["uid"] for q in filled["questions"]]
        self.assertEqual(len(set(uids)), 3)

    async def test_diagnose_called_with_sub_id(self):
        await staged.grade_staged(
            [(b"img", "image/jpeg")], "英语", "七年级", "", self.settings,
            provider_factory=factory_for(self.scripts, self.calls))
        diag_calls = [c for c in self.calls if c["stage"] == "diagnose"]
        self.assertEqual(len(diag_calls), 1)
        self.assertIn('"1-3"', diag_calls[0]["user"])


class AttributionDedupTest(unittest.IsolatedAsyncioTestCase):
    """修2：同一组答案不许归属到两个题号。"""

    def _grade(self, extract_questions, solve_solutions, calls):
        scripts = {"fake": {"extract": [json.dumps({"questions": extract_questions},
                                                   ensure_ascii=False)],
                            "solve": [json.dumps({"solutions": solve_solutions},
                                                 ensure_ascii=False)],
                            "compare": [], "diagnose": []}}
        settings = make_settings("fake")
        return staged.grade_staged(
            [(b"img", "image/jpeg")], "英语", "七年级", "", settings,
            provider_factory=factory_for(scripts, calls))

    async def test_phantom_with_missing_stem_is_uncertain(self):
        calls = []
        outcome = await self._grade(
            [{"no": "二", "stem": "选词填空 passage", "student_answer": "1. F\n2. B\n3. C",
              "page": "1", "handwriting_uncertain": False, "uncertain_note": ""},
             {"no": "三", "stem": "（题目内容未在图片中显示）",
              "student_answer": "11F 12B 13C", "page": "1",
              "handwriting_uncertain": False, "uncertain_note": ""}],
            [{"no": "二", "correct_answer": "1.F 2.B 3.C", "steps": []},
             {"no": "三", "correct_answer": "", "undeterminable": True,
              "steps": ["题干缺失"]}],
            calls)
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        # "二"按小题展开且全对
        self.assertEqual(by_no["二-1"]["status"], "correct")
        self.assertEqual(by_no["二-2"]["status"], "correct")
        self.assertEqual(by_no["二-3"]["status"], "correct")
        # "三"是借用答案的幽灵题：uncertain，不判 wrong
        self.assertEqual(by_no["三"]["status"], "uncertain")
        missing = outcome.result["missing_info"]
        self.assertTrue(any("「三」" in m and "归属" in m for m in missing),
                        f"missing_info={missing}")

    async def test_both_real_stems_only_warns(self):
        calls = []
        outcome = await self._grade(
            [{"no": "4", "stem": "题干4", "student_answer": "A\nB",
              "page": "1", "handwriting_uncertain": False, "uncertain_note": ""},
             {"no": "5", "stem": "题干5", "student_answer": "A\nB",
              "page": "1", "handwriting_uncertain": False, "uncertain_note": ""}],
            [{"no": "4", "correct_answer": "1.A 2.B", "steps": []},
             {"no": "5", "correct_answer": "1.A 2.B", "steps": []}],
            calls)
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        # 都有题干：不自动改判，只提示人工核对
        self.assertEqual(by_no["4-1"]["status"], "correct")
        self.assertEqual(by_no["5-1"]["status"], "correct")
        missing = outcome.result["missing_info"]
        self.assertTrue(any("核对答案归属" in m for m in missing),
                        f"missing_info={missing}")


class ProductionIncidentRegressionTest(unittest.IsolatedAsyncioTestCase):
    """生产事故回归：2026-09-29 dfae3eed3b804094。

    完形 1-10 全对、选词填空 5 空全对；extract_zoom 把选词填空的答案又填进
    了题干缺失的"三、短文填空"。期望：15 小题全对，"三"标存疑（归属存疑），
    不判 wrong、不进诊断、不进错题台账口径。
    """

    async def test_full_paper(self):
        calls = []
        extract = json.dumps({"questions": [
            {"no": "1", "stem": "完形填空 passage…",
             "student_answer": "B\nC\nA\nA\nB\nB\nB\nA\nB\nA",
             "page": "1", "handwriting_uncertain": False, "uncertain_note": ""},
            {"no": "二", "stem": "选词填空 passage…",
             "student_answer": "1. F\n2. B\n3. C\n4. E\n5. A",
             "page": "1", "handwriting_uncertain": False, "uncertain_note": ""},
            {"no": "三", "stem": "（题目内容未在图片中显示）",
             "student_answer": "11F 12B 13C 14E 15A",
             "page": "1", "handwriting_uncertain": False, "uncertain_note": ""},
        ]}, ensure_ascii=False)
        solve = json.dumps({"solutions": [
            {"no": "1", "correct_answer": "1.B 2.C 3.A 4.A 5.B 6.B 7.B 8.A 9.B 10.A",
             "steps": []},
            {"no": "二", "correct_answer": "1.F 2.B 3.C 4.E 5.A", "steps": []},
            {"no": "三", "correct_answer": "", "undeterminable": True,
             "steps": ["题干未在图片中显示，无法求解"]},
        ]}, ensure_ascii=False)
        scripts = {"fake": {"extract": [extract], "solve": [solve],
                             "compare": [], "diagnose": []}}
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "英语", "七年级", "", make_settings("fake"),
            provider_factory=factory_for(scripts, calls))
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        # 15 个小题全对
        for i in range(1, 11):
            self.assertEqual(by_no[f"1-{i}"]["status"], "correct")
        for i in range(1, 6):
            self.assertEqual(by_no[f"二-{i}"]["status"], "correct")
        # "三"是幽灵题：存疑，不判错
        self.assertEqual(by_no["三"]["status"], "uncertain")
        self.assertEqual(by_no["三"]["error_rule"], "")
        # 诊断/模型比对都不该被调用（全是确定性直判）
        self.assertEqual([c for c in calls if c["stage"] == "compare"], [])
        self.assertEqual([c for c in calls if c["stage"] == "diagnose"], [])
        # 统计口径
        ov = outcome.result["overview"]
        self.assertEqual(ov["checked_questions"], 16)
        self.assertIn("答对 15 题", ov["summary"])
        self.assertIn("答案归属存疑 1 题", ov["summary"])
        # 补充材料入口有内容
        missing = outcome.result["missing_info"]
        self.assertTrue(any("「三」" in m for m in missing), f"missing_info={missing}")
