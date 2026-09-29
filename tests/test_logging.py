"""日志安全测试：第三方库不能把含密钥的 URL 打进日志。"""
from __future__ import annotations

import logging
import unittest

import app  # noqa: F401  导入即生效


class LoggingTest(unittest.TestCase):
    def test_httpx_logger_is_quiet(self):
        """httpx 的 INFO 会打印完整 URL，微信 secret 在查询串里 —— 必须压到 WARNING。"""
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").level, logging.WARNING)

    def test_wechat_secret_not_logged(self):
        """构造一次 wechat 调用，断言日志里不出现 secret（用假配置，不联网）。"""
        import asyncio
        from types import SimpleNamespace

        from app import wechat

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            cfg = SimpleNamespace(appid="", secret="")  # 未配置 → 不发请求
            openid, err = asyncio.run(wechat.code2session_result("fake-code", cfg))
            self.assertIsNone(openid)
            self.assertIn("未配置", err)
            self.assertFalse(any("secret" in m.lower() for m in records))
        finally:
            root.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()


class ConfigureLoggingTest(unittest.TestCase):
    def test_adds_stdout_handler_once(self):
        """_configure_logging 给 root 加 stdout handler，重复调用不翻倍。"""
        import os
        from unittest import mock

        from app.main import _configure_logging

        root = logging.getLogger()
        old_handlers = list(root.handlers)
        old_level = root.level
        for h in old_handlers:
            root.removeHandler(h)
        try:
            with mock.patch.dict(os.environ, {"LOG_LEVEL": "INFO"}):
                _configure_logging()
                first = [h for h in root.handlers
                         if isinstance(h, logging.StreamHandler)]
                self.assertEqual(len(first), 1)
                _configure_logging()
                second = [h for h in root.handlers
                          if isinstance(h, logging.StreamHandler)]
                self.assertEqual(len(second), 1)
            self.assertEqual(root.level, logging.INFO)
        finally:
            for h in list(root.handlers):
                root.removeHandler(h)
            for h in old_handlers:
                root.addHandler(h)
            root.setLevel(old_level)

    def test_info_records_pass_root(self):
        """配好后 app.* 的 INFO 能到达 root handler（之前会被静默丢弃）。"""
        import io
        import os
        from unittest import mock

        from app.main import _configure_logging

        root = logging.getLogger()
        old_handlers = list(root.handlers)
        old_level = root.level
        for h in old_handlers:
            root.removeHandler(h)
        buf = io.StringIO()
        try:
            with mock.patch.dict(os.environ, {"LOG_LEVEL": "INFO"}):
                _configure_logging()
                # 把 handler 的输出重定向到内存，验证 INFO 确实被放行
                for h in root.handlers:
                    if isinstance(h, logging.StreamHandler):
                        h.setStream(buf)
                logging.getLogger("app.hermes").info("调用 Hermes 技能测试")
            self.assertIn("调用 Hermes 技能测试", buf.getvalue())
        finally:
            for h in list(root.handlers):
                root.removeHandler(h)
            for h in old_handlers:
                root.addHandler(h)
            root.setLevel(old_level)
