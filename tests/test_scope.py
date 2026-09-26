"""资料区间测试：月考/期中/期末/周报默认规则、用户指定优先与缺口标注。"""
from __future__ import annotations

import unittest
from datetime import date

from app import scope


class ComputeScopeTest(unittest.TestCase):
    def test_user_specified_wins(self):
        d = scope.compute_scope(task_type="weekly_report", scope_start="2026-09-01",
                                scope_end="2026-09-10", today=date(2026, 9, 26))
        self.assertEqual(d.start_date, "2026-09-01")
        self.assertEqual(d.end_date, "2026-09-10")
        self.assertIn("用户指定", d.note)

    def test_user_specified_start_only_uses_today(self):
        d = scope.compute_scope(task_type="training", training_kind="topic",
                                scope_start="2026-09-01", today=date(2026, 9, 26))
        self.assertEqual(d.end_date, "2026-09-26")

    def test_monthly_uses_first_day_of_month(self):
        d = scope.compute_scope(task_type="training", training_kind="monthly",
                                today=date(2026, 9, 26))
        self.assertEqual(d.start_date, "2026-09-01")
        self.assertEqual(d.end_date, "2026-09-26")
        self.assertFalse(d.missing)

    def test_midterm_without_term_start_reports_gap(self):
        d = scope.compute_scope(task_type="training", training_kind="midterm",
                                today=date(2026, 9, 26))
        self.assertEqual(d.start_date, "")
        self.assertEqual(d.end_date, "2026-09-26")
        self.assertTrue(d.missing)
        self.assertIn("开学日期", d.missing[0])

    def test_midterm_with_term_start(self):
        d = scope.compute_scope(task_type="training", training_kind="midterm",
                                term_start_date="2026-09-01", today=date(2026, 9, 26))
        self.assertEqual(d.start_date, "2026-09-01")
        self.assertFalse(d.missing)

    def test_weekly_report_starts_monday(self):
        # 2026-09-26 是周六，本周一为 2026-09-21
        d = scope.compute_scope(task_type="weekly_report", today=date(2026, 9, 26))
        self.assertEqual(d.start_date, "2026-09-21")
        self.assertEqual(d.end_date, "2026-09-26")

    def test_grading_has_no_interval(self):
        d = scope.compute_scope(task_type="grading", today=date(2026, 9, 26))
        self.assertEqual(d.start_date, "")
        self.assertEqual(d.end_date, "")
        self.assertIn("未限定", d.note)


class DescribeScopeTest(unittest.TestCase):
    def test_limited_scope_note(self):
        info = scope.describe_scope({
            "task_type": "training", "training_kind": "monthly",
            "scope_start": "2026-09-01", "scope_end": "2026-09-26",
            "exam_scope": "第一章 有理数",
        })
        self.assertIn("2026-09-01", info["note"])
        self.assertIn("月考", info["note"])
        self.assertEqual(info["missing"], [])

    def test_training_without_exam_scope_reports_gap(self):
        info = scope.describe_scope({
            "task_type": "training", "training_kind": "topic",
            "scope_start": "2026-09-01", "scope_end": "2026-09-26",
        })
        self.assertTrue(any("考试范围" in m for m in info["missing"]))

    def test_midterm_without_term_start_reports_gap(self):
        info = scope.describe_scope({
            "task_type": "training", "training_kind": "midterm",
            "scope_start": "", "scope_end": "2026-09-26", "exam_scope": "全册",
        }, term_start_date="")
        self.assertTrue(any("开学日期" in m for m in info["missing"]))


if __name__ == "__main__":
    unittest.main()
