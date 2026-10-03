"""转写准确性防线测试：版块标题过滤、题号序列复核、逐题复核、复查协议统一。

回归场景（2026-09-29 英语卷事故）：
- extract 把 18-23 读成 19-24（整段题号 +1 错位），下游照错题干求解、比对；
- (difficulty) 被抄成 (easy)，参考答案地基就是错的；
- "四、按要求填写单词，补全对话（每题2分，共10分）"这类版块标题被当成一道题送入 solve；
- 复查提示词对"看不清"同时要求 transcript_ok=false 和 state=unverified，自相矛盾。
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import staged
from app.hermes import build_review_messages
from app.staged import ExtractedQuestion, StageError, format_extraction_log
from app.providers import ProviderError
from tests.test_staged import factory_for, make_settings


def _q(no, stem, answer="", **kw):
    d = {"no": no, "stem": stem, "student_answer": answer, "page": "1",
         "handwriting_uncertain": False, "uncertain_note": "",
         "stem_uncertain": False, "stem_note": "",
         "number_uncertain": False, "number_note": ""}
    d.update(kw)
    return d


def _extract_script(*qs):
    return json.dumps({"questions": list(qs)}, ensure_ascii=False)


def _numbers_script(*nos):
    return json.dumps({"numbers": list(nos)}, ensure_ascii=False)


class SectionHeaderFilterTest(unittest.TestCase):
    def test_header_with_scores_is_dropped(self):
        q = ExtractedQuestion(no="四", stem="四、按要求填写单词，补全对话（每题2分，共10分）")
        self.assertTrue(staged._is_section_header(q))

    def test_header_reading_section_is_dropped(self):
        q = ExtractedQuestion(no="五", stem="五、阅读理解（共20分）")
        self.assertTrue(staged._is_section_header(q))

    def test_real_question_is_kept(self):
        q = ExtractedQuestion(no="18", stem="They find it ___18___ (excite) to see it.",
                              student_answer="exciting")
        self.assertFalse(staged._is_section_header(q))

    def test_header_with_scores_dropped_even_with_answer(self):
        # "每题X分/共X分"这类分值说明不可能出现在真题干里：即使带了作答也是标题
        q = ExtractedQuestion(no="1", stem="一、听力（每题2分）", student_answer="A")
        self.assertTrue(staged._is_section_header(q))

    def test_chinese_numbered_stem_without_scores_is_kept(self):
        # 中文编号开头但无分值说明、有作答：保守起见不当标题
        q = ExtractedQuestion(no="1", stem="一、把下列单词抄写一遍", student_answer="hello")
        self.assertFalse(staged._is_section_header(q))

    def test_drop_returns_kept_and_descriptions(self):
        qs = [ExtractedQuestion(no="四", stem="四、按要求填写单词，补全对话（每题2分，共10分）"),
              ExtractedQuestion(no="24", stem="___24___", student_answer="How")]
        kept, dropped = staged._drop_section_headers(qs)
        self.assertEqual([q.no for q in kept], ["24"])
        self.assertEqual(len(dropped), 1)
        self.assertIn("四", dropped[0])


class HeaderFilterPipelineTest(unittest.IsolatedAsyncioTestCase):
    """端到端：版块标题不送入 solve，也不出现在结果里。"""

    def setUp(self):
        self.calls = []
        extract = _extract_script(
            _q("四", "四、按要求填写单词，补全对话（每题2分，共10分）"),
            _q("24", "___24___ (believe) failure is a good thing", "I believe"),
            _q("25", "___25___ we ask ourselves a question?", "Should"),
        )
        solve = json.dumps({"solutions": [
            {"no": "24", "correct_answer": "I believe", "steps": ["原样填入"]},
            {"no": "25", "correct_answer": "Should", "steps": ["一般疑问句"]},
        ]}, ensure_ascii=False)
        self.scripts = {"fake": {
            "extract": [extract],
            "number_verify": [_numbers_script("24", "25")],
            "solve": [solve], "compare": [], "diagnose": []}}
        self.settings = make_settings("fake")

    async def _grade(self, on_stage=None):
        return await staged.grade_staged(
            [(b"img", "image/jpeg")], "英语", "七年级", "", self.settings,
            provider_factory=factory_for(self.scripts, self.calls),
            on_stage=on_stage)

    def _solve_nos(self):
        solve_calls = [c for c in self.calls if c["stage"] == "solve"]
        self.assertEqual(len(solve_calls), 1)
        user = solve_calls[0]["user"]
        start = user.index("```json") + len("```json")
        end = user.index("```", start)
        return [i["no"] for i in json.loads(user[start:end])]

    async def test_header_not_sent_to_solve(self):
        await self._grade()
        self.assertEqual(self._solve_nos(), ["24", "25"])

    async def test_header_not_in_result(self):
        outcome = await self._grade()
        nos = [q["no"] for q in outcome.result["questions"]]
        self.assertNotIn("四", nos)
        self.assertEqual(len(nos), 2)

    async def test_header_drop_recorded_in_extract_note(self):
        seen = {}

        async def recorder(name, data):
            seen[name] = data

        await self._grade(on_stage=recorder)
        self.assertIn("过滤版块标题", seen["extract"]["zoom_note"])


class NumberVerifyTest(unittest.IsolatedAsyncioTestCase):
    """题号序列复核：diff 不一致 → 标存疑 → 不送求解。"""

    def setUp(self):
        self.calls = []

    def _scripts(self, numbers, extract=None, solve=None):
        if extract is None:
            extract = _extract_script(
                _q("18", "They find it ___18___ (excite) to see it.", "exciting"),
                _q("19", "His grandmother found it ___19___ (difficulty).", "difficult"),
            )
        if solve is None:
            solve = json.dumps({"solutions": []}, ensure_ascii=False)
        return {"fake": {
            "extract": [extract],
            "number_verify": [_numbers_script(*numbers)],
            "solve": [solve], "compare": [], "diagnose": []}}

    async def _grade(self, numbers, scripts=None):
        settings = make_settings("fake")
        return await staged.grade_staged(
            [(b"img", "image/jpeg")], "英语", "七年级", "", settings,
            provider_factory=factory_for(scripts or self._scripts(numbers), self.calls))

    async def test_mismatch_flags_uncertain(self):
        # 事故重演：转写 18/19，复核 19/20（整体 +1 错位）
        outcome = await self._grade(["19", "20"])
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(by_no["18"]["status"], "uncertain")
        self.assertEqual(by_no["19"]["status"], "uncertain")
        self.assertEqual(by_no["18"]["correct_answer"], "")

    async def test_mismatch_missing_info_mentions_number(self):
        outcome = await self._grade(["19", "20"])
        missing = outcome.result["missing_info"]
        self.assertTrue(any("「18」" in m and "题号" in m for m in missing),
                        f"missing_info={missing}")

    async def test_count_mismatch_flags_all(self):
        outcome = await self._grade(["18", "19", "20"])
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(by_no["18"]["status"], "uncertain")
        self.assertEqual(by_no["19"]["status"], "uncertain")

    async def test_consistent_numbers_flow_normally(self):
        extract = _extract_script(_q("1", "解方程 2x+1=9", "x=4"))
        solve = json.dumps({"solutions": [
            {"no": "1", "correct_answer": "x=4", "steps": ["2x=8"]},
        ]}, ensure_ascii=False)
        outcome = await self._grade(["1"], scripts=self._scripts(["1"], extract, solve))
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(by_no["1"]["status"], "correct")

    async def test_verify_failure_degrades_gracefully(self):
        # 复核调用失败：降级为保留首轮转写，主流程继续
        extract = _extract_script(_q("1", "解方程 2x+1=9", "x=4"))
        solve = json.dumps({"solutions": [
            {"no": "1", "correct_answer": "x=4", "steps": ["2x=8"]},
        ]}, ensure_ascii=False)
        scripts = self._scripts(["1"], extract, solve)
        scripts["fake"]["number_verify"] = [ProviderError("fake boom")]
        outcome = await self._grade(["1"], scripts=scripts)
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(by_no["1"]["status"], "correct")


class OrientationGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_image_orientation_stops_before_model_call(self):
        calls = []
        settings = make_settings("fake")
        scripts = {"fake": {"extract": [_extract_script(_q("1", "1+1=?", "2"))],
                             "solve": [], "compare": [], "diagnose": []}}
        factory = factory_for(scripts, calls)
        info = {"width": 1600, "height": 1200,
                "exif_orientation": None, "exif_rotated": False,
                "text_rotation_degrees": 90, "orientation_confidence": 0.5,
                "orientation_status": "uncertain",
                "orientation_check_required": True,
                "orientation_error": "orientation confidence below threshold"}
        settings.staged_grading.orientation_visual_fallback = False
        from app.orientation import ConfirmationRequired
        with patch.object(staged.image_prep, "prepare_extract_image",
                          return_value=(b"image", "image/jpeg", info)):
            with self.assertRaisesRegex(ConfirmationRequired, "确认"):
                await staged.grade_staged(
                    [(b"image", "image/jpeg")], "数学", "一年级", "", settings,
                    provider_factory=factory)
        self.assertEqual(calls, [])

class NumberUncertainFromExtractTest(unittest.IsolatedAsyncioTestCase):
    """extract 自己声明 number_uncertain：不送求解，直接标存疑。"""

    async def test_not_sent_to_solve(self):
        calls = []
        extract = _extract_script(
            _q("18", "They find it ___18___ (excite).", "exciting"),
            _q("19", "___19___ (difficulty).", "difficult",
               number_uncertain=True, number_note="题号18/19难辨"),
        )
        solve = json.dumps({"solutions": [
            {"no": "18", "correct_answer": "exciting", "steps": ["形容词作表语"]},
        ]}, ensure_ascii=False)
        scripts = {"fake": {
            "extract": [extract],
            "number_verify": [_numbers_script("18", "19")],
            "solve": [solve], "compare": [], "diagnose": []}}
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "英语", "七年级", "", make_settings("fake"),
            provider_factory=factory_for(scripts, calls))
        solve_calls = [c for c in calls if c["stage"] == "solve"]
        user = solve_calls[0]["user"]
        start = user.index("```json") + len("```json")
        end = user.index("```", start)
        self.assertEqual([i["no"] for i in json.loads(user[start:end])], ["18"])
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(by_no["19"]["status"], "uncertain")
        self.assertEqual(by_no["19"]["correct_answer"], "")


class PerQuestionVerifyTest(unittest.IsolatedAsyncioTestCase):
    """逐题复核（默认关闭）：只核对题号 + 括号原词，不碰学生答案。"""

    def setUp(self):
        self.calls = []

    def _base_scripts(self):
        extract = _extract_script(
            _q("18", "They find it ___18___ (excite) to see it.", "exciting"),
            _q("19", "His grandmother found it ___19___ (difficulty).", "difficult"),
        )
        solve = json.dumps({"solutions": [
            {"no": "18", "correct_answer": "exciting", "steps": ["形容词作表语"]},
            {"no": "19", "correct_answer": "difficult", "steps": ["find it adj. to do"]},
        ]}, ensure_ascii=False)
        return {"fake": {
            "extract": [extract],
            "number_verify": [_numbers_script("18", "19")],
            "per_question_verify": [
                json.dumps({"no": "18", "base_word": "excite"}, ensure_ascii=False),
                # 事故重演：(difficulty) 被复核成别的词
                json.dumps({"no": "19", "base_word": "easiness"}, ensure_ascii=False),
            ],
            "solve": [solve], "compare": [], "diagnose": []}}

    async def test_base_word_mismatch_flags_uncertain(self):
        settings = make_settings("fake")
        settings.staged_grading.per_question_verify = True
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "英语", "七年级", "", settings,
            provider_factory=factory_for(self._base_scripts(), self.calls))
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(by_no["18"]["status"], "correct")
        self.assertEqual(by_no["19"]["status"], "uncertain")
        pq_calls = [c for c in self.calls if c["stage"] == "per_question_verify"]
        self.assertEqual(len(pq_calls), 2)

    async def test_disabled_by_default(self):
        settings = make_settings("fake")
        self.assertFalse(settings.staged_grading.per_question_verify)
        outcome = await staged.grade_staged(
            [(b"img", "image/jpeg")], "英语", "七年级", "", settings,
            provider_factory=factory_for(self._base_scripts(), self.calls))
        pq_calls = [c for c in self.calls if c["stage"] == "per_question_verify"]
        self.assertEqual(pq_calls, [])
        by_no = {q["no"]: q for q in outcome.result["questions"]}
        self.assertEqual(by_no["19"]["status"], "correct")

    async def test_over_limit_skips_whole_batch(self):
        settings = make_settings("fake")
        settings.staged_grading.per_question_verify = True
        settings.staged_grading.per_question_verify_max_items = 1  # 2 题 > 上限
        await staged.grade_staged(
            [(b"img", "image/jpeg")], "英语", "七年级", "", settings,
            provider_factory=factory_for(self._base_scripts(), self.calls))
        pq_calls = [c for c in self.calls if c["stage"] == "per_question_verify"]
        self.assertEqual(pq_calls, [])


class ExtractPromptTest(unittest.TestCase):
    def test_bracket_word_rule(self):
        sys = staged.EXTRACT_SYSTEM
        self.assertIn("括号原词逐字照抄", sys)
        self.assertIn("(difficulty)", sys)

    def test_number_uncertain_rule(self):
        sys = staged.EXTRACT_SYSTEM
        self.assertIn("number_uncertain", sys)
        self.assertIn("绝不猜题号", sys)

    def test_section_header_not_a_question(self):
        sys = staged.EXTRACT_SYSTEM
        self.assertIn("版块标题行", sys)

    def test_user_format_has_number_fields(self):
        user = staged._extract_user_initial("英语", "七年级", 1, "")
        self.assertIn("number_uncertain", user)
        self.assertIn("number_note", user)

    def test_number_flag_in_log(self):
        data = {"questions": [
            _q("19", "___19___ (difficulty).", "difficult",
               number_uncertain=True, number_note="题号18/19难辨"),
        ]}
        out = format_extraction_log(data)
        self.assertIn("【题号存疑】", out)
        self.assertIn("题号18/19难辨", out)

    def test_stem_base_words(self):
        self.assertEqual(staged._stem_base_words("___19___ (difficulty) to hold"),
                         {"difficulty"})
        self.assertEqual(staged._stem_base_words("连词 (we, how, overcome)"), set())
        self.assertEqual(staged._stem_base_words("五、阅读理解（共20分）"), set())


class ReviewProtocolTest(unittest.TestCase):
    def _text(self, with_images):
        from app.config import Settings
        settings = Settings.model_validate({
            "engine": {"mode": "hermes"},
            "hermes": {"base_url": "http://127.0.0.1:8642", "api_key": "k"},
            "workspace": {"dir": "/tmp/ws"},
        })
        task = {"id": "t1", "task_type": "grading", "subject": "英语", "grade_level": "七年级"}
        run = {"run_no": 1, "kind": "initial"}
        q = {"id": "q1", "no": "18", "stem": "___18___ (excite)",
             "student_answer": "exciting",
             "status": "wrong", "correct_answer": "exciting"}
        images = ["data:image/jpeg;base64,AAA"] if with_images else None
        messages = build_review_messages(settings, task, run, [q], images=images)
        return messages[1]["content"][0]["text"]

    def test_unreadable_is_null_not_false(self):
        text = self._text(True)
        # 看不清 → transcript_ok=null + unverified 是合法终态，不再自相矛盾
        self.assertIn("transcript_ok 写 null", text)
        self.assertIn("只能标 unverified", text)
        self.assertNotIn("无法重读 → transcript_ok=false", text)

    def test_checklist_present(self):
        text = self._text(True)
        self.assertIn("转写必核清单", text)
        self.assertIn("题号数字", text)
        self.assertIn("括号", text)
        self.assertIn("人名", text)

    def test_false_still_requires_disagreed(self):
        text = self._text(True)
        self.assertIn("transcript_ok=false 的题必须标 disagreed", text)

    def test_text_only_path_checks_word_bank(self):
        # q29 教训：参考答案不能凭空加词丢词
        text = self._text(False)
        self.assertIn("不能凭空加词、丢词", text)


if __name__ == "__main__":
    unittest.main()
