"""受控 Git 同步测试：只提交授权文件、原题不入库、无变化不建空提交、冲突停止上传。

全部使用临时目录中的真实 git 仓库，不触碰仓库自身与用户的 Git 配置。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app import git_sync, workspace
from app.config import Settings


async def run_git(cwd: Path, *args: str) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode().strip(), err.decode().strip()


def make_settings(tmp: str, repo: Path, **overrides) -> Settings:
    data = {
        "engine": {"mode": "hermes"},
        "hermes": {"base_url": "", "api_key": ""},
        "data_dir": str(Path(tmp) / "data"),
        "workspace": {"dir": str(repo), "init_readme": False},
        "git": {"enabled": True, "remote": "origin", "timeout_seconds": 30},
    }
    data.update(overrides)
    return Settings.model_validate(data)


class GitSyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "workspace"
        self.repo.mkdir(parents=True, exist_ok=True)
        self.remote = base / "remote.git"
        await run_git(self.repo, "init")
        await run_git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        await run_git(self.repo, "config", "user.name", "test")
        await run_git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "README.md").write_text("学习工作区\n", encoding="utf-8")
        await run_git(self.repo, "add", "--", "README.md")
        await run_git(self.repo, "commit", "-m", "init")
        await run_git(self.remote.parent, "init", "--bare", str(self.remote))
        # bare 仓库默认 HEAD 可能是 master，显式指向 main，避免 clone 得到空仓库
        await run_git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        await run_git(self.repo, "remote", "add", "origin", str(self.remote))
        await run_git(self.repo, "push", "-u", "origin", "main")

        self.settings = make_settings(self.tmp.name, self.repo)
        workspace.ensure_workspace(self.settings)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _archive(self, relative: str = "数学/错题解析/2026-09-26.md") -> dict:
        target = self.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("## 来源：9月3周作业\n\n- 第 1 题：移项未变号\n", encoding="utf-8")
        return {"status": "generated", "path": str(target), "note": ""}

    def _task(self) -> dict:
        return {"id": "t1", "subject": "数学", "task_type": "grading"}

    def _result(self) -> dict:
        return {"subject": "数学", "scope": {"end_date": "2026-09-26"}}

    async def test_disabled_returns_not_configured(self):
        settings = make_settings(self.tmp.name, self.repo,
                                 git={"enabled": False}, delivery={"git_enabled": False})
        out = await git_sync.sync_workspace(
            settings, task=self._task(), run={"run_no": 1},
            archive=self._archive(), result=self._result())
        self.assertEqual(out["status"], "not_configured")
        self.assertFalse(out["committed"])

    async def test_commits_and_pushes_only_archive(self):
        # 原题资料放在原题目录，必须保持不被提交
        original = self.repo / "数学" / "原题" / "2026" / "9月3周" / "page1.jpg"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"fake-image")

        out = await git_sync.sync_workspace(
            self.settings, task=self._task(), run={"run_no": 1},
            archive=self._archive(), result=self._result())

        self.assertEqual(out["status"], "committed", out)
        self.assertTrue(out["committed"])
        self.assertTrue(out["pushed"])
        self.assertEqual(out["paths"], ["数学/错题解析/2026-09-26.md"])

        code, tracked, _ = await run_git(self.repo, "ls-files")
        self.assertIn("数学/错题解析/2026-09-26.md", tracked)
        self.assertNotIn("page1.jpg", tracked)

        code, remote_log, _ = await run_git(self.remote, "log", "--oneline", "main")
        self.assertIn("学习记录", remote_log)

    async def test_no_change_creates_no_empty_commit(self):
        archive = self._archive()
        first = await git_sync.sync_workspace(
            self.settings, task=self._task(), run={"run_no": 1},
            archive=archive, result=self._result())
        self.assertEqual(first["status"], "committed")

        second = await git_sync.sync_workspace(
            self.settings, task=self._task(), run={"run_no": 2},
            archive=archive, result=self._result())
        self.assertEqual(second["status"], "skipped")
        self.assertIn("没有变化", second["reason"])

    async def test_foreign_staged_change_stops_upload(self):
        (self.repo / "其他文件.md").write_text("用户的改动\n", encoding="utf-8")
        await run_git(self.repo, "add", "--", "其他文件.md")

        out = await git_sync.sync_workspace(
            self.settings, task=self._task(), run={"run_no": 1},
            archive=self._archive(), result=self._result())
        self.assertEqual(out["status"], "failed")
        self.assertIn("暂存区已存在", out["reason"])

    async def test_missing_upstream_stops_upload(self):
        await run_git(self.repo, "checkout", "-b", "feature")
        out = await git_sync.sync_workspace(
            self.settings, task=self._task(), run={"run_no": 1},
            archive=self._archive(), result=self._result())
        self.assertEqual(out["status"], "failed")
        self.assertIn("上游分支", out["reason"])

    async def test_non_fast_forward_writes_conflict_record_and_stops(self):
        # 远端被别人推进一步，本地再提交 → 推送被拒（分支分歧）
        other = Path(self.tmp.name) / "other"
        code, out, err = await run_git(Path(self.tmp.name), "clone", str(self.remote), str(other))
        self.assertEqual(code, 0, f"clone 失败: {out} {err}")
        await run_git(other, "config", "user.name", "other")
        await run_git(other, "config", "user.email", "other@example.com")
        (other / "note.md").write_text("远端改动\n", encoding="utf-8")
        await run_git(other, "add", "--", "note.md")
        code, out, err = await run_git(other, "commit", "-m", "远端提交")
        self.assertEqual(code, 0, f"远端提交失败: {out} {err}")
        code, out, err = await run_git(other, "push", "origin", "main")
        self.assertEqual(code, 0, f"远端推送失败: {out} {err}")

        out = await git_sync.sync_workspace(
            self.settings, task=self._task(), run={"run_no": 1},
            archive=self._archive(), result=self._result())

        self.assertEqual(out["status"], "failed")
        self.assertTrue(out["committed"])
        self.assertFalse(out["pushed"])
        self.assertTrue(out["conflict_record"], out)
        record = self.repo / out["conflict_record"]
        self.assertTrue(record.exists())
        text = record.read_text(encoding="utf-8")
        self.assertIn("未推送", text)
        self.assertIn("当前状态与待处理事项", text)

        # 冲突记录本身不自动提交
        code, tracked, _ = await run_git(self.repo, "ls-files")
        self.assertNotIn(out["conflict_record"], tracked)

    async def test_not_a_git_repo_fails_honestly(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir(parents=True, exist_ok=True)
        settings = make_settings(self.tmp.name, plain)
        target = plain / "数学" / "错题解析" / "2026-09-26.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
        out = await git_sync.sync_workspace(
            settings, task=self._task(), run={"run_no": 1},
            archive={"status": "generated", "path": str(target)}, result=self._result())
        self.assertEqual(out["status"], "failed")
        self.assertIn("不是 Git 仓库", out["reason"])


if __name__ == "__main__":
    unittest.main()
