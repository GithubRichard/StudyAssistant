"""配置解析：环境变量替换不得破坏网页账号密码哈希。

历史 bug：`_ENV_RE` 曾支持"裸 $VAR"写法，会把哈希里的 `$<salt_hex>`
（盐以 a-f 开头时）当成环境变量名替换成空串，导致密码永远校验失败。
这里锁死该行为，避免回归。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.auth import hash_password, verify_password_hash
from app.config import _sub_env, load_settings

# 盐以字母开头，正是会被"裸 $VAR"规则吃掉的形态
HASH = ("pbkdf2_sha256$260000$bace6828c481b1449f33570ae260b34e"
        "$5d30846d13879676dc1cef1739c39d7e17c0ad7d712ad89647e3ae7c2bb2f725")


class SubEnvTest(unittest.TestCase):
    def test_dollar_in_hash_is_not_treated_as_env_var(self):
        text = f'password_hash: "{HASH}"'
        self.assertEqual(_sub_env(text), text)

    def test_generated_hash_survives_substitution(self):
        h = hash_password("pw-one")
        self.assertEqual(_sub_env(f'h: "{h}"'), f'h: "{h}"')
        self.assertTrue(verify_password_hash("pw-one", _sub_env(h)))

    def test_braced_var_still_replaced(self):
        os.environ["SA_TEST_VAR"] = "hello"
        try:
            self.assertEqual(_sub_env("k: ${SA_TEST_VAR}"), "k: hello")
        finally:
            os.environ.pop("SA_TEST_VAR", None)

    def test_braced_var_default(self):
        self.assertEqual(_sub_env("k: ${SA_TEST_MISSING:-fallback}"), "k: fallback")


class LoadSettingsTest(unittest.TestCase):
    def test_password_hash_survives_loading(self):
        h = hash_password("pw-one")
        cfg = f"""engine:
  mode: "hermes"
data_dir: "data"
web:
  enabled: true
  title: "学习助手"
  users:
    - username: "kid1"
      display_name: "老大"
      password_hash: "{h}"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(cfg, encoding="utf-8")
            settings = load_settings(str(path))

        self.assertEqual(len(settings.web.users), 1)
        self.assertEqual(settings.web.users[0].username, "kid1")
        # 关键：哈希必须原样保留，且仍能校验通过
        self.assertEqual(settings.web.users[0].password_hash, h)
        self.assertTrue(verify_password_hash("pw-one", settings.web.users[0].password_hash))


if __name__ == "__main__":
    unittest.main()
