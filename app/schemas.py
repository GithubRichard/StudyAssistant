"""版本化结果协议与请求模型。

设计要点：
- 新结果（schema_version=2）支持五态判定、二次核查、归档与交付状态。
- 旧结果（schema_version=1）不丢弃、不篡改，按只读方式转换后展示。
- 校验失败不静默放行：宁可让任务标记为校验失败，也不把不可信结果写进学习记录。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, BeforeValidator, Field, model_validator

from .scope import TRAINING_KINDS

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSION = 2      # 上一版协议：仍然只读展示，不回溯改写历史记录
V1_SCHEMA_VERSION = 1

TASK_TYPES = ("grading", "qa", "weekly_report", "training", "retest")
QUESTION_STATUSES = ("correct", "wrong", "unanswered", "uncertain", "unprocessed")
REVIEW_STATES = ("agreed", "disagreed", "unverified", "unprocessed", "not_applicable")
FINAL_DECISIONS = (
    "kept_wrong", "corrected_to_correct", "kept_correct",
    "kept_uncertain", "reclassified_unanswered", "pending",
)
REVIEW_SUMMARY_STATES = ("not_required", "completed", "partial", "failed", "not_run")
DELIVERY_STATUSES = (
    "not_configured", "skipped", "generated", "sent", "committed", "failed",
)
# 订正与复测：待订正 / 已订正待复测 / 复测通过 / 复测未通过 / 不适用
REMEDIATION_STATES = (
    "pending_correction", "corrected_pending_retest",
    "retest_passed", "retest_failed", "not_applicable",
)
REMEDIATION_LABELS = {
    "pending_correction": "待订正",
    "corrected_pending_retest": "已订正待复测",
    "retest_passed": "复测通过",
    "retest_failed": "复测未通过",
    "not_applicable": "不适用",
}
RETEST_RESULTS = ("retest_passed", "retest_failed", "corrected")
# 台账事件口径：复测/订正/异议标记。网页版“我觉得判错了”写入 disputed（记异议事件，并把条目置为 withdrawn 从台账撤回）。
LEDGER_EVENT_RESULTS = ("corrected", "retest_passed", "retest_failed", "disputed")

# 归档子目录与任务类型的对应关系（防止周报写进错题解析这类错位）
ARCHIVE_SUBDIRS_BY_TASK = {
    "grading": ("错题解析",),
    "qa": ("错题解析",),
    "weekly_report": ("周报分析",),
    "training": ("强化训练",),
    "retest": ("强化训练", "错题解析"),
}

# 各任务类型的建议段落标题（内容模板，见工作区 README）；用于提示与自查，不作为硬校验
SECTION_HINTS = {
    "grading": ("当日概览", "来源信息", "逐题解析", "未作答与存疑题",
                "做得好的题", "概念问答", "当日重点"),
    "qa": ("概念问答", "当日重点"),
    "weekly_report": ("本周概览", "错题明细", "做得好的题", "概念问答整理",
                      "错误类型归纳", "下周行动清单"),
    "training": ("训练目标与资料范围", "问题依据", "分层题目", "独立答案解析区",
                 "实际作答与复测结果", "下一步建议"),
    "retest": ("实际作答与复测结果", "下一步建议"),
}

# 「粗心」类笼统错因不接受：技能要求给出具体误用规则
_VAGUE_ERROR_RULES = {"粗心", "不认真", "马虎", "不小心", "看错了"}

_TASK_TYPE_LABELS = {
    "grading": "作业批改",
    "qa": "学习问答",
    "weekly_report": "周报分析",
    "training": "针对性训练",
    "retest": "复测",
}


def drop_nulls(value: Any) -> Any:
    """把结果 JSON 里的 null 视为「未提供」：删掉该键，让字段默认值生效。

    严格模式下 None 过不了 str 校验，且第一处错误就会中断整卷校验
    （真实事故：16 题全对、仅因 remediation.updated_date=null 导致整次任务失败）。
    模型常用 null 表示「本栏无内容」，删键与「省略该字段」完全等价：
    有默认值的字段回落默认值，必填字段仍如实报缺失，不会静默放行。
    """
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(v) for v in value if v is not None]
    return value


class StrictModel(BaseModel):
    """统一开启严格字段校验（多余字段忽略，缺失必填报错）。"""

    model_config = {"extra": "ignore"}


_INT_RE = re.compile(r"-?\d+")


def coerce_int(value: Any) -> Any:
    """把模型写歪的计数容错成整数。

    模型有时把「scope」这类计数栏写成说明文字（真实事故：review_summary.scope =
    "已判错题的二次核查（本次 16 题均未判定为错题）"），严格模式下会因一个字段
    类型不符废掉整卷结果。

    容错口径（刻意保守）：数字、数字字符串、" 3 " 这类纯数字文本直接转；
    **不做**「从说明文字里抠第一个数字」——上面那句里的 16 是题数总计，抠出来会把
    scope 从 0 变成 16，反而绕过「核查完成且存在错题时 scope 不能为 0」的保护。
    因此含说明文字时一律落 0 并记 warning（review_summary 还会把原文写进 note 留痕）：
    overview 的计数随后由服务端按逐题数据重算，落 0 不会污染口径。
    """
    if isinstance(value, bool):          # bool 是 int 子类，单独处理避免 True→1 意外
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else int(round(value))
    if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        return int(value.strip())
    log.warning("结果字段不是整数，已按 0 处理（计数字段不要写说明文字）: %r", value)
    return 0


# 计数类字段的宽松类型：容错模型把计数写成字符串或说明文字
LooseInt = Annotated[int, BeforeValidator(coerce_int)]


def coerce_str(value: Any) -> str:
    """把模型写成数字/布尔的文本字段容错成字符串。

    真实事故：提取阶段的 `page`（图片序号）本就该是文本，模型写 `"page": 1`，
    严格模式下 12 个字段报错、整个提取阶段失败并切备胎（备胎同样写法就整单失败）。

    容错口径：字符串原样保留（不 strip，避免改变内容语义）；None 落空串；
    整数写成不带小数的文本；整数浮点（1.0）同样写 "1"，避免出现 "1.0" 这种题号；
    其余类型用 str() 兜底，不抛异常——类型对的字段不受影响。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if float(value).is_integer() else repr(value)
    return str(value)


