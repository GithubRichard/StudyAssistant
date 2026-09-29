"""思考过程日志（app/thinking.py）单元测试：纯本地，不联网。"""
from __future__ import annotations

import logging
import os
import unittest
from unittest import mock

from app import thinking
from app.providers import GradeOutcome


class ExtractReasoningTest(unittest.TestCase):
    def test_str_reasoning_content(self):
        msg = {"content": "{}", "reasoning_content": "  先想想…\n"}
        self.assertEqual(thinking.extract_reasoning(msg), "先想想…")

    def test_reasoning_key_fallback(self):
        msg = {"content": "{}", "reasoning": "思路"}
        self.assertEqual(thinking.extract_reasoning(msg), "思路")

    def test_missing_returns_empty(self):
        self.assertEqual(thinking.extract_reasoning({"content": "{}"}), "")
        self.assertEqual(thinking.extract_reasoning({}), "")
        self.assertEqual(thinking.extract_reasoning(None), "")
        self.assertEqual(thinking.extract_reasoning("not-a-dict"), "")

    def test_dict_with_text(self):
        msg = {"reasoning_content": {"text": "分步思考"}}
        self.assertEqual(thinking.extract_reasoning(msg), "分步思考")

    def test_list_parts(self):
        msg = {"reasoning": ["第一步", {"text": "第二步"}, ""]}
        self.assertEqual(thinking.extract_reasoning(msg), "第一步\n第二步")

    def test_empty_string_returns_empty(self):
        self.assertEqual(thinking.extract_reasoning({"reasoning_content": "  "}), "")


class LogThinkingTest(unittest.TestCase):
    def _capture(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logger = logging.getLogger("studyassistant.thinking")
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        return records, handler, logger, old_level

    def _release(self, handler, logger, old_level):
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    def test_disabled_by_default(self):
        records, handler, logger, old_level = self._capture()
        try:
            with mock.patch.dict(os.environ, clear=True):
                self.assertNotIn("SA_DEBUG_THINKING", os.environ)
                thinking.log_thinking("ctx", "some thinking")
            self.assertEqual(records, [])
        finally:
            self._release(handler, logger, old_level)

    def test_enabled_logs_with_markers(self):
        records, handler, logger, old_level = self._capture()
        try:
            with mock.patch.dict(os.environ, {"SA_DEBUG_THINKING": "1"}):
                thinking.log_thinking("stage=solve provider=qwen", "思考内容")
            self.assertEqual(len(records), 1)
            self.assertIn("【模型思考过程】", records[0])
            self.assertIn("【思考过程结束】", records[0])
            self.assertIn("stage=solve provider=qwen", records[0])
            self.assertIn("思考内容", records[0])
        finally:
            self._release(handler, logger, old_level)

    def test_enabled_but_empty_reasoning_no_log(self):
        records, handler, logger, old_level = self._capture()
        try:
            with mock.patch.dict(os.environ, {"SA_DEBUG_THINKING": "true"}):
                thinking.log_thinking("ctx", "   ")
            self.assertEqual(records, [])
        finally:
            self._release(handler, logger, old_level)


class GradeOutcomeThinkingTest(unittest.TestCase):
    def test_thinking_defaults_empty(self):
        o = GradeOutcome(text="{}", provider="qwen", model="qwen-max")
        self.assertEqual(o.thinking, "")

    def test_thinking_accepted(self):
        o = GradeOutcome(text="{}", provider="qwen", model="qwen-max",
                         thinking="why")
        self.assertEqual(o.thinking, "why")


if __name__ == "__main__":
    unittest.main()
