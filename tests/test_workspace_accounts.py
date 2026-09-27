"""账号目录映射测试：身份 → 工作区目录名（网页账号、微信 openid、异常输入）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.workspace import (DEFAULT_ACCOUNT_DIR, ACCOUNT_DIR_MAX, account_dir,
                           account_dir_name, account_home, safe_archive_path,
                           workspace_accounts)


def make_settings(tmp: str, **overrides) -> Settings:
    data = {
        "engine": {"mode": "hermes"},
        "hermes": {"base_url": "", "api_key": ""},
        "data_dir": str(Path(tmp) / "data"),
        "workspace": {"dir": str(Path(tmp) / "workspace"), "init_readme": False},
    }
    data.update(overrides)
    return Settings.model_validate(data)


class AccountDirNameTest(unittest.TestCase):
    def test_web_account_uses_username(self):
        self.assertEqual(account_dir_name("web:leo"), "leo")
        self.assertEqual(account_dir_name("web:kid-2"), "kid-2")

    def test_wechat_openid_gets_prefix(self):
        self.assertEqual(account_dir_name("oABC123"), "wx-oABC123")

    def test_empty_falls_back_to_default(self):
        self.assertEqual(account_dir_name(""), DEFAULT_ACCOUNT_DIR)
        self.assertEqual(account_dir_name("   "), DEFAULT_ACCOUNT_DIR)
        self.assertEqual(account_dir_name(None), DEFAULT_ACCOUNT_DIR)

    def test_separators_dots_and_spaces_are_neutralized(self):
        name = account_dir_name("web:../etc")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)
        self.assertEqual(name, "etc")
        self.assertEqual(account_dir_name("web:a b"), "a-b")
        self.assertEqual(account_dir_name("web:.hidden"), "hidden")

    def test_result_is_truncated_and_still_valid(self):
        name = account_dir_name("web:" + "a" * 100)
        self.assertEqual(len(name), ACCOUNT_DIR_MAX)

    def test_never_escapes_workspace_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            root = account_dir(settings, "web:../../evil").resolve()
            self.assertEqual(root.parent.parent, Path(settings.workspace_dir).resolve().parent)


class AccountScopeTest(unittest.TestCase):
    def test_account_home_is_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            home = Path(account_home(settings, "web:leo"))
            self.assertEqual(home.name, "leo")
            self.assertTrue(str(home).startswith(str(Path(settings.workspace_dir).resolve())))

    def test_workspace_accounts_merges_config_and_db_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp, web={"users": [
                {"username": "leo", "password_hash": "placeholder"}]})
            names = workspace_accounts(settings, ["oABC", "web:leo", ""])
            self.assertEqual(names, ["leo", "wx-oABC", DEFAULT_ACCOUNT_DIR])

    def test_safe_path_rejects_illegal_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            for bad_account in ["../leo", "a/b", ".hidden", "x" * 41, ""]:
                self.assertIsNone(
                    safe_archive_path(settings, "数学/错题解析/2026-09-26.md",
                                      account=bad_account), bad_account)


if __name__ == "__main__":
    unittest.main()
