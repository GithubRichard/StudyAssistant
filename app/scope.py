"""资料区间计算：把学习规范的任务识别规则落到服务端，而不是只写给模型。

规则（`用户明确指定 > 默认值`）：

| 任务 | 默认读取范围 |
| --- | --- |
| 作业批改 / 学习问答 / 专项训练 / 复测 | 不限定区间，按需读取当天记录与必要历史 |
| 月考 | 当月 1 日至今天 |
| 期中考 / 期末考 | 本学期开学至今天（开学日期未配置时如实给出缺口，不猜测） |
| 周报 | 本周一至今天 |

信息不足时返回缺口说明，由调用方在结果与界面中如实标注；**没有记录不等于没有错误**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

# 训练子类型：专项 / 月考 / 期中 / 期末
TRAINING_KINDS = ("topic", "monthly", "midterm", "final")
TRAINING_KIND_LABELS = {
    "topic": "专项训练",
    "monthly": "月考",
    "midterm": "期中考",
    "final": "期末考",
}
# 需要按「本学期开学至今天」累计记录筛选的训练子类型
_TERM_SCOPED_KINDS = ("midterm", "final")


@dataclass
class ScopeDecision:
    """一次区间计算的结果：实际区间、可读说明与缺口。"""

    start_date: str = ""
    end_date: str = ""
    note: str = ""
    missing: List[str] = field(default_factory=list)

    @property
    def limited(self) -> bool:
        return bool(self.start_date or self.end_date)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "note": self.note,
            "missing": list(self.missing),
        }


def compute_scope(*, task_type: str, training_kind: str = "", scope_start: str = "",
                  scope_end: str = "", term_start_date: str = "",
                  today: Optional[date] = None) -> ScopeDecision:
    """按任务类型与用户指定值计算实际资料区间。"""
    today = today or date.today()
    today_s = today.isoformat()

    if scope_start or scope_end:
        start = (scope_start or "").strip()
        end = (scope_end or "").strip() or today_s
        return ScopeDecision(start, end, f"按用户指定区间 {start or '不限'} ~ {end}", [])

    if task_type == "training" and training_kind == "monthly":
        start = today.replace(day=1).isoformat()
        return ScopeDecision(start, today_s, f"按月考默认规则读取当月 {start} ~ {today_s}", [])

    if task_type == "training" and training_kind in _TERM_SCOPED_KINDS:
        label = TRAINING_KIND_LABELS[training_kind]
        if not term_start_date:
            return ScopeDecision(
                "", today_s,
                f"{label}默认需要本学期开学日期，当前未配置，起始日期未限定",
                ["未配置本学期开学日期（可在设置页填写），无法按整学期累计记录筛选"],
            )
        return ScopeDecision(
            term_start_date, today_s,
            f"按{label}默认规则读取本学期 {term_start_date} ~ {today_s}", [])

    if task_type == "weekly_report":
        start = (today - timedelta(days=today.weekday())).isoformat()
        return ScopeDecision(start, today_s, f"按周报默认规则读取本周 {start} ~ {today_s}", [])

    return ScopeDecision("", "", "未限定资料区间：按需读取当天记录与必要历史", [])


def describe_scope(task: Dict[str, Any], term_start_date: str = "") -> Dict[str, Any]:
    """执行阶段生成区间说明与缺口（供 Hermes 消息与结果视图使用）。"""
    task_type = task.get("task_type", "grading")
    training_kind = task.get("training_kind", "") or ""
    start = (task.get("scope_start") or "").strip()
    end = (task.get("scope_end") or "").strip()
    missing: List[str] = []
    needs_term = task_type == "training" and training_kind in _TERM_SCOPED_KINDS

    if start or end:
        prefix = ""
        if task_type == "training" and training_kind in TRAINING_KIND_LABELS:
            prefix = f"{TRAINING_KIND_LABELS[training_kind]}："
        note = f"{prefix}资料区间 {start or '未指定'} ~ {end or '未指定'}"
        if needs_term and not term_start_date:
            missing.append("未配置本学期开学日期，无法确认是否覆盖整学期记录；请在设置页补充")
    else:
        note = "未限定资料区间（按任务类型读取当天记录与必要历史）"
        if needs_term and not term_start_date:
            missing.append("未配置本学期开学日期，无法按整学期累计记录筛选；请在设置页补充")

    if task_type == "training" and not (task.get("exam_scope") or "").strip():
        missing.append(
            "未提供学校考试范围：本次仅为基于已归档错题的针对性训练，不代表完整考试范围")

    return {"note": note, "missing": missing}