# 文本类字段的宽松类型：容错模型把题号/页码等文本写成数字
LooseStr = Annotated[str, BeforeValidator(coerce_str)]


def coerce_str_list(value: Any) -> List[str]:
    """把模型写成单个字符串的列表字段容错成字符串列表。

    真实模型经常把 `steps`/`explanation` 写成一句字符串而不是数组；
    这里包装成单元素列表，语义由后续阶段校验，不因形状差异废掉整阶段。
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [coerce_str(v) for v in value]
    return [coerce_str(value)]


# 文本列表的宽松类型：容错模型把数组写成单字符串或混合类型
LooseStrList = Annotated[List[str], BeforeValidator(coerce_str_list)]


class ScopeInfo(StrictModel):
    start_date: str = ""
    end_date: str = ""
    sources: List[str] = Field(default_factory=list)


class Overview(StrictModel):
    checked_questions: LooseInt = 0
    correct: LooseInt = 0
    wrong: LooseInt = 0
    unanswered: LooseInt = 0
    uncertain: LooseInt = 0
    unprocessed: LooseInt = 0
    summary: str = ""
    # 订正与复测口径：各状态计数（由服务端按逐题数据重算，避免与题目不一致）
    remediation: Dict[str, LooseInt] = Field(default_factory=dict)
    # 错误率只在分母（已检查题数）可确认时才计算，否则留空并说明
    error_rate: float = 0.0
    error_rate_basis: str = ""


class Remediation(StrictModel):
    """订正与复测状态：只描述当前证据，不美化历史。"""

    state: str = "not_applicable"
    updated_date: str = ""          # 实际发生日期，取任务发生时日期
    linked_training: str = ""       # 关联训练/复测记录（相对工作区路径）
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Remediation":
        if self.state not in REMEDIATION_STATES:
            preview = repr(self.state[:80])
            if len(self.state) > 80 or len(preview) > 120:
                preview = preview[:117] + "..."
            raise ValueError(f"remediation.state 非法: {preview}")
        self.updated_date = (self.updated_date or "").strip()
        if self.updated_date and not _DATE_RE.match(self.updated_date):
            raise ValueError("remediation.updated_date 必须形如 YYYY-MM-DD")
        if self.state in ("corrected_pending_retest", "retest_passed", "retest_failed") \
                and not self.updated_date:
            raise ValueError(f"remediation.state={self.state} 必须给出实际发生日期 updated_date")
        return self


class RetestEvent(StrictModel):
    """一次真实发生的复测/订正事件；只追加，不改写历史判定。"""

    question_uid: str = ""
    subject: str = ""
    source: str = ""
    page: str = ""
    no: str = ""
    occurred_date: str = ""
    result: str = "retest_passed"
    student_answer: str = ""
    source_task_id: str = ""
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "RetestEvent":
        if self.result not in RETEST_RESULTS:
            raise ValueError(f"retests.result 非法: {self.result}")
        self.occurred_date = (self.occurred_date or "").strip()
        if self.occurred_date and not _DATE_RE.match(self.occurred_date):
            raise ValueError("retests.occurred_date 必须形如 YYYY-MM-DD")
        if not self.occurred_date:
            raise ValueError("复测事件必须给出实际发生日期 occurred_date")
        return self


class QuestionReview(StrictModel):
    state: str = "not_applicable"
    note: str = ""
    basis: str = ""
    # 转写二次确认结论：True=重读与转写一致，False=不符或无法重读，None=本次未做转写核对
    transcript_ok: Optional[bool] = None
    reread_answer: str = ""  # 对照原图重读到的学生作答（与转写一致时照抄转写）

    @model_validator(mode="after")
    def _check_state(self) -> "QuestionReview":
        if self.state not in REVIEW_STATES:
            raise ValueError(f"review.state 非法: {self.state}")
        if self.state == "disagreed" and not self.basis.strip():
            raise ValueError("review.state=disagreed 必须提供 basis（可核验依据）")
        return self


# ---------- 第二模型复查输出协议（独立于 StudyResult）----------
# 复查方无权改学业判定：协议里只有逐题核查结论，不含 status / 答案 / 订正 / 复测字段。
REVIEW_ITEM_STATES = ("agreed", "disagreed", "unverified")


class ReviewItem(StrictModel):
    """复查方对单道送审题的结论：无异议 / 有异议（须给依据）/ 无法核查。

    附带转写二次确认结论：transcript_ok=false（重读与转写实质不符）时，
    该题必须标 disagreed 并在 basis 写清转写差异。
    """

    id: str
    state: str = "unverified"
    note: str = ""
    basis: str = ""
    transcript_ok: Optional[bool] = None  # None=本次未做转写核对（纯文字复查）
    reread_answer: str = ""

    @model_validator(mode="after")
    def _check(self) -> "ReviewItem":
        if not self.id.strip():
            raise ValueError("复查项 id 不能为空")
        if self.state not in REVIEW_ITEM_STATES:
            raise ValueError(f"复查项 state 非法: {self.state}")
        if self.state == "disagreed" and not self.basis.strip():
            raise ValueError(f"复查项 {self.id}: state=disagreed 必须提供 basis（可核验依据）")
        return self


class ReviewResponse(StrictModel):
    """复查方整体输出。reviews 必须显式提供（空列表视为非法，由服务端对账）。"""

    reviews: List[ReviewItem]

    @model_validator(mode="after")
    def _check(self) -> "ReviewResponse":
        ids = [r.id.strip() for r in self.reviews]
        if len(ids) != len(set(ids)):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"复查项 id 重复: {', '.join(dup)}")
        if not self.reviews:
            raise ValueError("复查响应 reviews 不能为空列表")
        return self


class QuestionResult(StrictModel):
    id: str
    uid: str = ""               # 稳定去重键（来源+日期+页码+题号），由服务端回填
    no: str = ""
    source: str = ""
    page: str = ""
    stem: str = ""
    student_answer: str = ""
    status: str
    correct_answer: str = ""
    steps: List[str] = Field(default_factory=list)
    error_rule: str = ""
    knowledge_point: str = ""
    evidence: str = ""
    review: QuestionReview = Field(default_factory=QuestionReview)
    final_decision: str = "pending"
    final_decision_basis: str = ""
    remediation: Remediation = Field(default_factory=Remediation)

    @model_validator(mode="before")
    @classmethod
    def _normalize_blank_remediation_state(cls, data: Any) -> Any:
        """只容错明确非错题的空状态；错题缺少状态仍交给严格校验。"""
        if not isinstance(data, dict) or data.get("status") not in (
            "correct", "unanswered", "uncertain", "unprocessed"
        ):
            return data
        remediation = data.get("remediation")
        if not isinstance(remediation, dict):
            return data
        state = remediation.get("state")
        if isinstance(state, str) and not state.strip():
            return {**data, "remediation": {**remediation, "state": "not_applicable"}}
        return data

    @model_validator(mode="after")
    def _check(self) -> "QuestionResult":
        if not self.id.strip():
            raise ValueError("题目 id 不能为空")
        if self.status not in QUESTION_STATUSES:
            raise ValueError(f"status 非法: {self.status}")
        if self.final_decision not in FINAL_DECISIONS:
            raise ValueError(f"final_decision 非法: {self.final_decision}")

        if self.status == "wrong":
            if not (self.correct_answer.strip() or self.steps):
                raise ValueError(f"题 {self.id}: 判错必须给出 correct_answer 或 steps")
            rule = self.error_rule.strip()
            if not rule:
                raise ValueError(f"题 {self.id}: 判错必须给出具体 error_rule")
            if rule in _VAGUE_ERROR_RULES:
                raise ValueError(f"题 {self.id}: error_rule 不能笼统写作「{rule}」")

        if self.status in ("unanswered", "uncertain") and self.final_decision == "kept_wrong":
            raise ValueError(f"题 {self.id}: {self.status} 不能标记为 kept_wrong")

        if self.review.state == "disagreed" and not self.final_decision_basis.strip():
            raise ValueError(f"题 {self.id}: 核查有异议时必须写明 final_decision_basis")
        return self


class Section(StrictModel):
    title: str
    body: str = ""


class ArchiveSuggestion(StrictModel):
    suggested_path: str = ""
    action: str = "append"
    content_markdown: str = ""

    @model_validator(mode="after")
    def _check_action(self) -> "ArchiveSuggestion":
        if self.action not in ("append", "create", "none"):
            raise ValueError(f"archive.action 非法: {self.action}")
        return self


class DeliveryItem(StrictModel):
    status: str = "not_configured"
    note: str = ""
    # 以下字段由服务端按真实同步结果填写（模型给的会被服务端覆盖）
    committed: bool = False
    pushed: bool = False
    commit: str = ""
    conflict_record: str = ""

    @model_validator(mode="after")
    def _check_status(self) -> "DeliveryItem":
        if self.status not in DELIVERY_STATUSES:
            raise ValueError(f"delivery.status 非法: {self.status}")
        return self


class ArchiveDeliveryItem(DeliveryItem):
    """学习记录归档状态：由服务端填写，写明相对路径与关联的题目去重键。"""

    path: str = ""
    questions: List[str] = Field(default_factory=list)


class DeliveryReport(StrictModel):
    pdf: DeliveryItem = Field(default_factory=DeliveryItem)
    email: DeliveryItem = Field(default_factory=DeliveryItem)
    git: DeliveryItem = Field(default_factory=DeliveryItem)
    archive: ArchiveDeliveryItem = Field(default_factory=ArchiveDeliveryItem)


class ReviewSummary(StrictModel):
    state: str = "not_run"
    scope: LooseInt = 0
    disagreed: LooseInt = 0
    unverified: LooseInt = 0
    note: str = ""
    # ---------- 服务端二次复查扩展字段（全部带默认值，旧结果不写这些键也能读）----------
    target_count: LooseInt = 0     # 当前全部应复查题数（wrong + uncertain）
    unprocessed: LooseInt = 0      # 未送审或响应漏掉、尚无复查结果的题数
    model_requested: str = ""      # 请求路由（别名或底层模型 ID + provider），仅记录意图
    model_reported: str = ""       # 网关报告的模型；缺失时保持空串，不用请求值冒充
    model_identity: str = ""       # confirmed / mismatch / unknown（身份核验结论）
    coverage: str = ""             # 材料范围：transcript_only（纯转写核查，不读图）
                                   #          reread（先对照原图做转写二次确认，再核查）

    @model_validator(mode="before")
    @classmethod
    def _trace_coerced_counts(cls, data: Any) -> Any:
        """计数栏被写成说明文字时，把原文留在 note 里，不悄悄丢掉信息。"""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        traces: List[str] = []
        for key in ("scope", "disagreed", "unverified"):
            raw = data.get(key)
            if isinstance(raw, str) and not _INT_RE.fullmatch(raw.strip()):
                traces.append(f"{key} 原文为「{raw.strip()[:80]}」，已按 {coerce_int(raw)} 处理")
                data[key] = coerce_int(raw)
        if traces:
            note = (data.get("note") or "").strip()
            data["note"] = "；".join([note] + traces) if note else "；".join(traces)
        return data

    @model_validator(mode="after")
    def _check_state(self) -> "ReviewSummary":
        if self.state not in REVIEW_SUMMARY_STATES:
            raise ValueError(f"review_summary.state 非法: {self.state}")
        return self


class StudyResultV2(StrictModel):
    """上一版协议（schema_version=2）：历史结果只读展示，不再由新任务产出。"""

    schema_version: int = LEGACY_SCHEMA_VERSION
    task_type: str = "grading"
    subject: str = ""
    grade_level: str = ""
    scope: ScopeInfo = Field(default_factory=ScopeInfo)
    overview: Overview = Field(default_factory=Overview)
    questions: List[QuestionResult] = Field(default_factory=list)
    sections: List[Section] = Field(default_factory=list)
    missing_info: List[str] = Field(default_factory=list)
    parent_tips: List[str] = Field(default_factory=list)
    review_summary: ReviewSummary = Field(default_factory=ReviewSummary)
    archive: ArchiveSuggestion = Field(default_factory=ArchiveSuggestion)
    delivery: DeliveryReport = Field(default_factory=DeliveryReport)

    @model_validator(mode="after")
    def _check_all(self) -> "StudyResultV2":
        if self.task_type not in TASK_TYPES:
            raise ValueError(f"task_type 非法: {self.task_type}")

        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"题目 id 重复: {', '.join(dup)}")

        counts = count_statuses(self.questions)
        for field, actual in counts.items():
            given = getattr(self.overview, field)
            if given and given != actual:
                raise ValueError(
                    f"overview.{field}={given} 与逐题统计 {actual} 不一致"
                )
        self.overview = Overview(
            checked_questions=max(self.overview.checked_questions, len(self.questions)),
            summary=self.overview.summary,
            **counts,
        )
        if self.review_summary.state == "completed" and counts["wrong"] and not self.review_summary.scope:
            raise ValueError("存在判错题且 review_summary.state=completed 时 scope 不能为 0")
        return self


class StudyResult(StrictModel):
    """当前协议结果（schema_version=3）。

    相比 v2 增加：题目稳定去重键 uid、订正与复测状态、复测事件、考试范围与训练子类型，
    并在服务端重算统计口径（含错误率分母说明）与订正状态计数。
    """

    schema_version: int = SCHEMA_VERSION
    task_type: str = "grading"
    subject: str = ""
    grade_level: str = ""
    exam_scope: str = ""
    training_kind: str = ""
    scope: ScopeInfo = Field(default_factory=ScopeInfo)
    overview: Overview = Field(default_factory=Overview)
    questions: List[QuestionResult] = Field(default_factory=list)
    retests: List[RetestEvent] = Field(default_factory=list)
    sections: List[Section] = Field(default_factory=list)
    missing_info: List[str] = Field(default_factory=list)
    parent_tips: List[str] = Field(default_factory=list)
    review_summary: ReviewSummary = Field(default_factory=ReviewSummary)
    archive: ArchiveSuggestion = Field(default_factory=ArchiveSuggestion)
    delivery: DeliveryReport = Field(default_factory=DeliveryReport)

    @model_validator(mode="after")
    def _check_all(self) -> "StudyResult":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version 必须为 {SCHEMA_VERSION}（当前: {self.schema_version}）")
        if self.task_type not in TASK_TYPES:
            raise ValueError(f"task_type 非法: {self.task_type}")
        if self.task_type != "training" and self.training_kind:
            raise ValueError("只有 training 任务可以带 training_kind")

        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"题目 id 重复: {', '.join(dup)}")
        uids = [q.uid for q in self.questions if q.uid.strip()]
        if len(uids) != len(set(uids)):
            dup = sorted({u for u in uids if uids.count(u) > 1})
            raise ValueError(f"题目 uid 重复: {', '.join(dup)}")

        counts = count_statuses(self.questions)
        # overview 允许由模型留空（全 0），但填了就必须与逐题数据一致
        for field, actual in counts.items():
            given = getattr(self.overview, field)
            if given and given != actual:
                raise ValueError(
                    f"overview.{field}={given} 与逐题统计 {actual} 不一致"
                )

        for q in self.questions:
            state = q.remediation.state
            if q.status == "wrong":
                if state == "not_applicable":
                    raise ValueError(f"题 {q.id}: 判错题必须给出订正/复测状态 remediation.state")
            elif state != "not_applicable":
                raise ValueError(
                    f"题 {q.id}: {q.status} 不是错题，remediation.state 必须为 not_applicable")

        self.overview = build_overview(self.overview, self.questions)

        if self.review_summary.state == "completed" and counts["wrong"] and not self.review_summary.scope:
            raise ValueError("存在判错题且 review_summary.state=completed 时 scope 不能为 0")

        subdir = archive_subdir(self.archive.suggested_path)
        allowed = ARCHIVE_SUBDIRS_BY_TASK.get(self.task_type, ())
        if subdir and allowed and subdir not in allowed:
            raise ValueError(
                f"archive.suggested_path 的子目录应为 {' / '.join(allowed)}，当前为「{subdir}」")
        return self

def count_statuses(questions: List[QuestionResult]) -> Dict[str, int]:
    counts = {s: 0 for s in QUESTION_STATUSES}
    for q in questions:
        counts[q.status] += 1
    return {k: v for k, v in counts.items()}


def count_remediations(questions: List[QuestionResult]) -> Dict[str, int]:
    counts = {s: 0 for s in REMEDIATION_STATES}
    for q in questions:
        counts[q.remediation.state] += 1
    return counts


def build_overview(overview: Overview, questions: List[QuestionResult]) -> Overview:
    """重算统计口径：五态、订正状态计数，以及仅在分母可确认时才给的错误率。"""
    counts = count_statuses(questions)
    checked = max(int(overview.checked_questions or 0), len(questions))
    basis = ""
    rate = 0.0
    if checked > 0:
        rate = round(counts["wrong"] / checked, 4)
        basis = (f"错误率=确认错题 {counts['wrong']} / 已检查题 {checked}"
                 f"（分母为已检查题数；未作答与存疑不计入错误）")
    else:
        basis = "未检查到题目，分母不可确认，不计算错误率"
    return Overview(
        checked_questions=checked,
        summary=overview.summary,
        remediation=count_remediations(questions),
        error_rate=rate,
        error_rate_basis=basis,
        **counts,
    )


def question_uid(subject: str, source: str, page: str, no: str, date_str: str = "") -> str:
    """稳定去重键：学科+来源+日期+页码+题号。

    同一错题出现在日解析与周报中、或重复识图时得到同一 uid，从而不重复计入出错事件；
    再次实际作答才产生新的复测事件。
    """
    raw = "|".join([
        (subject or "").strip(),
        (source or "").strip(),
        (date_str or "").strip(),
        (page or "").strip(),
        (no or "").strip(),
    ])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"q-{digest}"


def archive_subdir(relative_path: str) -> str:
    """取归档建议路径的第二段（子目录）；不合法时返回空串。"""
    if not relative_path:
        return ""
    parts = Path(relative_path.strip().lstrip("./")).parts
    return parts[1] if len(parts) >= 3 else ""


# 页码归一化：只取第一个「字母+数字」片段当起始页（P12-13 / P12~13 / 第 12 页 → P12 / 12）
_PAGE_TOKEN_RE = re.compile(r"[A-Za-z]*\d+")


def page_start_token(page: str) -> str:
    """把页码归一化到起始页，用于比较同一题号是否落在不同页上。

    跨页题的页码可能被写成 `P12-13`、`P12~13`、`第 12-13 页`，统一取第一个
    「字母+数字」片段（→ `P12` / `12`）；无法识别时返回大写去空白的原文。
    """
    text = (page or "").strip().upper().replace(" ", "")
    if not text:
        return ""
    match = _PAGE_TOKEN_RE.search(text)
    return match.group(0).upper() if match else text


def cross_page_divergence_notes(result: Dict[str, Any]) -> List[str]:
    """只读检测疑似跨页分叉：同来源同题号但页码不一致。

    跨页题被拆成两条（如第 5 题分别登记成 P12 与 P13）、或其中一条漏填页码时，
    服务端无法判断是否为同一题，只如实提示人工核对：不改判、不合并条目、不改 uid。
    仅返回提示文本，由调用方并入 missing_info。
    """
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for question in result.get("questions") or []:
        source = (question.get("source") or "").strip()
        no = (question.get("no") or "").strip()
        if not source or not no:
            continue  # 来源或题号缺失时无从判断，不猜
        group = groups.setdefault((source, no), {"pages": [], "has_blank": False})
        page_raw = (question.get("page") or "").strip()
        if not page_raw:
            group["has_blank"] = True
        elif page_raw not in group["pages"]:
            group["pages"].append(page_raw)

    samples: List[str] = []
    for (source, no), group in groups.items():
        distinct = {page_start_token(p) for p in group["pages"]}
        if len(distinct) < 2 and not (distinct and group["has_blank"]):
            continue
        shown = " / ".join(group["pages"]) or "（页码空缺）"
        if group["has_blank"]:
            shown += " / （页码空缺）"
        samples.append(f"来源「{source}」第 {no} 题：{shown}")
    if not samples:
        return []

    sample = "；".join(samples[:3]) + ("…" if len(samples) > 3 else "")
    return [
        f"检测到 {len(samples)} 处疑似跨页分叉（同来源同题号但页码不一致：{sample}），"
        f"请人工核对是否为同一道题的续页；若属同一题，请以起始页登记为一条"
    ]


def fill_question_uids(result: Dict[str, Any], date_str: str) -> Dict[str, Any]:
    """回填题目与复测事件的稳定去重键。

    去重键 = 学科+来源+日期+页码+题号；同一题重复出现时不重复计入出错事件，
    重复条目会带 `-2` 后缀并在 missing_info 中如实说明，便于人工核对。
    另外做一次只读的疑似跨页分叉检测（同来源同题号但页码不一致），只提示不合并。
    """
    subject = result.get("subject", "") or ""
    seen: Dict[str, int] = {}
    duplicated = 0

    def _assign(holder: Dict[str, Any]) -> None:
        nonlocal duplicated
        uid = (holder.get("uid") or holder.get("question_uid") or "").strip()
        if not uid:
            uid = question_uid(subject, holder.get("source", ""), holder.get("page", ""),
                               holder.get("no", ""), date_str)
        if uid in seen:
            duplicated += 1
            seen[uid] += 1
            uid = f"{uid}-{seen[uid]}"
        else:
            seen[uid] = 1
        if "uid" in holder or holder.get("status") is not None:
            holder["uid"] = uid
        else:
            holder["question_uid"] = uid

    for question in result.get("questions") or []:
        _assign(question)
    for retest in result.get("retests") or []:
        _assign(retest)

    notes: List[str] = []
    if duplicated:
        notes.append(
            f"检测到 {duplicated} 条重复题目条目（同来源同页码同题号），"
            f"已按去重键区分，请核对是否重复识图")
    notes.extend(cross_page_divergence_notes(result))
    if notes:
        missing = result.setdefault("missing_info", [])
        for note in notes:
            if note not in missing:
                missing.append(note)
    return result


# --------------------------- 旧结果（v1）兼容 ---------------------------


class LegacyQuestion(StrictModel):
    no: str = ""
    student_answer: str = ""
    is_correct: bool = False
    correct_answer: str = ""
    explanation: List[str] = Field(default_factory=list)
    knowledge_point: str = ""


class LegacyResult(StrictModel):
    total_questions: int = 0
    correct_count: int = 0
    questions: List[LegacyQuestion] = Field(default_factory=list)
    summary: str = ""


def _legacy_question_to_display(q: LegacyQuestion) -> Dict[str, Any]:
    return {
        "id": f"legacy-{q.no or 'q'}",
        "uid": "",
        "no": q.no,
        "source": "（旧版本结果，未记录来源）",
        "page": "",
        "stem": "",
        "student_answer": q.student_answer,
        "status": "correct" if q.is_correct else "wrong",
        "correct_answer": q.correct_answer,
        "steps": list(q.explanation),
        "error_rule": "" if q.is_correct else "旧版本未记录错因规则",
        "knowledge_point": q.knowledge_point,
        "evidence": "",
        "review": {"state": "not_applicable", "note": "旧版本结果，未执行二次核查", "basis": ""},
        "final_decision": "kept_correct" if q.is_correct else "kept_wrong",
        "final_decision_basis": "",
        "remediation": {"state": "not_applicable", "updated_date": "",
                        "linked_training": "", "note": "旧版本结果未记录订正与复测状态"},
    }


def _present(data: Dict[str, Any], *, legacy_schema: int) -> Dict[str, Any]:
    """统一展示结构：前端只按一种字段布局渲染，并如实标注协议版本。"""
    data["legacy"] = legacy_schema == V1_SCHEMA_VERSION
    data["legacy_schema"] = legacy_schema
    data["task_type_label"] = _TASK_TYPE_LABELS.get(
        data.get("task_type", ""), data.get("task_type", ""))
    return data


def _upgrade_v2_display(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把 v2 结果转成统一展示结构，并明确标注「未记录订正与复测状态」。"""
    data = StudyResultV2.model_validate(raw).model_dump()
    data["schema_version"] = LEGACY_SCHEMA_VERSION
    data["schema_note"] = "v2 结果：未记录订正与复测状态"
    data["exam_scope"] = ""
    data["training_kind"] = ""
    data["retests"] = []
    questions = data.get("questions") or []
    for q in questions:
        q["uid"] = ""
        q["remediation"] = {"state": "not_applicable", "updated_date": "",
                            "linked_training": "", "note": "v2 结果未记录订正与复测状态"}
    overview = Overview(**(data.get("overview") or {}))
    data["overview"] = build_overview(
        overview, [QuestionResult(**q) for q in questions]).model_dump()
    return data


