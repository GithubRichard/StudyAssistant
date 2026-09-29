"""题干存疑（stem_uncertain）防线测试：提取阶段声明题干不可信时，
求解阶段不调用、比对直接标存疑、missing_info 提示补充材料。

回归场景：横拍照片导致提取阶段编造题干（2026-09-29 英语卷事故）。
"""
from __future__ import annotations

import json
import unittest

from app import staged
from app.staged import ExtractedQuestion, format_extraction_log
from tests.test_staged import FakeProvider, factory_for, make_settings


class PromptTest(unittest.TestCase):
    def test_anti_hallucination_rules(self):
        sys = staged.EXTRACT_SYSTEM
        # 题干防幻觉：逐字照抄，绝不编造
        self.assertIn("绝不编造题干", sys)
        self.assertIn("stem_uncertain", sys)
        # 先看方向：思考中先说明图片方向
        self.assertIn("图片方向", sys)
        # 红笔=妈妈批改痕迹，不转写
        self.assertIn("红笔字迹一律视为批改痕迹", sys)

    def test_example_json_has_stem_fields(self):
        user = staged._extract_user_initial("英语", "七年级", 1, "")
        self.assertIn("stem_uncertain", user)
        self.assertIn("stem_note", user)


class SchemaTest(unittest.TestCase):
    def test_defaults(self):
        q = ExtractedQuestion(no="1")
        self.assertFalse(q.stem_uncertain)
        self.assertEqual(q.stem_note, "")

    def test_stem_uncertain_accepted(self):
        q = ExtractedQuestion(no="29", stem="", stem_uncertain=True,
                              stem_note="图片旋转无法辨认")
        self.assertTrue(q.stem_uncertain)
        self.assertEqual(q.stem_note, "图片旋转无法辨认")


class ExtractionLogTest(unittest.TestCase):
    def test_stem_flag_shown(self):
        data = {"questions": [
            {"no": "29", "stem": "", "student_answer": "D", "page": "1",
             "handwriting_uncertain": False, "uncertain_note": "",
             "stem_uncertain": True, "stem_note": "图片旋转无法辨认"},
        ]}
        out = format_extraction_log(data)
        self.assertIn("【题干存疑】", out)
        self.assertIn("图片旋转无法辨认", out)


class StemUncertainPipelineTest(unittest.IsolatedAsyncioTestCase):
    """端到端：题干存疑的题不送求解、整题 uncertain、missing_info 提示补充。"""

    def setUp(self):
        self.calls = []
        extract = json.dumps({"questions": [
            {"no": "1", "stem": "解方程 2x+1=9", "student_answer": "x=4",
             "page": "1", "handwriting_uncertain": False, "uncertain_note": "",
             "stem_uncertain": False, "stem_note": ""},
            # 第 2 题：提取阶段声明题干不可信（模拟横拍事故）
            {"no": "2", "stem": "", "student_answer": "D",
             "page": "1", "handwriting_uncertain": False, "uncertain_note": "",
             "stem_uncertain": True, "stem_note": "图片旋转无法辨认"},
        ]}, ensure_ascii=False)
        # 求解剧本只给第 1 题：第 2 题根本不该被送去求解
        solve = json.dumps({"solutions": [
            {"no": "1", "correct_answer": "x=4", "steps": ["2x=8"]},
        ]}, ensure_ascii=False)
        self.scripts = {"fake": {"extract": [extract], "solve": [solve],
                                 "compare": [], "diagnose": []}}
        self.settings = make_settings("fake")

    async def _grade(self):
        return await staged.grade_staged(
            [(b"img", "image/jpeg")], "数学", "七年级", "", self.settings,
            provider_factory=factory_for(self.scripts, self.calls))

    def _solve_user_json(self):
        solve_calls = [c for c in self.calls if c["stage"] == "solve"]
        self.assertEqual(len(solve_calls), 1)
        user = solve_calls[0]["user"]
        start = user.index("```json") + len("```json")
        end = user.index("```", start)
        return json.loads(user[start:end])

    async def test_solve_not_called_for_stem_uncertain(self):
        await self._grade()
        items = self._solve_user_json()
        nos = [i["no"] for i in items]
        self.assertEqual(nos, ["1"])

    async def test_stem_uncertain_is_uncertain(self):
        outcome = await self._grade()
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(by_no["2"]["status"], "uncertain")
        self.assertNotEqual(by_no["2"]["status"], "wrong")
        self.assertEqual(by_no["2"]["correct_answer"], "")

    async def test_compare_model_not_called_when_all_uncertain(self):
        await self._grade()
        compare_calls = [c for c in self.calls if c["stage"] == "compare"]
        self.assertEqual(compare_calls, [])

    async def test_missing_info_asks_for_stem(self):
        outcome = await self._grade()
        missing = outcome.result["missing_info"]
        self.assertTrue(any("「2」" in m and "图片旋转无法辨认" in m for m in missing),
                        f"missing_info={missing}")


if __name__ == "__main__":
    unittest.main()
