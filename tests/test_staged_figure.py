"""配图识别：数学几何题转写后看原图写出图形描述，供求解与示意图重绘使用。"""
from __future__ import annotations

import json
import unittest

from app import diagram, staged
from app.hermes import validate_result
from app.providers import ProviderError
from tests.test_diagram import RECT_JSON, SequenceProvider, outcome
from tests.test_staged import factory_for, make_settings

STEM = ("20. 甲、乙两张正方形纸片的边长分别为a、b，a+b=12。图1中两纸片底边共线并排放置，"
        "H为AE的中点，连接DH、FH。")
FIGURE = "图1：甲在左、乙在右，底边共线；A 为甲左下顶点，E 为乙右下顶点；阴影为△DHF。"

GEOMETRY_EXTRACT = json.dumps({"questions": [
    {"no": "20(1)", "stem": STEM + "(1) 求a²+b²。", "student_answer": "80", "page": "1"},
    {"no": "20(2)", "stem": STEM + "(2) 求图1的阴影面积。", "student_answer": "8", "page": "1"},
    {"no": "21", "stem": "解方程 2x+1=9", "student_answer": "x=4", "page": "1"},
]}, ensure_ascii=False)

GEOMETRY_SOLVE = json.dumps({"solutions": [
    {"no": "20(1)", "correct_answer": "80", "steps": ["(a+b)²-2ab"]},
    {"no": "20(2)", "correct_answer": "8", "steps": ["按配图计算"]},
    {"no": "21", "correct_answer": "x=4", "steps": ["2x=8"]},
]}, ensure_ascii=False)

FIGURE_OK = json.dumps({"figures": [{"no": "20(1)", "figure": FIGURE}]}, ensure_ascii=False)


class FigureExtractTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.settings = make_settings("fake")

    async def grade(self, script, subject="数学", settings=None, **kw):
        return await staged.grade_staged(
            [(b"img", "image/jpeg")], subject, "七年级", "", settings or self.settings,
            provider_factory=factory_for({"fake": script}, self.calls), **kw)

    def stage_calls(self, stage):
        return [c for c in self.calls if c["stage"] == stage]

    async def test_figure_reaches_solve_and_result(self):
        outcome_ = await self.grade({"extract": [GEOMETRY_EXTRACT], "figure": [FIGURE_OK],
                                     "solve": [GEOMETRY_SOLVE]})
        figure_calls = self.stage_calls("figure")
        self.assertEqual(len(figure_calls), 1)
        self.assertEqual(figure_calls[0]["n_images"], 1)
        # 同一大题的小题只识别一次；纯代数题不送识别
        self.assertIn('no="20(1)"', figure_calls[0]["user"])
        self.assertNotIn('no="20(2)"', figure_calls[0]["user"])
        self.assertNotIn('no="21"', figure_calls[0]["user"])

        solve_user = self.stage_calls("solve")[0]["user"]
        items = json.loads(solve_user.split("```json\n")[1].split("\n```")[0])
        by_no = {i["no"]: i for i in items}
        self.assertEqual(by_no["20(1)"]["figure"], FIGURE)
        self.assertEqual(by_no["20(2)"]["figure"], FIGURE)  # 小题共用大题配图
        self.assertNotIn("figure", by_no["21"])
        self.assertTrue(all(set(i) <= {"no", "stem", "figure"} for i in items))

        qs = {q["no"]: q for q in outcome_.result["questions"]}
        self.assertEqual(qs["20(1)"]["figure"], FIGURE)
        self.assertEqual(qs["20(2)"]["figure"], FIGURE)
        self.assertEqual(qs["21"]["figure"], "")
        self.assertIn("配图识别完成", outcome_.stages["extract"]["zoom_note"])

    async def test_non_math_subject_skips_figure(self):
        await self.grade({"extract": [GEOMETRY_EXTRACT], "solve": [GEOMETRY_SOLVE]},
                         subject="英语")
        self.assertEqual(self.stage_calls("figure"), [])

    async def test_empty_subject_still_detects_geometry(self):
        await self.grade({"extract": [GEOMETRY_EXTRACT], "figure": [FIGURE_OK],
                          "solve": [GEOMETRY_SOLVE]}, subject="")
        self.assertEqual(len(self.stage_calls("figure")), 1)

    async def test_algebra_only_math_paper_skips_figure(self):
        extract = json.dumps({"questions": [
            {"no": "1", "stem": "解方程 2x+1=9", "student_answer": "x=4", "page": "1"}]},
            ensure_ascii=False)
        solve = json.dumps({"solutions": [
            {"no": "1", "correct_answer": "x=4", "steps": ["2x=8"]}]}, ensure_ascii=False)
        await self.grade({"extract": [extract], "solve": [solve]})
        self.assertEqual(self.stage_calls("figure"), [])

    async def test_failure_degrades_to_text_only(self):
        outcome_ = await self.grade({"extract": [GEOMETRY_EXTRACT],
                                     "figure": [ProviderError("boom")],
                                     "solve": [GEOMETRY_SOLVE]})
        solve_user = self.stage_calls("solve")[0]["user"]
        self.assertNotIn('"figure"', solve_user)
        self.assertTrue(all(q["figure"] == "" for q in outcome_.result["questions"]))
        self.assertIn("配图识别未完成", outcome_.stages["extract"]["zoom_note"])

    async def test_unknown_numbers_and_overlong_text_are_contained(self):
        reply = json.dumps({"figures": [
            {"no": "99", "figure": "不存在的题"},
            {"no": "题20(1)", "figure": "长" * 2000}]}, ensure_ascii=False)
        outcome_ = await self.grade({"extract": [GEOMETRY_EXTRACT], "figure": [reply],
                                     "solve": [GEOMETRY_SOLVE]})
        q = {q["no"]: q for q in outcome_.result["questions"]}["20(1)"]
        self.assertEqual(len(q["figure"]), staged._FIGURE_MAX_CHARS)
        self.assertIn("无法匹配", outcome_.stages["extract"]["zoom_note"])

    async def test_too_many_geometry_questions_skips(self):
        self.settings.staged_grading.figure_max_items = 1
        extract = json.dumps({"questions": [
            {"no": "1", "stem": "如图，求阴影面积", "student_answer": "4", "page": "1"},
            {"no": "2", "stem": "如图，求三角形面积", "student_answer": "6", "page": "1"}]},
            ensure_ascii=False)
        solve = json.dumps({"solutions": [
            {"no": "1", "correct_answer": "4", "steps": ["s"]},
            {"no": "2", "correct_answer": "6", "steps": ["s"]}]}, ensure_ascii=False)
        outcome_ = await self.grade({"extract": [extract], "solve": [solve]})
        self.assertEqual(self.stage_calls("figure"), [])
        self.assertIn("配图识别跳过", outcome_.stages["extract"]["zoom_note"])

    async def test_disabled_by_config(self):
        self.settings.staged_grading.figure_extract = False
        await self.grade({"extract": [GEOMETRY_EXTRACT], "solve": [GEOMETRY_SOLVE]})
        self.assertEqual(self.stage_calls("figure"), [])

    async def test_followup_revision_keeps_previous_figure(self):
        first = await self.grade({"extract": [GEOMETRY_EXTRACT], "figure": [FIGURE_OK],
                                  "solve": [GEOMETRY_SOLVE]})
        self.calls.clear()
        followup = json.dumps({"new_questions": [], "revisions": [
            {"prev_no": "20(2)", "student_answer": "8", "note": "补拍"}]}, ensure_ascii=False)
        solve = json.dumps({"solutions": [
            {"no": "20(2)", "correct_answer": "8", "steps": ["按配图计算"]}]},
            ensure_ascii=False)
        second = await self.grade({"extract": [followup], "solve": [solve]},
                                  prev_result=first.result, followup_no=1)
        self.assertEqual(self.stage_calls("figure"), [])
        self.assertIn(FIGURE, self.stage_calls("solve")[0]["user"])
        q = {q["no"]: q for q in second.result["questions"]}["20(2)"]
        self.assertEqual(q["figure"], FIGURE)


class FigureHelpersTest(unittest.TestCase):
    def test_group_key_merges_sub_questions(self):
        self.assertEqual(staged._figure_group_key("20(1)"), "20")
        self.assertEqual(staged._figure_group_key("20（2）"), "20")
        self.assertEqual(staged._figure_group_key("V-3"), "V-3")

    def test_schema_keeps_figure(self):
        raw = {"schema_version": 3, "task_type": "grading", "subject": "数学",
               "questions": [{"id": "q1", "no": "1", "stem": "如图", "figure": FIGURE,
                              "status": "correct"}],
               "archive": {"action": "none"}}
        self.assertEqual(validate_result(raw)["questions"][0]["figure"], FIGURE)


class DiagramUsesFigureTest(unittest.IsolatedAsyncioTestCase):
    async def test_figure_is_sent_to_drawing_model(self):
        class Recorder(SequenceProvider):
            async def complete_text(self, system, user, max_tokens=0, timeout=None):
                self.users = getattr(self, "users", []) + [user]
                return await super().complete_text(system, user, max_tokens, timeout)

        prov = Recorder([outcome(RECT_JSON)])
        r = await diagram.generate_diagram("如图，求阴影面积", prov, figure=FIGURE)
        self.assertEqual(r.status, "generated")
        self.assertIn(FIGURE, prov.users[0])
        self.assertIn("原图配图描述", prov.users[0])

    def test_without_figure_prompt_is_unchanged(self):
        self.assertEqual(diagram.diagram_user("如图"), "题目：\n如图")
        self.assertEqual(diagram.diagram_user("如图", "  "), "题目：\n如图")


if __name__ == "__main__":
    unittest.main()