def _normalize_versioned(raw: Dict[str, Any], version: Any) -> Optional[Dict[str, Any]]:
    """按声明的 schema_version 归一化；未声明版本号时先试 v3 再试 v2。"""
    if version in (None, SCHEMA_VERSION):
        try:
            return _present(StudyResult.model_validate(raw).model_dump(),
                            legacy_schema=SCHEMA_VERSION)
        except Exception:  # noqa: BLE001
            if version == SCHEMA_VERSION:
                return None
    if version in (None, LEGACY_SCHEMA_VERSION):
        try:
            return _present(_upgrade_v2_display(raw), legacy_schema=LEGACY_SCHEMA_VERSION)
        except Exception:  # noqa: BLE001
            return None
    return None


def normalize_result(raw: Any) -> Optional[Dict[str, Any]]:
    """把数据库中的结果 JSON 转换成统一展示结构。

    支持 v3（当前）、v2（只读，补齐订正/复测占位）、v1（旧批改结果）。
    返回 None 表示格式无法识别；调用方应如实说明「结果格式无法识别」，不要猜测。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict) or not raw:
        return None
    # 历史结果里也可能存着 null（旧版本未拦截），同样按「未提供」读取
    raw = drop_nulls(raw)

    version = raw.get("schema_version")
    looks_versioned = version in (SCHEMA_VERSION, LEGACY_SCHEMA_VERSION) or (
        version is None and "questions" in raw and "task_type" in raw)
    if looks_versioned:
        return _normalize_versioned(raw, version)

    try:
        legacy = LegacyResult.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None

    questions = [_legacy_question_to_display(q) for q in legacy.questions]
    counts = {s: 0 for s in QUESTION_STATUSES}
    for q in questions:
        counts[q["status"]] += 1
    return _present({
        "schema_version": V1_SCHEMA_VERSION,
        "legacy_schema": V1_SCHEMA_VERSION,
        "schema_note": "v1 旧批改结果：未记录来源、错因规则与二次核查",
        "task_type": "grading",
        "subject": "",
        "grade_level": "",
        "exam_scope": "",
        "training_kind": "",
        "scope": {"start_date": "", "end_date": "", "sources": []},
        "overview": {
            "checked_questions": max(legacy.total_questions, len(questions)),
            "summary": legacy.summary,
            **counts,
            "remediation": {s: 0 for s in REMEDIATION_STATES},
            "error_rate": 0.0,
            "error_rate_basis": "旧版本结果未记录题目明细，分母不可确认，不计算错误率",
        },
        "questions": questions,
        "retests": [],
        "sections": [],
        "missing_info": [],
        "parent_tips": [],
        "review_summary": {
            "state": "not_run", "scope": 0, "disagreed": 0, "unverified": 0,
            "note": "旧版本结果：未记录二次核查",
        },
        "archive": {"suggested_path": "", "action": "none", "content_markdown": ""},
        "delivery": {
            "pdf": {"status": "not_configured", "note": "旧版本结果无交付记录"},
            "email": {"status": "not_configured", "note": "旧版本结果无交付记录"},
            "git": {"status": "not_configured", "note": "旧版本结果无交付记录"},
        },
    }, legacy_schema=V1_SCHEMA_VERSION)


# --------------------------- 请求模型 ---------------------------


class StudyTaskCreate(StrictModel):
    task_type: str = "grading"
    subject: str = "数学"
    grade_level: str = ""
    text: str = ""
    asset_ids: List[str] = Field(default_factory=list)
    scope_start: str = ""
    scope_end: str = ""
    exam_scope: str = ""        # 学校考试范围；未提供时训练只针对已归档错题
    training_kind: str = ""     # 仅 training 有效：topic / monthly / midterm / final

    @model_validator(mode="after")
    def _check(self) -> "StudyTaskCreate":
        if self.task_type not in TASK_TYPES:
            raise ValueError(f"task_type 非法: {self.task_type}")
        if not self.text.strip() and not self.asset_ids:
            raise ValueError("必须提供文字说明或至少一张图片")
        if len(self.asset_ids) > 20:
            raise ValueError("单次任务图片不能超过 20 张")

        self.exam_scope = (self.exam_scope or "").strip()
        if len(self.exam_scope) > 500:
            raise ValueError("考试范围描述不能超过 500 字")

        self.training_kind = (self.training_kind or "").strip()
        if self.task_type == "training":
            if not self.training_kind:
                self.training_kind = "topic"
            if self.training_kind not in TRAINING_KINDS:
                raise ValueError(f"training_kind 非法: {self.training_kind}")
        else:
            # 非训练任务不接受训练子类型，避免下游误判
            self.training_kind = ""

        for label, value in (("scope_start", self.scope_start), ("scope_end", self.scope_end)):
            value = (value or "").strip()
            if value and not _DATE_RE.match(value):
                raise ValueError(f"{label} 必须形如 YYYY-MM-DD")
        self.scope_start = (self.scope_start or "").strip()
        self.scope_end = (self.scope_end or "").strip()
        return self


class FollowupCreate(StrictModel):
    text: str = ""
    asset_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "FollowupCreate":
        if not self.text.strip() and not self.asset_ids:
            raise ValueError("补充材料必须包含文字或图片")
        return self


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ManualLedgerCreate(StrictModel):
    """做题页人工登记：自己判错的题直接记入复习台账。"""

    subject: str = ""
    question_no: str = ""
    stem: str = ""
    student_answer: str = ""
    correct_answer: str = ""
    knowledge_point: str = ""
    note: str = ""
    question_uid: str = ""
    source_task_id: str = ""

    @model_validator(mode="after")
    def _check(self) -> "ManualLedgerCreate":
        if not self.stem.strip():
            raise ValueError("题干不能为空")
        for key in ("subject", "question_no", "stem", "student_answer",
                    "correct_answer", "knowledge_point", "note", "question_uid"):
            setattr(self, key, (getattr(self, key) or "").strip())
        return self


class LedgerEventCreate(StrictModel):
    """人工登记一次订正/复测结果（真实作答后才登记）。disputed 为网页版“我觉得判错了”：记异议事件，条目置为 withdrawn 从台账撤回。"""

    result: str
    occurred_date: str = ""
    student_answer: str = ""
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "LedgerEventCreate":
        if self.result not in LEDGER_EVENT_RESULTS:
            raise ValueError(f"result 非法: {self.result}（可选 {' / '.join(LEDGER_EVENT_RESULTS)}）")
        self.occurred_date = (self.occurred_date or "").strip()
        if self.occurred_date and not _DATE_RE.match(self.occurred_date):
            raise ValueError("occurred_date 必须形如 YYYY-MM-DD")
        self.student_answer = (self.student_answer or "").strip()
        self.note = (self.note or "").strip()
        return self


class FamilySettingsUpdate(StrictModel):
    """家庭设置：学期起始日期与学科清单（用于区间计算与任务识别）。"""

    subjects: List[str] = Field(default_factory=list)
    term_start_date: str = ""

    @model_validator(mode="after")
    def _check(self) -> "FamilySettingsUpdate":
        if self.term_start_date and not _DATE_RE.match(self.term_start_date.strip()):
            raise ValueError("term_start_date 必须形如 YYYY-MM-DD")
        self.term_start_date = self.term_start_date.strip()
        cleaned: List[str] = []
        for raw in self.subjects:
            name = raw.strip()
            if not name:
                continue
            if len(name) > 20:
                raise ValueError(f"学科名称过长: {name}")
            if name not in cleaned:
                cleaned.append(name)
        self.subjects = cleaned
        return self
        return self


def request_hash(payload: Dict[str, Any]) -> str:
    """幂等请求指纹：同一幂等键 + 同一内容 → 返回原任务；内容不同 → 冲突。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def grading_result_to_v3(grading_result: Any, subject: str = "",
                         grade_level: str = "", provider: str = "") -> Dict[str, Any]:
    """把 legacy 单轮批改结果转换为当前协议结构。

    关键：明确标注「未执行技能流程与二次核查」，不把旧模式伪装成技能执行成功；
    旧模式不产出订正/复测记录，一律标为 not_applicable 并在结果中说明。
    """
    questions: List[Dict[str, Any]] = []
    for index, q in enumerate(getattr(grading_result, "questions", []) or [], start=1):
        qno = getattr(q, "no", "") or str(index)
        steps = list(getattr(q, "explanation", []) or [])
        correct_answer = getattr(q, "correct_answer", "") or ""
        is_correct = bool(getattr(q, "is_correct", False))
        if is_correct:
            status, final, state = "correct", "kept_correct", "not_applicable"
            error_rule = ""
        elif correct_answer or steps:
            status, final, state = "wrong", "kept_wrong", "pending_correction"
            error_rule = "旧模式未给出具体错因规则，需人工复核"
        else:
            status, final, state = "uncertain", "kept_uncertain", "not_applicable"
            error_rule = ""
        questions.append({
            "id": f"legacy-{qno}-{index}",
            "no": qno,
            "source": "（旧模式结果，未记录来源）",
            "student_answer": getattr(q, "student_answer", "") or "",
            "status": status,
            "correct_answer": correct_answer,
            "steps": steps,
            "error_rule": error_rule,
            "knowledge_point": getattr(q, "knowledge_point", "") or "",
            "review": {"state": "not_applicable", "note": "旧模式未执行二次核查", "basis": ""},
            "final_decision": final,
            "remediation": {"state": state, "updated_date": "", "linked_training": "",
                            "note": "旧模式未记录订正与复测状态"},
        })

    summary = getattr(grading_result, "summary", "") or ""
    raw = {
        "schema_version": SCHEMA_VERSION,
        "task_type": "grading",
        "subject": subject,
        "grade_level": grade_level,
        "overview": {
            "checked_questions": int(getattr(grading_result, "total_questions", 0) or 0),
            "summary": summary,
        },
        "questions": questions,
        "sections": ([{"title": "批改小结", "body": summary}] if summary else []),
        "review_summary": {
            "state": "not_run", "scope": 0, "disagreed": 0, "unverified": 0,
            "note": f"旧模式（{provider or '直连模型'}）不执行技能流程与二次核查",
        },
        "archive": {"action": "none"},
        "delivery": {
            "pdf": {"status": "not_configured", "note": "旧模式不生成 PDF"},
            "email": {"status": "not_configured", "note": "旧模式不发送邮件"},
            "git": {"status": "not_configured", "note": "旧模式不执行学习记录同步"},
        },
    }
    return StudyResult.model_validate(raw).model_dump()
