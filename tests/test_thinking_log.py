"""思考过程日志：按天轮转的单一文件，保留 5 天。"""
from __future__ import annotations

import logging
import os
import tempfile
import time
import unittest
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from unittest import mock

from app import thinking


class ThinkingFileLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 隔离全局 logger 状态
        self._handlers = thinking.log.handlers[:]
        self._propagate = thinking.log.propagate
        self._level = thinking.log.level
        thinking.log.handlers.clear()
        thinking.log.propagate = True

    def tearDown(self):
        for h in thinking.log.handlers[:]:
            thinking.log.removeHandler(h)
            h.close()
        thinking.log.handlers.extend(self._handlers)
        thinking.log.propagate = self._propagate
        thinking.log.setLevel(self._level)
        self.tmp.cleanup()

    def test_setup_creates_daily_rotating_handler(self):
        path = thinking.setup_file_logging(self.tmp.name)
        self.assertEqual(path, str(Path(self.tmp.name) / "logs" / "thinking.log"))
        handlers = [h for h in thinking.log.handlers
                    if isinstance(h, TimedRotatingFileHandler)]
        self.assertEqual(len(handlers), 1)
        h = handlers[0]
        self.assertEqual(h.when, "MIDNIGHT")
        # thinking.log + 4 个历史文件 = 5 天
        self.assertEqual(h.backupCount, thinking.RETAIN_DAYS - 1)
        self.assertEqual(thinking.RETAIN_DAYS, 5)
        # 只写文件，不再进 docker 日志
        self.assertFalse(thinking.log.propagate)

    def test_setup_idempotent(self):
        thinking.setup_file_logging(self.tmp.name)
        thinking.setup_file_logging(self.tmp.name)
        handlers = [h for h in thinking.log.handlers
                    if isinstance(h, TimedRotatingFileHandler)]
        self.assertEqual(len(handlers), 1)

    def test_log_thinking_writes_to_file(self):
        path = thinking.setup_file_logging(self.tmp.name)
        with mock.patch.dict(os.environ, {"SA_DEBUG_THINKING": "1"}):
            thinking.log_thinking("stage=test", "这是思考内容")
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("【模型思考过程】", content)
        self.assertIn("这是思考内容", content)
        self.assertIn("【思考过程结束】", content)

    def test_disabled_writes_nothing(self):
        path = thinking.setup_file_logging(self.tmp.name)
        with mock.patch.dict(os.environ, {"SA_DEBUG_THINKING": "0"}):
            thinking.log_thinking("stage=test", "这是思考内容")
        self.assertFalse(Path(path).exists(), "关闭时不应产生日志文件")

    def test_setup_failure_keeps_console(self):
        # data_dir 指向一个已存在的文件 → 建目录失败
        bad = Path(self.tmp.name) / "not-a-dir"
        bad.write_text("x")
        with self.assertLogs(level="WARNING") as cm:
            path = thinking.setup_file_logging(str(bad))
        self.assertEqual(path, "")
        self.assertTrue(thinking.log.propagate, "失败时保持输出到控制台")
        self.assertTrue(any("思考过程日志文件初始化失败" in m for m in cm.output))

    def test_rotation_naming(self):
        # 轮转后的文件名形如 thinking.log.2026-09-29
        thinking.setup_file_logging(self.tmp.name)
        h = next(x for x in thinking.log.handlers
                 if isinstance(x, TimedRotatingFileHandler))
        name = h.rotation_filename(
            h.baseFilename + "." + time.strftime(h.suffix, time.localtime()))
        self.assertRegex(Path(name).name, r"^thinking\.log\.\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
