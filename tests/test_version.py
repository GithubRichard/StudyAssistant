"""服务端版本号：SA_GIT_VERSION → git rev-parse → unknown。"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app import version


class GitVersionTest(unittest.TestCase):
    def setUp(self):
        version.git_version.cache_clear()
        self._old = os.environ.get("SA_GIT_VERSION")

    def tearDown(self):
        version.git_version.cache_clear()
        if self._old is None:
            os.environ.pop("SA_GIT_VERSION", None)
        else:
            os.environ["SA_GIT_VERSION"] = self._old

    def test_env_var_wins(self):
        os.environ["SA_GIT_VERSION"] = "abc1234"
        self.assertEqual(version.git_version(), "abc1234")

    def test_env_var_blank_falls_through(self):
        os.environ["SA_GIT_VERSION"] = "  "
        with patch("subprocess.run", side_effect=OSError("no git")):
            self.assertEqual(version.git_version(), "unknown")

    def test_git_failure_gives_unknown(self):
        os.environ.pop("SA_GIT_VERSION", None)
        with patch("subprocess.run", side_effect=OSError("no git")):
            self.assertEqual(version.git_version(), "unknown")

    def test_real_repo_returns_hash(self):
        # 本仓库就是 git  checkout：应返回短哈希而非 unknown
        os.environ.pop("SA_GIT_VERSION", None)
        got = version.git_version()
        self.assertNotEqual(got, "unknown")
        self.assertTrue(got.strip())
        self.assertLessEqual(len(got), 40)


if __name__ == "__main__":
    unittest.main()
