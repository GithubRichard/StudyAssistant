"""任务链路测试：鉴权归属、幂等与配额、执行器结果、中断恢复、补充材料。"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from app import auth, db, tasks
from app.config import Settings
from app.hermes import HermesAuthError, HermesUncertain
from app.schemas import request_hash
from tests.mock_hermes import LEARNING_RESULT


def make_settings(tmp: str, **overrides) -> Settings:
    data = {
        "engine": {"mode": "hermes"},
        "hermes": {"base_url": "http://hermes.local", "api_key": "k"},
        "data_dir": str(Path(tmp) / "data"),
        "workspace": {"dir": str(Path(tmp) / "workspace")},
        "limits": {"worker_enabled": False, "worker_poll_seconds": 0.05},
    }
    data.update(overrides)
    return Settings.model_validate(data)


class FakeClient:
    """替代 HermesClient：返回固定结果或指定异常，并记录调用次数。"""

    def __init__(self, result=None, error=None) -> None:
        self.result = result or dict(LEARNING_RESULT)
        self.error = error
        self.calls = 0

    async def run_task(self, messages, session_id):
        self.calls += 1
        if self.error:
            raise self.error
        return {"result": self.result, "model": "hermes-agent",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


async def seed_user(settings: Settings, openid: str) -> None:
    await db.get_or_create_user(settings.db_path, openid, settings.quota.new_user_bonus)


async def seed_asset(settings: Settings, openid: str, asset_id: str) -> None:
    await db.create_asset(settings.db_path, {
        "id": asset_id, "openid": openid, "sha256": "0" * 64, "mime": "image/jpeg",
        "bytes": 100, "width": 10, "height": 10,
        "path": str(Path(settings.upload_dir) / f"{asset_id}.jpg"), "created_at": time.time(),
    })


class SessionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        await db.init_db(self.settings.db_path)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_issue_and_authenticate(self):
        await seed_user(self.settings, "u1")
        session = await auth.issue_session(self.settings.db_path, "u1", [])
        ctx = await auth.authenticate(self.settings.db_path, session["token"])
        self.assertEqual(ctx["openid"], "u1")
        # 令牌明文不落库
        rows = await db.get_session_by_token(self.settings.db_path, session["token"])
        self.assertIsNone(rows)

    async def test_invalid_and_expired_tokens_rejected(self):
        with self.assertRaises(auth.AuthError):
            await auth.authenticate(self.settings.db_path, "nope-nope-nope")
        await seed_user(self.settings, "u1")
        session = await auth.issue_session(self.settings.db_path, "u1", [], ttl_seconds=-1)
        with self.assertRaises(auth.AuthError):
            await auth.authenticate(self.settings.db_path, session["token"])

    async def test_allowlist_blocks_unauthorized_account(self):
        await seed_user(self.settings, "u2")
        with self.assertRaises(auth.AuthError) as ctx:
            await auth.issue_session(self.settings.db_path, "u2", ["u1"])
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_ensure_owner_hides_other_users_resources(self):
        session = {"openid": "u1"}
        with self.assertRaises(auth.AuthError) as ctx:
            auth.ensure_owner("u2", session)
        self.assertEqual(ctx.exception.status_code, 404)


class QuotaTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        await db.init_db(self.settings.db_path)
        await seed_user(self.settings, "u1")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_reserve_settle_does_not_double_charge(self):
        before = await db.quota_remaining(self.settings.db_path, "u1", 3)
        res = await db.reserve_quota(self.settings.db_path, "u1", "t1", 3, 50)
        self.assertTrue(res["ok"])
        self.assertEqual(await db.quota_remaining(self.settings.db_path, "u1", 3), before - 1)
        await db.settle_reservation(self.settings.db_path, "t1")
        await db.settle_reservation(self.settings.db_path, "t1")
        self.assertEqual(await db.quota_remaining(self.settings.db_path, "u1", 3), before - 1)

    async def test_release_with_refund_restores_quota(self):
        before = await db.quota_remaining(self.settings.db_path, "u1", 3)
        await db.reserve_quota(self.settings.db_path, "u1", "t2", 3, 50)
        await db.release_reservation(self.settings.db_path, "t2", refund=True)
        self.assertEqual(await db.quota_remaining(self.settings.db_path, "u1", 3), before)

    async def test_unknown_user_and_daily_cap(self):
        bad = await db.reserve_quota(self.settings.db_path, "ghost", "t3", 3, 50)
        self.assertFalse(bad["ok"])
        await db.reserve_quota(self.settings.db_path, "u1", "t4", 3, 1)
        capped = await db.reserve_quota(self.settings.db_path, "u1", "t5", 3, 1)
        self.assertFalse(capped["ok"])
        self.assertIn("上限", capped["reason"])


class CreationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        await db.init_db(self.settings.db_path)
        await seed_user(self.settings, "u1")
        await seed_user(self.settings, "u2")
        await seed_asset(self.settings, "u1", "a1")
        await seed_asset(self.settings, "u2", "a2")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_idempotent_creation_returns_same_task(self):
        payload = {"task_type": "grading", "subject": "数学", "text": "", "asset_ids": ["a1"]}
        first = await tasks.create_study_task(self.settings, "u1", payload, "key-1")
        second = await tasks.create_study_task(self.settings, "u1", payload, "key-1")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertTrue(second["duplicate"])
        remaining = await db.quota_remaining(self.settings.db_path, "u1", 3)
        self.assertEqual(remaining, self.settings.quota.new_user_bonus + 3 - 1,
                         "重复提交不能重复扣次")

    async def test_same_key_different_content_conflicts(self):
        await tasks.create_study_task(self.settings, "u1",
                                     {"task_type": "qa", "text": "问题", "asset_ids": []}, "key-2")
        with self.assertRaises(tasks.TaskError) as ctx:
            await tasks.create_study_task(
                self.settings, "u1",
                {"task_type": "qa", "text": "另一个问题", "asset_ids": []}, "key-2")
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_cannot_use_other_users_asset(self):
        with self.assertRaises(tasks.TaskError) as ctx:
            await tasks.create_study_task(
                self.settings, "u1",
                {"task_type": "grading", "text": "", "asset_ids": ["a2"]}, "")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_asset_count_and_text_required(self):
        with self.assertRaises(tasks.TaskError):
            await tasks.create_study_task(self.settings, "u1",
                                         {"task_type": "qa", "text": "", "asset_ids": []}, "")

    async def test_request_hash_is_stable(self):
        payload = {"a": 1, "b": [2, 3]}
        self.assertEqual(request_hash(payload), request_hash({"b": [2, 3], "a": 1}))


async def run_executor(settings: Settings, client) -> None:
    runner = tasks.TaskRunner(settings, client)
    task = await db.claim_next_task(settings.db_path, "tester", 60)
    assert task is not None
    await runner.execute(task)


class ExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        await db.init_db(self.settings.db_path)
        await seed_user(self.settings, "u1")
        await seed_asset(self.settings, "u1", "a1")
        # 需要真实文件才能构造 data_url
        path = Path(self.settings.upload_dir) / "a1.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        from tests.test_workspace import png_bytes
        path.write_bytes(png_bytes())

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _create(self):
        return await tasks.create_study_task(
            self.settings, "u1",
            {"task_type": "grading", "subject": "数学", "text": "", "asset_ids": ["a1"]}, "")

    async def test_success_writes_archive_and_settles_quota(self):
        created = await self._create()
        client = FakeClient()
        await run_executor(self.settings, client)

        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(client.calls, 1)
        # 结果包含 missing_info → 待补充材料
        self.assertEqual(task["status"], "waiting_input")
        self.assertEqual(task["provider"], "hermes")

        # 归档落在该账号自己的目录下（openid "u1" → 账号目录 wx-u1）
        archive = (Path(self.settings.workspace_dir) / "wx-u1" / "数学" / "错题解析"
                   / "2026-09-26.md")
        self.assertTrue(archive.exists())
        self.assertIn("移项未变号", archive.read_text(encoding="utf-8"))

        artifacts = await db.list_artifacts(self.settings.db_path, created["task_id"])
        self.assertTrue(any(a["kind"] == "archive" for a in artifacts))

        reservation = await db.get_idempotency(self.settings.db_path, "nonexistent")
        self.assertIsNone(reservation)

    async def test_uncertain_result_is_interrupted_and_keeps_quota_reserved(self):
        before = await db.quota_remaining(self.settings.db_path, "u1", 3)
        created = await self._create()
        await run_executor(self.settings, FakeClient(error=HermesUncertain("读取超时")))

        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(task["status"], "interrupted")
        self.assertIn("未自动重试", task["error"])
        after = await db.quota_remaining(self.settings.db_path, "u1", 3)
        self.assertEqual(after, before - 1, "结果未确认时保持已扣次数")

    async def test_certain_failure_refunds_quota(self):
        before = await db.quota_remaining(self.settings.db_path, "u1", 3)
        created = await self._create()
        await run_executor(self.settings, FakeClient(error=HermesAuthError("鉴权失败")))

        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(task["status"], "failed")
        after = await db.quota_remaining(self.settings.db_path, "u1", 3)
        self.assertEqual(after, before, "确认未执行应退还次数")

    async def test_restart_marks_dispatched_task_interrupted(self):
        created = await self._create()
        await db.update_task(self.settings.db_path, created["task_id"], status="grading")
        recovered = await db.recover_interrupted(self.settings.db_path)
        self.assertIn(created["task_id"], recovered)
        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(task["status"], "interrupted")

    async def test_followup_blocked_while_result_unconfirmed(self):
        created = await self._create()
        await run_executor(self.settings, FakeClient(error=HermesUncertain("超时")))
        with self.assertRaises(tasks.TaskError) as ctx:
            await tasks.add_followup(self.settings, "u1", created["task_id"],
                                     {"text": "补充", "asset_ids": []})
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_followup_creates_second_run(self):
        created = await self._create()
        await run_executor(self.settings, FakeClient())
        res = await tasks.add_followup(self.settings, "u1", created["task_id"],
                                      {"text": "补充：开学日期 9 月 1 日", "asset_ids": []})
        self.assertEqual(res["run_no"], 2)
        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["run_count"], 2)

        await run_executor(self.settings, FakeClient())
        task = await db.get_task(self.settings.db_path, created["task_id"])
        self.assertEqual(task["status"], "waiting_input")
        runs = await db.list_runs(self.settings.db_path, created["task_id"])
        self.assertEqual([r["run_no"] for r in runs], [1, 2])

    async def test_task_view_reads_legacy_result(self):
        legacy = {"total_questions": 2, "correct_count": 1, "summary": "旧结果",
                  "questions": [{"no": "1", "student_answer": "x=2", "is_correct": False,
                                 "correct_answer": "x=3", "explanation": ["移项变号"],
                                 "knowledge_point": "方程"}]}
        created = await self._create()
        await db.update_task(self.settings.db_path, created["task_id"], status="done",
                            result_json=__import__("json").dumps(legacy, ensure_ascii=False))
        task = await db.get_task(self.settings.db_path, created["task_id"])
        view = await tasks.build_task_view(self.settings, task)
        self.assertTrue(view["result"]["legacy"])
        self.assertEqual(view["result"]["questions"][0]["status"], "wrong")
        self.assertEqual(view["result"]["review_summary"]["state"], "not_run")


async def _git(cwd: Path, *args: str):
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode().strip(), err.decode().strip()


class GitDeliveryTest(unittest.IsolatedAsyncioTestCase):
    """归档与受控 Git 交付的串联：真实提交、失败如实上报且不阻断归档。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "workspace"
        self.repo.mkdir(parents=True, exist_ok=True)
        self.settings = make_settings(
            self.tmp.name, git={"enabled": True, "remote": "origin", "timeout_seconds": 30})
        await db.init_db(self.settings.db_path)
        await seed_user(self.settings, "u1")
        await seed_asset(self.settings, "u1", "a1")
        # 需要真实文件才能构造 data_url
        from tests.test_workspace import png_bytes
        asset_path = Path(self.settings.upload_dir) / "a1.jpg"
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(png_bytes())

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _init_repo(self) -> None:
        base = Path(self.tmp.name)
        await _git(self.repo, "init")
        await _git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        await _git(self.repo, "config", "user.name", "test")
        await _git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "README.md").write_text("学习工作区\n", encoding="utf-8")
        await _git(self.repo, "add", "--", "README.md")
        await _git(self.repo, "commit", "-m", "init")
        remote = base / "remote.git"
        await _git(base, "init", "--bare", str(remote))
        await _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
        await _git(self.repo, "remote", "add", "origin", str(remote))
        await _git(self.repo, "push", "-u", "origin", "main")

    async def _create_and_run(self) -> str:
        created = await tasks.create_study_task(
            self.settings, "u1",
            {"task_type": "grading", "subject": "数学", "text": "", "asset_ids": ["a1"]}, "")
        await run_executor(self.settings, FakeClient())
        return created["task_id"]

    async def test_archive_is_committed_and_pushed(self):
        await self._init_repo()
        task_id = await self._create_and_run()

        task = await db.get_task(self.settings.db_path, task_id)
        self.assertEqual(task["git_status"], "committed")
        self.assertEqual(task["archive_path"].endswith("2026-09-26.md"), True)

        view = await tasks.build_task_view(self.settings, task)
        git_delivery = view["result"]["delivery"]["git"]
        self.assertEqual(git_delivery["status"], "committed")
        self.assertTrue(git_delivery["committed"])
        self.assertTrue(git_delivery["pushed"])
        self.assertTrue(git_delivery["commit"])
        # 台账条目与去重键一并落库
        self.assertTrue(view["ledger"])
        self.assertTrue(view["ledger"][0]["question_uid"].startswith("q-"))

        code, tracked, _ = await _git(self.repo, "ls-files")
        self.assertIn("数学/错题解析/2026-09-26.md", tracked)

        last = await db.latest_git_sync(self.settings.db_path)
        self.assertEqual(last["status"], "committed")
        self.assertTrue(last["pushed"])

    async def test_git_failure_does_not_block_archive_and_is_reported(self):
        # 工作区不是 Git 仓库：归档成功、git 如实报告失败
        task_id = await self._create_and_run()
        task = await db.get_task(self.settings.db_path, task_id)
        view = await tasks.build_task_view(self.settings, task)
        self.assertEqual(view["result"]["delivery"]["archive"]["status"], "generated")
        git_delivery = view["result"]["delivery"]["git"]
        self.assertEqual(git_delivery["status"], "failed")
        self.assertIn("不是 Git 仓库", git_delivery["note"])
        self.assertFalse(git_delivery["pushed"])

    async def test_disabled_git_reports_not_configured(self):
        # 复用同一工作区与数据库，只把 Git 同步开关关掉
        disabled = make_settings(self.tmp.name)
        created = await tasks.create_study_task(
            disabled, "u1",
            {"task_type": "grading", "subject": "数学", "text": "", "asset_ids": ["a1"]}, "")
        await run_executor(disabled, FakeClient())
        task = await db.get_task(disabled.db_path, created["task_id"])
        view = await tasks.build_task_view(disabled, task)
        self.assertEqual(view["result"]["delivery"]["git"]["status"], "not_configured")


if __name__ == "__main__":
    unittest.main()
