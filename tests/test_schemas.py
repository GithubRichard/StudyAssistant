"""结果协议后处理：跨页分叉只读检测。

约定：同来源同题号但页码不一致时只提示人工核对，不合并条目、不改判、不改 uid。
"""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.schemas import (ReviewResponse, cross_page_divergence_notes,
                         fill_question_uids, page_start_token)

SOURCE = "9月3周数学作业"


def _question(no: str, page: str, source: str = SOURCE, status: str = "wrong"):
    return {"id": f"{source}-{no}-{page or 'blank'}", "no": no, "source": source,
            "page": page, "status": status}


class PageStartTokenTest(unittest.TestCase):
    def test_start_page_normalization(self):
        self.assertEqual(page_start_token("P12-13"), "P12")
        self.assertEqual(page_start_token("p12"), "P12")
        self.assertEqual(page_start_token("P12~13"), "P12")
        self.assertEqual(page_start_token("第 12-13 页"), "12")
        self.assertEqual(page_start_token("  "), "")
        self.assertEqual(page_start_token(""), "")

    def test_unrecognized_page_falls_back_to_trimmed_text(self):
        self.assertEqual(page_start_token(" 附录 "), "附录")


class CrossPageDivergenceTest(unittest.TestCase):
    def test_same_number_on_different_pages_is_flagged(self):
        result = {"questions": [_question("5", "P12"), _question("5", "P13")]}
        notes = cross_page_divergence_notes(result)
        self.assertEqual(len(notes), 1)
        self.assertIn("疑似跨页分叉", notes[0])
        self.assertIn("请人工核对", notes[0])
        self.assertIn("第 5 题", notes[0])
        self.assertIn("P12", notes[0])
        self.assertIn("P13", notes[0])

    def test_same_page_different_case_is_not_flagged(self):
        result = {"questions": [_question("5", "P12"), _question("5", "p12 ")]}
        self.assertEqual(cross_page_divergence_notes(result), [])

    def test_page_range_matching_start_page_is_not_flagged(self):
        result = {"questions": [_question("5", "P12-13"), _question("5", "P12")]}
        self.assertEqual(cross_page_divergence_notes(result), [])

    def test_different_question_number_is_not_flagged(self):
        result = {"questions": [_question("5", "P12"), _question("6", "P13")]}
        self.assertEqual(cross_page_divergence_notes(result), [])

    def test_different_source_is_not_flagged(self):
        result = {"questions": [_question("5", "P12"),
                                _question("5", "P13", source="9月4周数学作业")]}
        self.assertEqual(cross_page_divergence_notes(result), [])

    def test_blank_page_next_to_known_page_is_flagged(self):
        result = {"questions": [_question("5", "P12"), _question("5", "")]}
        notes = cross_page_divergence_notes(result)
        self.assertEqual(len(notes), 1)
        self.assertIn("页码空缺", notes[0])

    def test_all_blank_pages_is_not_flagged(self):
        result = {"questions": [_question("5", ""), _question("5", "")]}
        self.assertEqual(cross_page_divergence_notes(result), [])

    def test_missing_source_or_number_is_skipped(self):
        result = {"questions": [_question("", "P12"), _question("5", "P13", source="")]}
        self.assertEqual(cross_page_divergence_notes(result), [])

    def test_sample_is_truncated_when_many_groups(self):
        questions = []
        for no in ("1", "2", "3", "4"):
            questions.append(_question(no, "P12"))
            questions.append(_question(no, "P13"))
        notes = cross_page_divergence_notes({"questions": questions})
        self.assertEqual(len(notes), 1)
        self.assertIn("检测到 4 处", notes[0])
        # 只列前 3 处样例，其余以省略号带过
        self.assertIn("第 1 题", notes[0])
        self.assertNotIn("第 4 题", notes[0])
        self.assertIn("…", notes[0])


