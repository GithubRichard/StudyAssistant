"""每周日凌晨生成「周总结」：按账号×科目汇总上一完整自然周的学习数据。

自然周按 Asia/Shanghai 时区划分（周一 00:00 ~ 周日 23:59:59），与服务器
所在时区无关。生成结果写入 weekly_summaries 表，同一周重复生成直接覆盖
（幂等），前端通过 API 读取展示。

统计口径（每科目）：
- tasks：本周完成的批改任务数
- questions：批改题目数（答对/答错/未作答/存疑）；uncertain 未给出确定
  结论，不计入正确率分母
- accuracy：correct / (correct + wrong + unanswered)
- prev_accuracy：上周同科目正确率（环比；无上周数据则为 None）
- new_mistakes：本周新增台账条目（已撤回的不计）
- corrections / retests：本周订正 / 复测事件数
- top_causes / top_points：本周高频错因 / 薄弱知识点 TOP5
- pending_correction / pending_retest：截至生成时的待办快照
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from . import db

log = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 每周日这个时刻之后生成上一周的总结（上海时间）
GENERATE_AFTER_HOUR = 2
#: 补生成往回看的周数（覆盖服务器周日宕机的情况）
LOOKBACK_WEEKS = 8


def week_bounds(week_start: date) -> tuple[float, float]:
    """给定周一日期，返回该自然周 [周一00:00, 下周一00:00) 的时间戳（上海时区）。"""
    start = datetime(week_start.year, week_start.month, week_start.day,
                     tzinfo=SHANGHAI)
    end = start + timedelta(days=7)
    return start.timestamp(), end.timestamp()


def last_complete_week_monday(now: Optional[datetime] = None) -> date:
    """最近一个完整自然周的周一日期（上海时间）。

    例如周日 2026-09-27 调用返回 2026-09-21（周一）。
    """
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    this_monday = (now - timedelta(days=now.isoweekday() - 1)).date()
    return this_monday - timedelta(days=7)


def _accuracy(stats: dict) -> Optional[float]:
    checked = stats.get("checked", 0)
    if checked <= 0:
        return None
    return round(stats["correct"] / checked, 4)


async def generate_for_user(db_path: str, openid: str, week_start: date) -> List[str]:
    """为单个账号生成某周的周总结，返回已生成的科目列表。"""
    week_key = week_start.isoformat()
    start_ts, end_ts = week_bounds(week_start)
    prev_start, prev_end = week_bounds(week_start - timedelta(days=7))

    grading = await db.weekly_grading_by_subject(db_path, openid, start_ts, end_ts)
    prev_grading = await db.weekly_grading_by_subject(db_path, openid, prev_start, prev_end)
    mistakes = await db.weekly_mistake_stats(db_path, openid, start_ts, end_ts)
    events = await db.weekly_event_counts(db_path, openid, start_ts, end_ts)
    pending = await db.weekly_pending_by_subject(db_path, openid)

    subjects = set(grading) | set(mistakes) | set(events)
    generated: List[str] = []
    week_end = week_start + timedelta(days=6)
    for subject in sorted(subjects):
        g = grading.get(subject, {})
        m = mistakes.get(subject, {})
        e = events.get(subject, {})
        p = pending.get(subject, {})
        pg = prev_grading.get(subject, {})
        summary = {
            "week_start": week_key,
            "week_end": week_end.isoformat(),
            "subject": subject,
            "tasks": int(g.get("tasks", 0)),
            "questions": {
                "total": int(g.get("checked", 0)) + int(g.get("uncertain", 0)),
                "correct": int(g.get("correct", 0)),
                "wrong": int(g.get("wrong", 0)),
                "unanswered": int(g.get("unanswered", 0)),
                "uncertain": int(g.get("uncertain", 0)),
            },
            "accuracy": _accuracy(g),
            "prev_accuracy": _accuracy(pg),
            "new_mistakes": int(m.get("new_mistakes", 0)),
            "corrections": int(e.get("corrections", 0)),
            "retests": int(e.get("retests", 0)),
            "top_causes": m.get("top_causes", []),
            "top_points": m.get("top_points", []),
            "pending_correction": int(p.get("pending_correction", 0)),
            "pending_retest": int(p.get("pending_retest", 0)),
        }
        await db.upsert_weekly_summary(db_path, openid, week_key, subject, summary)
        generated.append(subject)
    if generated:
        log.info("周总结已生成: openid=%s week=%s 科目=%s",
                 openid, week_key, "、".join(generated))
    return generated


def _candidate_weeks(now: datetime, weeks_back: int) -> List[date]:
    """待检查的周一日期列表（倒序）：全部是已完整结束的自然周。

    周日凌晨 GENERATE_AFTER_HOUR 点之前，最近一个完整周暂不生成，
    等过 2 点后再说（"每周日凌晨"）。
    """
    latest = last_complete_week_monday(now)
    weeks = [latest - timedelta(days=7 * i) for i in range(weeks_back)]
    if now.isoweekday() == 7 and now.hour < GENERATE_AFTER_HOUR:
        weeks = weeks[1:]
    return weeks


async def generate_missing(db_path: str, weeks_back: int = LOOKBACK_WEEKS,
                           now: Optional[datetime] = None) -> Dict[str, int]:
    """补生成缺失的周总结：过去 weeks_back 个完整周中有数据但未生成的。

    每周日凌晨由定时循环调用；服务器周日宕机重启后也能在这里补上。
    返回 {openid: 生成的周数}。
    """
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    weeks = _candidate_weeks(now, weeks_back)

    result: Dict[str, int] = {}
    try:
        openids = await db.list_user_openids(db_path)
    except Exception:
        log.exception("周总结：获取账号列表失败")
        return result
    for openid in openids:
        done = 0
        for week_start in weeks:
            week_key = week_start.isoformat()
            try:
                if await db.has_weekly_summary(db_path, openid, week_key):
                    continue
                start_ts, end_ts = week_bounds(week_start)
                if not await db.week_has_data(db_path, openid, start_ts, end_ts):
                    continue
                await generate_for_user(db_path, openid, week_start)
                done += 1
            except Exception:
                log.exception("周总结生成失败: openid=%s week=%s", openid, week_key)
        if done:
            result[openid] = done
    return result
