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

from . import db, thinking

log = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 每周日这个时刻之后生成上一周的总结（上海时间）
GENERATE_AFTER_HOUR = 2
#: 补生成往回看的周数（覆盖服务器周日宕机的情况）
LOOKBACK_WEEKS = 8
#: 送分析时单个字段的最大长度（防超长题干烧 token）
_ANALYSIS_FIELD_LEN = 120

_ANALYSIS_SYSTEM = (
    "你是初中学习分析师，擅长从错题中发现规律、定位知识漏洞。"
    "请只输出 JSON，不要输出任何解释文字。"
)


def _truncate(text: str, limit: int = _ANALYSIS_FIELD_LEN) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _build_analysis_prompt(subject: str, mistakes: List[dict]) -> str:
    lines = []
    for m in mistakes:
        parts = [f"第{m['question_no'] or '?'}题"]
        if m["stem"]:
            parts.append(f"题干：{_truncate(m['stem'])}")
        if m["student_answer"]:
            parts.append(f"学生答案：{_truncate(m['student_answer'])}")
        if m["correct_answer"]:
            parts.append(f"正确答案：{_truncate(m['correct_answer'])}")
        if m["error_rule"]:
            parts.append(f"批改记录错因：{_truncate(m['error_rule'])}")
        if m["knowledge_point"]:
            parts.append(f"知识点：{_truncate(m['knowledge_point'])}")
        lines.append("｜".join(parts))
    return (
        f"请分析下面这位学生本周【{subject}】的 {len(mistakes)} 道错题，完成两件事：\n"
        "1. 归类分析：把错题按错误类型归类（如计算失误、概念不清、审题偏差、"
        "思路缺失、粗心大意等），2-5 类；\n"
        "2. 知识点总结：指出薄弱知识点和共性漏洞，给出下周学习重点。\n\n"
        "错题列表：\n" + "\n".join(lines) + "\n\n"
        "只输出以下 JSON（不要输出其它内容）：\n"
        '{"categories": ['
        '{"name": "错误类型", "question_nos": ["3"], '
        '"pattern": "这类错的共性表现（一句话）", '
        '"advice": "针对性建议（一句话）"}], '
        '"knowledge_summary": "薄弱知识点总结（2-3 句话）", '
        '"focus_next_week": ["下周重点1", "下周重点2"]}\n'
        "注意：question_nos 只能使用上面列表中出现的题号，不要编造。"
    )


def _normalize_analysis(parsed: dict) -> Optional[dict]:
    """形状宽松校验：能用的字段尽量保留，整体不可用返回 None。"""
    if not isinstance(parsed, dict):
        return None
    categories = []
    for c in parsed.get("categories") or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        nos = c.get("question_nos") or []
        categories.append({
            "name": name,
            "question_nos": [str(n) for n in nos if str(n).strip()][:20],
            "pattern": str(c.get("pattern") or "").strip(),
            "advice": str(c.get("advice") or "").strip(),
        })
    knowledge = str(parsed.get("knowledge_summary") or "").strip()
    focus = [str(f).strip() for f in (parsed.get("focus_next_week") or [])
             if str(f).strip()][:10]
    if not categories and not knowledge:
        return None
    return {"categories": categories[:8],
            "knowledge_summary": knowledge,
            "focus_next_week": focus}