class FillQuestionUidsTest(unittest.TestCase):
    def test_hint_is_appended_without_touching_questions(self):
        result = {"subject": "数学",
                  "missing_info": ["本学期开学日期未提供"],
                  "questions": [_question("5", "P12"), _question("5", "P13")]}
        before = [dict(q) for q in result["questions"]]
        filled = fill_question_uids(result, "2026-09-28")

        self.assertEqual(len(filled["missing_info"]), 2)
        self.assertEqual(filled["missing_info"][0], "本学期开学日期未提供")
        self.assertIn("疑似跨页分叉", filled["missing_info"][1])
        # 只提示：题目字段原样保留，uid 只做回填
        self.assertEqual([q["page"] for q in filled["questions"]],
                         [q["page"] for q in before])
        self.assertEqual([q["status"] for q in filled["questions"]],
                         [q["status"] for q in before])
        self.assertTrue(all(q["uid"].startswith("q-") for q in filled["questions"]))
        self.assertNotEqual(filled["questions"][0]["uid"],
                            filled["questions"][1]["uid"])

    def test_repeated_fill_does_not_duplicate_hint(self):
        result = {"subject": "数学",
                  "questions": [_question("5", "P12"), _question("5", "P13")]}
        fill_question_uids(result, "2026-09-28")
        fill_question_uids(result, "2026-09-28")
        notes = [m for m in result["missing_info"] if "疑似跨页分叉" in m]
        self.assertEqual(len(notes), 1)

    def test_duplicate_detection_still_works_without_cross_page_hint(self):
        result = {"subject": "数学",
                  "questions": [_question("5", "P12"), _question("5", "P12")]}
        filled = fill_question_uids(result, "2026-09-28")
        notes = filled["missing_info"]
        self.assertEqual(len(notes), 1)
        self.assertIn("重复题目条目", notes[0])
        self.assertNotIn("疑似跨页分叉", notes[0])

    def test_no_hint_when_nothing_suspicious(self):
        result = {"subject": "数学", "questions": [_question("5", "P12"),
                                                  _question("6", "P13")]}
        filled = fill_question_uids(result, "2026-09-28")
        self.assertNotIn("missing_info", filled)


class ReviewResponseTest(unittest.TestCase):
    """复查输出协议：复查方无权改学业判定，输出约束在 reviews 内。"""

    def test_valid_response_parses(self):
        data = {"reviews": [
            {"id": "q1", "state": "agreed", "note": "无异议", "basis": ""},
            {"id": "q2", "state": "disagreed", "basis": "计算无误，判错存疑"},
        ]}
        parsed = ReviewResponse.model_validate(data)
        self.assertEqual(len(parsed.reviews), 2)
        self.assertEqual(parsed.reviews[1].state, "disagreed")

    def test_empty_reviews_rejected(self):
        with self.assertRaises(ValidationError):
            ReviewResponse.model_validate({"reviews": []})

    def test_missing_reviews_key_rejected(self):
        with self.assertRaises(ValidationError):
            ReviewResponse.model_validate({})

    def test_duplicate_ids_rejected(self):
        data = {"reviews": [
            {"id": "q1", "state": "agreed"},
            {"id": "q1", "state": "unverified"},
        ]}
        with self.assertRaises(ValidationError):
            ReviewResponse.model_validate(data)

    def test_disagreed_without_basis_rejected(self):
        with self.assertRaises(ValidationError):
            ReviewResponse.model_validate(
                {"reviews": [{"id": "q1", "state": "disagreed", "basis": ""}]})

    def test_illegal_state_rejected(self):
        with self.assertRaises(ValidationError):
            ReviewResponse.model_validate(
                {"reviews": [{"id": "q1", "state": "correct"}]})

    def test_status_fields_ignored_extra(self):
        """复查方试图夹带改判字段（status/分数）时被忽略，不进协议。"""
        data = {"reviews": [{"id": "q1", "state": "agreed", "status": "correct"}]}
        parsed = ReviewResponse.model_validate(data)
        self.assertFalse(hasattr(parsed.reviews[0], "status"))

    def test_review_summary_extended_fields_have_defaults(self):
        from app.schemas import ReviewSummary

        summary = ReviewSummary.model_validate({"state": "not_run"})
        self.assertEqual(summary.target_count, 0)
        self.assertEqual(summary.unprocessed, 0)
        self.assertEqual(summary.model_requested, "")
        self.assertEqual(summary.model_reported, "")
        self.assertEqual(summary.model_identity, "")
        self.assertEqual(summary.coverage, "")

    def test_review_summary_accepts_extended_fields(self):
        from app.schemas import ReviewSummary

        summary = ReviewSummary.model_validate({
            "state": "completed", "scope": 3, "disagreed": 1, "unverified": 0,
            "target_count": 3, "unprocessed": 0,
            "model_requested": "glm", "model_reported": "glm-5.3",
            "model_identity": "confirmed", "coverage": "full_images",
        })
        self.assertEqual(summary.model_reported, "glm-5.3")


if __name__ == "__main__":
    unittest.main()
