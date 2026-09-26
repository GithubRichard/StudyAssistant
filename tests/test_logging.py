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
