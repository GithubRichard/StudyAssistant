"""周总结测试：自然周划分、按科目统计口径、幂等、补生成、API。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
import httpx
from fastapi import FastAPI

from app import api, auth, db, weekly
from app.auth import web_openid
from tests.test_web_v1 import make_settings

SH = ZoneInfo("Asia/Shanghai")
WEEK = date(2026, 9, 21)  # 周一


def _ts(dt: datetime) -> float:
    return dt.replace(tzinfo=SH).timestamp()


async def seed_grading_task(db_path: str, openid: str, task_id: str, subject: str,
                            finished: datetime, statuses: list) -> None:
    ts = _ts(finished)
    result = {"questions": [
        {"id": f"q{i}", "no": str(i + 1), "status": st}
        for i, st in enumerate(statuses)]}
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO tasks(id, openid, subject, grade_level, image_path, status,
                                 task_type, input_text, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (task_id, openid, subject, "", "", "done", "grading", "", ts, ts))
        await conn.execute(
            """INSERT INTO task_runs(id, task_id, run_no, kind, input_text, status,
                                     hermes_session_id, created_at, finished_at, result_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (f"run-{task_id}", task_id, 1, "grade", "", "finished", "",
             ts, ts + 5, json.dumps(result)))
        await conn.commit()


async def seed_mistake(db_path: str, openid: str, uid: str, subject: str,
                       created: datetime, **overrides) -> None:
    row = {
        "openid": openid, "task_id": "t1", "question_no": "3",
        "knowledge_point": "一元一次方程", "note": "",
        "created_at": _ts(created),
        "subject": subject, "source": "", "page": "", "question_uid": uid,
        "stem": "解方程", "student_answer": "x=5", "correct_answer": "x=4",
        "error_rule": "移项未变号", "status": "wrong",
        "remediation_state": "pending_correction", "last_event_at": 0.0,
        "archive_path": "",
    }
    row.update(overrides)
    cols = ",".join(row.keys())
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            f"INSERT INTO mistakes({cols}) VALUES({','.join('?' for _ in row)})",
            tuple(row.values()))
        await conn.commit()


async def seed_event(db_path: str, openid: str, uid: str, subject: str,
                     event_type: str, created: datetime) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO question_events(id, openid, question_uid, subject, event_type,
                                           result, occurred_date, student_answer, note,
                                           source_task_id, archive_path, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"ev-{uid}-{event_type}", openid, uid, subject, event_type, "",
             created.strftime("%Y-%m-%d"), "", "", "t1", "", _ts(created)))
        await conn.commit()


class WeeklyLogicTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        await db.init_db(self.settings.db_path)
        self.db_path = self.settings.db_path
        self.openid = web_openid("kid1")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def test_last_complete_week_monday(self):
        # 周日 2026-09-27 03:00（上海）：9/21 那周的周日还没过完，
        # 上一个完整周是 9/14（周一）
        now = datetime(2026, 9, 27, 3, 0, tzinfo=SH)
        self.assertEqual(weekly.last_complete_week_monday(now), date(2026, 9, 14))
        # 周一 2026-09-28：上周（9/21~9/27）已完整
        now = datetime(2026, 9, 28, 12, 0, tzinfo=SH)
        self.assertEqual(weekly.last_complete_week_monday(now), date(2026, 9, 21))
        # 周六 2026-09-26：本周未结束，上一个完整周是 9/14
        now = datetime(2026, 9, 26, 12, 0, tzinfo=SH)
        self.assertEqual(weekly.last_complete_week_monday(now), date(2026, 9, 14))

    def test_week_bounds_seven_days(self):
        start, end = weekly.week_bounds(WEEK)
        self.assertEqual(end - start, 7 * 86400)
        self.assertEqual(datetime.fromtimestamp(start, SH).date(), WEEK)
        self.assertEqual(datetime.fromtimestamp(end, SH).date(), WEEK + timedelta(days=7))

    async def test_generate_for_user(self):
        mon = datetime(2026, 9, 21, 10, 0, tzinfo=SH)
        tue = datetime(2026, 9, 22, 10, 0, tzinfo=SH)
        # 数学：3 对 1 错 1 未作答 1 存疑；英语：2 对
        await seed_grading_task(self.db_path, self.openid, "t1", "数学", mon,
                                ["correct"] * 3 + ["wrong", "unanswered", "uncertain"])
        await seed_grading_task(self.db_path, self.openid, "t2", "英语", tue,
                                ["correct"] * 2)
        # 上周数学：1 对 1 错 -> 上周正确率 0.5
        await seed_grading_task(self.db_path, self.openid, "t0", "数学",
                                datetime(2026, 9, 15, 10, 0, tzinfo=SH),
                                ["correct", "wrong"])
        # 本周新增错题 2 条（1 条已撤回不计），错因/知识点各 1
        await seed_mistake(self.db_path, self.openid, "u1", "数学", tue)
        await seed_mistake(self.db_path, self.openid, "u2", "数学", tue,
                           remediation_state="withdrawn")
        # 本周 1 订正 + 1 复测；另有 1 条上周遗留待订正
        await seed_event(self.db_path, self.openid, "u1", "数学", "correction", tue)
        await seed_event(self.db_path, self.openid, "u1", "数学", "retest", tue)
        await seed_mistake(self.db_path, self.openid, "u0", "数学",
                           datetime(2026, 9, 10, 10, 0, tzinfo=SH))

        subjects = await weekly.generate_for_user(self.db_path, self.openid, WEEK)
        self.assertEqual(subjects, ["数学", "英语"])

        rows = await db.get_weekly_summaries(self.db_path, self.openid, "2026-09-21")
        by_sub = {r["subject"]: r["summary"] for r in rows}
        math = by_sub["数学"]
        self.assertEqual(math["tasks"], 1)
        self.assertEqual(math["questions"],
                         {"total": 6, "correct": 3, "wrong": 1,
                          "unanswered": 1, "uncertain": 1})
        self.assertAlmostEqual(math["accuracy"], 0.6)  # 3/(3+1+1)
        self.assertAlmostEqual(math["prev_accuracy"], 0.5)
        self.assertEqual(math["new_mistakes"], 1)  # withdrawn 的不计
        self.assertEqual(math["corrections"], 1)
        self.assertEqual(math["retests"], 1)
        self.assertEqual(math["top_causes"], [{"cause": "移项未变号", "count": 1}])
        self.assertEqual(math["top_points"], [{"point": "一元一次方程", "count": 1}])
        self.assertEqual(math["pending_correction"], 2)  # u1 + 上周遗留 u0

        eng = by_sub["英语"]
        self.assertEqual(eng["tasks"], 1)
        self.assertAlmostEqual(eng["accuracy"], 1.0)
        self.assertIsNone(eng["prev_accuracy"])
        self.assertEqual(eng["new_mistakes"], 0)

    async def test_generate_idempotent(self):
        mon = datetime(2026, 9, 21, 10, 0, tzinfo=SH)
        await seed_grading_task(self.db_path, self.openid, "t1", "数学", mon,
                                ["correct"])
        await weekly.generate_for_user(self.db_path, self.openid, WEEK)
        await weekly.generate_for_user(self.db_path, self.openid, WEEK)
        rows = await db.get_weekly_summaries(self.db_path, self.openid, "2026-09-21")
        self.assertEqual(len(rows), 1)

    async def test_generate_missing_sunday_2am_gate(self):
        # 周日 9/27 01:00：最近完整周（9/14）暂不生成，等 2 点后
        mon = datetime(2026, 9, 14, 10, 0, tzinfo=SH)
        await seed_grading_task(self.db_path, self.openid, "t1", "数学", mon,
                                ["correct", "wrong"])
        before = datetime(2026, 9, 27, 1, 0, tzinfo=SH)
        result = await weekly.generate_missing(self.db_path, weeks_back=8, now=before)
        self.assertNotIn(self.openid, result)
        self.assertFalse(await db.has_weekly_summary(
            self.db_path, self.openid, "2026-09-14"))
        # 03:00 则生成
        after = datetime(2026, 9, 27, 3, 0, tzinfo=SH)
        result = await weekly.generate_missing(self.db_path, weeks_back=8, now=after)
        self.assertEqual(result.get(self.openid), 1)
        self.assertTrue(await db.has_weekly_summary(
            self.db_path, self.openid, "2026-09-14"))

    async def test_generate_missing_skips_empty_weeks(self):
        # 只有 9/14 那周有数据 -> 只生成那一周，空周不产生空行
        mon = datetime(2026, 9, 14, 10, 0, tzinfo=SH)
        await seed_grading_task(self.db_path, self.openid, "t1", "数学", mon,
                                ["correct", "wrong"])
        result = await weekly.generate_missing(self.db_path, weeks_back=8)
        self.assertEqual(result.get(self.openid), 1)
        self.assertTrue(await db.has_weekly_summary(
            self.db_path, self.openid, "2026-09-14"))
        weeks = await db.list_weekly_weeks(self.db_path, self.openid)
        self.assertEqual(weeks, ["2026-09-14"])


class WeeklyApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        await db.init_db(self.settings.db_path)
        api.settings = self.settings
        app = FastAPI()
        app.include_router(api.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test")
        res = await self.client.post("/api/web/login",
                                     data={"username": "kid1", "password": "pw-one"})
        self.assertEqual(res.status_code, 200)
        token = res.json()["token"]
        self.headers = {"Authorization": f"Bearer {token}"}
        self.openid = web_openid("kid1")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_api_flow(self):
        db_path = self.settings.db_path
        mon = datetime(2026, 9, 21, 10, 0, tzinfo=SH)
        await seed_grading_task(db_path, self.openid, "t1", "数学", mon,
                                ["correct", "wrong"])
        # 手动触发
        res = await self.client.post("/api/web/weekly-summaries/generate",
                                     json={"week_start": "2026-09-21"},
                                     headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["subjects"], ["数学"])
        # 周列表
        res = await self.client.get("/api/web/weekly-summaries/weeks",
                                    headers=self.headers)
        self.assertEqual(res.json()["weeks"], ["2026-09-21"])
        # 默认取最近一周
        res = await self.client.get("/api/web/weekly-summaries",
                                    headers=self.headers)
        body = res.json()
        self.assertEqual(body["week_start"], "2026-09-21")
        self.assertEqual(body["week_end"], "2026-09-27")
        self.assertEqual(len(body["subjects"]), 1)
        summary = body["subjects"][0]["summary"]
        self.assertAlmostEqual(summary["accuracy"], 0.5)
        # 未登录 401
        res = await self.client.get("/api/web/weekly-summaries/weeks")
        self.assertEqual(res.status_code, 401)

    async def test_api_empty(self):
        res = await self.client.get("/api/web/weekly-summaries",
                                    headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["subjects"], [])


# ---------- AI 错题归类分析 ----------

from app.providers import GradeOutcome  # noqa: E402


class FakeAnalysisProvider:
    def __init__(self, name, script, calls, cfg=None):
        self.name = name
        self.cfg = cfg
        self._script = script
        self.calls = calls

    async def complete_text(self, system, user, max_tokens=4000):
        self.calls.append({"system": system, "user": user,
                           "max_tokens": max_tokens, "provider": self.name})
        queue = self._script.get(self.name, [])
        assert queue, f"fake provider {self.name} 没有剧本"
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, GradeOutcome):
            return item
        return GradeOutcome(text=item, input_tokens=100, output_tokens=200,
                            provider=self.name, model=f"fake-{self.name}")


def analysis_factory(scripts: dict, calls: list):
    def factory(name, cfg):
        return FakeAnalysisProvider(name, scripts, calls, cfg)
    return factory


ANALYSIS_OK = json.dumps({
    "categories": [
        {"name": "计算失误", "question_nos": ["3", "5"],
         "pattern": "移项时忘记变号", "advice": "做完后代入验算"},
        {"name": "概念不清", "question_nos": ["7"],
         "pattern": "一元一次方程定义记混", "advice": "重看课本定义"},
    ],
    "knowledge_summary": "本周薄弱点集中在一元一次方程的移项与求解，建议重点复习。",
    "focus_next_week": ["移项变号专项练习", "方程应用题"],
}, ensure_ascii=False)


def make_ai_settings(tmp: str):
    from app.config import ProviderConfig
    s = make_settings(tmp)
    s.llm.providers["p1"] = ProviderConfig(
        base_url="http://fake1", api_key="k1", model="m1", enabled=True)
    s.llm.providers["p2"] = ProviderConfig(
        base_url="http://fake2", api_key="k2", model="m2", enabled=True)
    s.llm.default_provider = "p1"
    s.llm.fallback_order = ["p2"]
    return s


class WeeklyAiAnalysisTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_ai_settings(self.tmp.name)
        await db.init_db(self.settings.db_path)
        self.db_path = self.settings.db_path
        self.openid = web_openid("kid1")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _seed_week_mistakes(self):
        tue = datetime(2026, 9, 22, 10, 0, tzinfo=SH)
        await seed_mistake(self.db_path, self.openid, "u1", "数学", tue)
        await seed_mistake(self.db_path, self.openid, "u2", "数学", tue,
                           question_no="5", knowledge_point="因式分解",
                           error_rule="符号错误")

    async def test_analyze_success(self):
        await self._seed_week_mistakes()
        mistakes = await db.list_week_mistakes(
            self.db_path, self.openid, "数学", *_ts_range(), limit=30)
        self.assertEqual(len(mistakes), 2)
        calls = []
        out = await weekly._analyze_mistakes(
            self.settings, "数学", mistakes,
            provider_factory=analysis_factory({"p1": [ANALYSIS_OK], "p2": []}, calls))
        self.assertEqual(len(out["categories"]), 2)
        self.assertEqual(out["categories"][0]["name"], "计算失误")
        self.assertEqual(out["categories"][0]["question_nos"], ["3", "5"])
        self.assertIn("移项", out["knowledge_summary"])
        self.assertEqual(len(out["focus_next_week"]), 2)
        self.assertEqual(out["analyzed_mistakes"], 2)
        self.assertTrue(out["model"].startswith("p1/"))
        self.assertEqual(len(calls), 1)

    async def test_analyze_fallback(self):
        mistakes = [{"question_no": "3", "stem": "解方程", "student_answer": "x=5",
                     "correct_answer": "x=4", "error_rule": "", "knowledge_point": ""}]
        calls = []
        out = await weekly._analyze_mistakes(
            self.settings, "数学", mistakes,
            provider_factory=analysis_factory(
                {"p1": [RuntimeError("boom")], "p2": [ANALYSIS_OK]}, calls))
        self.assertEqual(len(out["categories"]), 2)
        self.assertTrue(out["model"].startswith("p2/"))
        self.assertEqual(len(calls), 2)

    async def test_analyze_all_fail_keeps_stats(self):
        await self._seed_week_mistakes()
        scripts = {"p1": [RuntimeError("boom1")], "p2": [RuntimeError("boom2")]}
        subjects = await weekly.generate_for_user(
            self.db_path, self.openid, WEEK, settings=self.settings,
            provider_factory=analysis_factory(scripts, []))
        self.assertEqual(subjects, ["数学"])
        rows = await db.get_weekly_summaries(self.db_path, self.openid, "2026-09-21")
        summary = rows[0]["summary"]
        # 统计部分照常落库
        self.assertEqual(summary["new_mistakes"], 2)
        # AI 部分记 error，不阻断
        self.assertIn("error", summary["ai_analysis"])
        self.assertIn("boom1", summary["ai_analysis"]["error"])

    async def test_no_settings_skips_ai(self):
        await self._seed_week_mistakes()
        await weekly.generate_for_user(self.db_path, self.openid, WEEK)
        rows = await db.get_weekly_summaries(self.db_path, self.openid, "2026-09-21")
        self.assertIsNone(rows[0]["summary"]["ai_analysis"])

    async def test_ai_disabled_by_config(self):
        self.settings.weekly_summary.ai_analysis = False
        await self._seed_week_mistakes()
        await weekly.generate_for_user(
            self.db_path, self.openid, WEEK, settings=self.settings,
            provider_factory=analysis_factory({"p1": [ANALYSIS_OK], "p2": []}, []))
        rows = await db.get_weekly_summaries(self.db_path, self.openid, "2026-09-21")
        self.assertIsNone(rows[0]["summary"]["ai_analysis"])

    async def test_no_mistakes_no_analysis_call(self):
        mon = datetime(2026, 9, 21, 10, 0, tzinfo=SH)
        await seed_grading_task(self.db_path, self.openid, "t1", "数学", mon,
                                ["correct"])
        calls = []
        await weekly.generate_for_user(
            self.db_path, self.openid, WEEK, settings=self.settings,
            provider_factory=analysis_factory({"p1": [ANALYSIS_OK], "p2": []}, calls))
        # 没有错题就不调模型
        self.assertEqual(calls, [])
        rows = await db.get_weekly_summaries(self.db_path, self.openid, "2026-09-21")
        self.assertIsNone(rows[0]["summary"]["ai_analysis"])


def _ts_range():
    start, end = weekly.week_bounds(WEEK)
    return start, end


if __name__ == "__main__":
    unittest.main()
