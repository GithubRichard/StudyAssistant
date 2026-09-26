"""版本化结果协议与请求模型。

设计要点：
- 新结果（schema_version=2）支持五态判定、二次核查、归档与交付状态。
- 旧结果（schema_version=1）不丢弃、不篡改，按只读方式转换后展示。
- 校验失败不静默放行：宁可让任务标记为校验失败，也不把不可信结果写进学习记录。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1

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

# 「粗心」类笼统错因不接受：技能要求给出具体误用规则
_VAGUE_ERROR_RULES = {"粗心", "不认真", "马虎", "不小心", "看错了"}

_TASK_TYPE_LABELS = {
    "grading": "作业批改",
    "qa": "学习问答",
    "weekly_report": "周报分析",
    "training": "针对性训练",
    "retest": "复测",
}


class StrictModel(BaseModel):
    """统一开启严格字段校验（多余字段忽略，缺失必填报错）。"""

    model_config = {"extra": "ignore"}


class ScopeInfo(StrictModel):
    start_date: str = ""
    end_date: str = ""
    sources: List[str] = Field(default_factory=list)


class Overview(StrictModel):
    checked_questions: int = 0
    correct: int = 0
    wrong: int = 0
    unanswered: int = 0
    uncertain: int = 0
    unprocessed: int = 0
    summary: str = ""


class QuestionReview(StrictModel):
    state: str = "not_applicable"
    note: str = ""
    basis: str = ""

    @model_validator(mode="after")
    def _check_state(self) -> "QuestionReview":
        if self.state not in REVIEW_STATES:
            raise ValueError(f"review.state 非法: {self.state}")
        if self.state == "disagreed" and not self.basis.strip():
            raise ValueError("review.state=disagreed 必须提供 basis（可核验依据）")
        return self


class QuestionResult(StrictModel):
    id: str
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

    @model_validator(mode="after")
    def _check_status(self) -> "DeliveryItem":
        if self.status not in DELIVERY_STATUSES:
            raise ValueError(f"delivery.status 非法: {self.status}")
        return self


class DeliveryReport(StrictModel):
    pdf: DeliveryItem = Field(default_factory=DeliveryItem)
    email: DeliveryItem = Field(default_factory=DeliveryItem)
    git: DeliveryItem = Field(default_factory=DeliveryItem)


class ReviewSummary(StrictModel):
    state: str = "not_run"
    scope: int = 0
    disagreed: int = 0
    unverified: int = 0
    note: str = ""

    @model_validator(mode="after")
    def _check_state(self) -> "ReviewSummary":
        if self.state not in REVIEW_SUMMARY_STATES:
            raise ValueError(f"review_summary.state 非法: {self.state}")
        return self


class StudyResult(StrictModel):
    """新协议结果（schema_version=2）。"""

    schema_version: int = SCHEMA_VERSION
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
    def _check_all(self) -> "StudyResult":
        if self.task_type not in TASK_TYPES:
            raise ValueError(f"task_type 非法: {self.task_type}")

        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"题目 id 重复: {', '.join(dup)}")

        counts = count_statuses(self.questions)
        # overview 允许由模型留空（全 0），但填了就必须与逐题数据一致
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


def count_statuses(questions: List[QuestionResult]) -> Dict[str, int]:
    counts = {s: 0 for s in QUESTION_STATUSES}
    for q in questions:
        counts[q.status] += 1
    return {k: v for k, v in counts.items()}


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


def _legacy_question_to_v2(q: LegacyQuestion) -> Dict[str, Any]:
    return {
        "id": f"legacy-{q.no or 'q'}",
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
    }


def normalize_result(raw: Any) -> Optional[Dict[str, Any]]:
    """把数据库中的结果 JSON 转换成统一展示结构。

    返回 None 表示既不是 v1 也不是 v2；调用方应如实说明「结果格式无法识别」。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict) or not raw:
        return None

    if raw.get("schema_version") == SCHEMA_VERSION or "questions" in raw and "task_type" in raw:
        try:
            data = StudyResult.model_validate(raw).model_dump()
        except Exception:  # noqa: BLE001 - 无法识别时按未知处理
            pass
        else:
            data["legacy"] = False
            data["task_type_label"] = _TASK_TYPE_LABELS.get(data["task_type"], data["task_type"])
            return data

    try:
        legacy = LegacyResult.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None

    questions = [_legacy_question_to_v2(q) for q in legacy.questions]
    counts = {s: 0 for s in QUESTION_STATUSES}
    for q in questions:
        counts[q["status"]] += 1
    return {
        "schema_version": LEGACY_SCHEMA_VERSION,
        "legacy": True,
        "task_type": "grading",
        "task_type_label": _TASK_TYPE_LABELS["grading"],
        "subject": "",
        "grade_level": "",
        "scope": {"start_date": "", "end_date": "", "sources": []},
        "overview": {
            "checked_questions": max(legacy.total_questions, len(questions)),
            "summary": legacy.summary,
            **counts,
        },
        "questions": questions,
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
    }


# --------------------------- 请求模型 ---------------------------


class StudyTaskCreate(StrictModel):
    task_type: str = "grading"
    subject: str = "数学"
    grade_level: str = ""
    text: str = ""
    asset_ids: List[str] = Field(default_factory=list)
    scope_start: str = ""
    scope_end: str = ""

    @model_validator(mode="after")
    def _check(self) -> "StudyTaskCreate":
        if self.task_type not in TASK_TYPES:
            raise ValueError(f"task_type 非法: {self.task_type}")
        if not self.text.strip() and not self.asset_ids:
            raise ValueError("必须提供文字说明或至少一张图片")
        if len(self.asset_ids) > 20:
            raise ValueError("单次任务图片不能超过 20 张")
        return self


class FollowupCreate(StrictModel):
    text: str = ""
    asset_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "FollowupCreate":
        if not self.text.strip() and not self.asset_ids:
            raise ValueError("补充材料必须包含文字或图片")
        return self


def request_hash(payload: Dict[str, Any]) -> str:
    """幂等请求指纹：同一幂等键 + 同一内容 → 返回原任务；内容不同 → 冲突。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def grading_result_to_v2(grading_result: Any, subject: str = "",
                         grade_level: str = "", provider: str = "") -> Dict[str, Any]:
    """把 legacy 单轮批改结果转换为新协议结构。

    关键：明确标注「未执行技能流程与二次核查」，不把旧模式伪装成技能执行成功。
    """
    questions: List[Dict[str, Any]] = []
    for index, q in enumerate(getattr(grading_result, "questions", []) or [], start=1):
        qno = getattr(q, "no", "") or str(index)
        steps = list(getattr(q, "explanation", []) or [])
        correct_answer = getattr(q, "correct_answer", "") or ""
        is_correct = bool(getattr(q, "is_correct", False))
        if is_correct:
            status, final = "correct", "kept_correct"
            error_rule = ""
        elif correct_answer or steps:
            status, final = "wrong", "kept_wrong"
            error_rule = "旧模式未给出具体错因规则，需人工复核"
        else:
            status, final = "uncertain", "kept_uncertain"
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
