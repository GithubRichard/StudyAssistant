"""错题台账测试：去重、状态流转、复测事件追加与归档写入边界。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import db, workspace
from app.config import Settings


def make_settings(tmp: str) -> Settings:
    return Settings.model_validate({
        "engine": {"mode": "hermes"},
        "hermes": {"base_url": "", "api_key": ""},
        "data_dir": str(Path(tmp) / "data"),
        "workspace": {"dir": str(Path(tmp) / "workspace"), "init_readme": False},
    })


def entry(uid: str = "q-1", **overrides) -> dict:
    data = {
        "question_uid": uid, "task_id": "t1", "question_no": "3", "subject": "数学",
        "source": "9月3周数学作业", "page": "P12", "stem": "解方程 2x+1=9",
        "student_answer": "x=5", "correct_answer": "x=4",
        "error_rule": "移项时忘记变号", "knowledge_point": "一元一次方程",
        "status": "wrong", "remediation_state": "pending_correction",
    }
    data.update(overrides)
    return data


class LedgerDedupeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        self.db_path = self.settings.db_path
        await db.init_db(self.db_path)
        await db.get_or_create_user(self.db_path, "u1", 5)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_same_uid_is_deduped_not_duplicated(self):
        first = await db.upsert_ledger_question(self.db_path, "u1", entry())
        second = await db.upsert_ledger_question(
            self.db_path, "u1", entry(student_answer="x=6"))
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        rows = await db.list_ledger(self.db_path, "u1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["student_answer"], "x=6")

    async def test_state_is_not_erased_by_empty_update(self):
        first = await db.upsert_ledger_question(self.db_path, "u1", entry())
        await db.update_ledger_state(self.db_path, "u1", first["id"],
                                     remediation_state="corrected_pending_retest")
        await db.upsert_ledger_question(self.db_path, "u1", entry(remediation_state=""))
        row = await db.get_ledger_entry(self.db_path, "u1", first["id"])
        self.assertEqual(row["remediation_state"], "corrected_pending_retest")

    async def test_counts_and_subjects(self):
        await db.upsert_ledger_question(self.db_path, "u1", entry("q-1"))
        await db.upsert_ledger_question(self.db_path, "u1", entry("q-2", subject="英语"))
        counts = await db.ledger_counts(self.db_path, "u1")
        self.assertEqual(counts.get("pending_correction"), 2)
        self.assertEqual(await db.ledger_subjects(self.db_path, "u1"), ["数学", "英语"])

    async def test_events_are_appended_and_history_kept(self):
        first = await db.upsert_ledger_question(self.db_path, "u1", entry())
        await db.add_question_event(self.db_path, "u1", {
            "question_uid": "q-1", "event_type": "retest", "result": "retest_failed",
            "occurred_date": "2026-09-26", "student_answer": "x=6"})
        await db.add_question_event(self.db_path, "u1", {
            "question_uid": "q-1", "event_type": "retest", "result": "retest_passed",
            "occurred_date": "2026-10-03", "student_answer": "x=4"})
        events = await db.list_question_events(self.db_path, "u1", "q-1")
        self.assertEqual(len(events), 2)
        self.assertEqual({e["result"] for e in events}, {"retest_failed", "retest_passed"})
        # 历史判定不被改写
        row = await db.get_ledger_entry(self.db_path, "u1", first["id"])
        self.assertEqual(row["status"], "wrong")
        self.assertEqual(row["student_answer"], "x=5")


class RetestNoteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        self.root = Path(self.settings.workspace_dir)
        workspace.ensure_workspace(self.settings)
        # 归档含账号层：<工作区>/<账号>/<学科>/<子目录>/<日期>.md
        self.archive = self.root / "leo" / "数学" / "错题解析" / "2026-09-26.md"
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.write_text("# 已有记录\n旧内容\n", encoding="utf-8")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _entry(self, archive_path: str = "leo/数学/错题解析/2026-09-26.md") -> dict:
        return {"id": 7, "source": "9月3周数学作业", "page": "P12", "question_no": "3",
                "archive_path": archive_path}

    async def test_retest_is_appended_and_not_duplicated(self):
        event = {"result": "retest_passed", "occurred_date": "2026-09-26",
                 "student_answer": "x=4", "note": "重新做对了"}
        first = await workspace.append_retest_note(self.settings, self._entry(), event)
        self.assertEqual(first["status"], "generated")
        text = self.archive.read_text(encoding="utf-8")
        self.assertIn("旧内容", text)
        self.assertIn("复测通过", text)
        self.assertIn("x=4", text)

        second = await workspace.append_retest_note(self.settings, self._entry(), event)
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(self.archive.read_text(encoding="utf-8").count("复测通过"), 1)

    async def test_out_of_scope_archive_path_is_rejected(self):
        out = await workspace.append_retest_note(
            self.settings, self._entry("../../etc/passwd"), {"result": "retest_passed"})
        self.assertEqual(out["status"], "skipped")

    async def test_missing_archive_file_is_reported(self):
        out = await workspace.append_retest_note(
            self.settings, self._entry("leo/数学/错题解析/2026-01-01.md"),
            {"result": "retest_failed"})
        self.assertEqual(out["status"], "skipped")
        self.assertIn("不存在", out["note"])


if __name__ == "__main__":
    unittest.main()


class DisputeWithdrawTest(unittest.IsolatedAsyncioTestCase):
    """用户点"我觉得判错了"：记异议事件，条目从台账撤回（不再计入默认视图与统计）。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        self.db_path = self.settings.db_path
        await db.init_db(self.db_path)
        await db.get_or_create_user(self.db_path, "u1", 5)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def test_disputed_maps_to_withdrawn(self):
        from app.api import _LEDGER_STATE_BY_RESULT
        self.assertEqual(_LEDGER_STATE_BY_RESULT["disputed"], "withdrawn")

    async def test_withdrawn_excluded_from_default_list_but_queryable(self):
        saved = await db.upsert_ledger_question(self.db_path, "u1", entry("q-1"))
        await db.upsert_ledger_question(self.db_path, "u1", entry("q-2"))
        await db.update_ledger_state(self.db_path, "u1", saved["id"],
                                     remediation_state="withdrawn")
        default = await db.list_ledger(self.db_path, "u1")
        self.assertEqual([r["question_uid"] for r in default], ["q-2"])
        explicit = await db.list_ledger(self.db_path, "u1", states=["withdrawn"])
        self.assertEqual([r["question_uid"] for r in explicit], ["q-1"])

    async def test_withdrawn_excluded_from_counts(self):
        await db.upsert_ledger_question(self.db_path, "u1", entry("q-1"))
        saved = await db.upsert_ledger_question(self.db_path, "u1", entry("q-2"))
        await db.update_ledger_state(self.db_path, "u1", saved["id"],
                                     remediation_state="withdrawn")
        counts = await db.ledger_counts(self.db_path, "u1")
        self.assertEqual(counts.get("pending_correction"), 1)
        self.assertNotIn("withdrawn", counts)

    async def test_dispute_event_history_kept(self):
        saved = await db.upsert_ledger_question(self.db_path, "u1", entry("q-1"))
        await db.add_question_event(self.db_path, "u1", {
            "question_uid": "q-1", "subject": "数学", "event_type": "retest",
            "result": "disputed", "occurred_date": "2026-09-29",
            "student_answer": "", "note": "学生认为判分有误", "archive_path": "",
        })
        await db.update_ledger_state(self.db_path, "u1", saved["id"],
                                     remediation_state="withdrawn")
        events = await db.list_question_events(self.db_path, "u1", "q-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["result"], "disputed")