async def _analyze_mistakes(settings, subject: str, mistakes: List[dict],
                            provider_factory=None) -> dict:
    """大模型错题归类分析：错误类型归类 + 知识点总结 + 下周重点。

    fail-open：模型失败时返回 {"error": ...}，不影响统计部分落库。
    """
    from . import providers
    from .config import provider_chain
    from .grading import extract_json

    chain = provider_chain(settings)
    if not chain:
        return {"error": "没有可用的模型 provider"}
    cfg = settings.weekly_summary
    factory = provider_factory or providers.make_provider
    user_prompt = _build_analysis_prompt(subject, mistakes)

    errors: List[str] = []
    for name in chain:
        pcfg = settings.llm.providers[name]
        provider = factory(name, pcfg)
        try:
            outcome = await provider.complete_text(
                _ANALYSIS_SYSTEM, user_prompt,
                max_tokens=cfg.analysis_max_tokens)
        except Exception as e:  # noqa: BLE001 - 换备胎继续
            errors.append(f"{name}: {e}")
            log.warning("周总结AI分析切换备胎（调用失败）: provider=%s %s", name, e)
            continue
        if getattr(outcome, "finish_reason", "") == "length":
            errors.append(f"{name}: 输出被截断")
            log.warning("周总结AI分析切换备胎（输出截断）: provider=%s", name)
            continue
        try:
            normalized = _normalize_analysis(extract_json(outcome.text))
        except Exception as e:  # noqa: BLE001 - 输出非法换备胎
            errors.append(f"{name}: 输出校验失败({e})")
            log.warning("周总结AI分析切换备胎（输出非法）: provider=%s %s", name, e)
            continue
        if normalized is None:
            errors.append(f"{name}: 输出为空或无法解析")
            continue
        thinking.log_thinking(
            f"weekly-analysis subject={subject} provider={name} model={outcome.model}",
            outcome.thinking)
        cost = round((outcome.input_tokens / 1_000_000) * pcfg.price_input_per_1m
                     + (outcome.output_tokens / 1_000_000) * pcfg.price_output_per_1m, 4)
        try:
            await db.add_daily_cost(settings.db_path, cost)
        except Exception:
            log.exception("周总结AI分析：记录费用失败")
        log.info("周总结AI分析成功: subject=%s provider=%s tokens=%d/%d cost≈%.4f元",
                 subject, name, outcome.input_tokens, outcome.output_tokens, cost)
        normalized["model"] = f"{name}/{outcome.model}"
        normalized["analyzed_mistakes"] = len(mistakes)
        return normalized
    return {"error": "所有模型都失败了: " + " | ".join(errors)}


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


async def generate_for_user(db_path: str, openid: str, week_start: date,
                          settings=None, provider_factory=None) -> List[str]:
    """为单个账号生成某周的周总结，返回已生成的科目列表。

    settings 为 None（或 weekly_summary.ai_analysis=False）时只做统计，
    不调大模型；模型失败时该科目的 ai_analysis 记 {"error": ...}，
    统计部分照常落库（fail-open）。
    """
    week_key = week_start.isoformat()
    start_ts, end_ts = week_bounds(week_start)
    prev_start, prev_end = week_bounds(week_start - timedelta(days=7))

    grading = await db.weekly_grading_by_subject(db_path, openid, start_ts, end_ts)
    prev_grading = await db.weekly_grading_by_subject(db_path, openid, prev_start, prev_end)
    mistakes = await db.weekly_mistake_stats(db_path, openid, start_ts, end_ts)
    events = await db.weekly_event_counts(db_path, openid, start_ts, end_ts)
    pending = await db.weekly_pending_by_subject(db_path, openid)

    wcfg = getattr(settings, "weekly_summary", None) if settings else None
    do_analysis = bool(wcfg and wcfg.ai_analysis)

    subjects = set(grading) | set(mistakes) | set(events)
    generated: List[str] = []
    week_end = week_start + timedelta(days=6)
    for subject in sorted(subjects):
        g = grading.get(subject, {})
        m = mistakes.get(subject, {})
        e = events.get(subject, {})
        p = pending.get(subject, {})
        pg = prev_grading.get(subject, {})

        ai_analysis = None
        if do_analysis:
            week_mistakes = await db.list_week_mistakes(
                db_path, openid, subject, start_ts, end_ts,
                limit=wcfg.analysis_max_mistakes)
            if week_mistakes:
                ai_analysis = await _analyze_mistakes(
                    settings, subject, week_mistakes, provider_factory)

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
            "ai_analysis": ai_analysis,
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
                           now: Optional[datetime] = None, settings=None,
                           provider_factory=None) -> Dict[str, int]:
    """补生成缺失的周总结：过去 weeks_back 个完整周中有数据但未生成的。

    每周日凌晨由定时循环调用；服务器周日宕机重启后也能在这里补上。
    settings 为 None 时只做统计、不调大模型。返回 {openid: 生成的周数}。
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
                await generate_for_user(db_path, openid, week_start,
                                        settings=settings,
                                        provider_factory=provider_factory)
                done += 1
            except Exception:
                log.exception("周总结生成失败: openid=%s week=%s", openid, week_key)
        if done:
            result[openid] = done
    return result
