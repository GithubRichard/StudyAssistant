import tempfile
from pathlib import Path
import unittest

from scripts.split_thinking import split_log


class SplitThinkingTest(unittest.TestCase):
    def run_split(self, text):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = root / "thinking.log"
        source.write_text(text, encoding="utf-8")
        output = root / "sessions"
        result = split_log(source, output)
        return result, output

    def test_groups_sessions_and_separates_anonymous_calls(self):
        text = (
            "2026-09-30 00:00:00 【模型思考过程】stage=extract\n甲\n【思考过程结束】\n"
            "2026-09-30 00:00:01 【模型思考过程】session=a\n乙\n【思考过程结束】\n"
            "2026-09-30 00:00:02 【模型思考过程】stage=verify\n丙\n【思考过程结束】\n"
            "2026-09-30 00:00:03 【模型思考过程】session=a\n丁\n【思考过程结束】\n"
        )
        result, output = self.run_split(text)
        self.assertEqual(result, (3, 4, 0))
        session = next(output.glob("*session_a.txt")).read_text()
        self.assertIn("乙", session)
        self.assertIn("丁", session)
        self.assertEqual(sum(len(p.read_text()) for p in output.iterdir()), len(text))

    def test_preserves_unassigned_and_truncated_records(self):
        text = "前言\n【模型思考过程】session=../../bad\n内容\n【模型思考过程】stage=x\n尾部"
        result, output = self.run_split(text)
        self.assertEqual(result, (3, 2, 2))
        self.assertEqual(sum(len(p.read_text()) for p in output.iterdir()), len(text))
        with self.assertRaises(FileExistsError):
            split_log(output.parent / "thinking.log", output)

    def test_no_markers_does_not_create_output(self):
        with self.assertRaises(ValueError):
            self.run_split("普通日志\n")
