"""服务端二次复查（第二模型）纯逻辑：候选选择、基线规范化、身份核验、覆盖对账与合并。

设计边界：
- 本模块不做网络、不碰数据库；IO 一律由调用方（TaskRunner / HermesClient）完成。
- 复查只写 review / review_summary 等服务端管理字段，**不改**学业判定
  （status、学生作答、答案、步骤、remediation、retests 原样保留）。
- 首轮模型自述的复查结论一律以本模块的规范化为准确认；未执行就是未执行。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

# 只有判错题与存疑题进入复查；unanswered 不送，避免把没作答的题当错题
REVIEW_CANDIDATE_STATUSES = ("wrong", "uncertain")

IDENTITY_CONFIRMED = "confirmed"
IDENTITY_MODEL_ONLY = "model_only"
IDENTITY_MISMATCH = "mismatch"
IDENTITY_UNKNOWN = "unknown"

# 可以据此采纳本次复查结论的身份核验结果（model_only：模型名已核对、provider 网关未报告）
IDENTITY_ACCEPTED = (IDENTITY_CONFIRMED, IDENTITY_MODEL_ONLY)


def _empty_summary() -> Dict[str, Any]:
    return {
        "state": "not_run", "scope": 0, "disagreed": 0, "unverified": 0,
        "note": "", "target_count": 0, "unprocessed": 0,
        "model_requested": "", "model_reported": "", "model_identity": "",
        "coverage": "",
    }


def is_candidate(question: Dict[str, Any]) -> bool:
    return question.get("status") in REVIEW_CANDIDATE_STATUSES


def select_review_targets(questions: List[Dict[str, Any]],
                          max_questions: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    """挑出应复查题：wrong + uncertain，按原顺序稳定；超限时判错题优先。

    返回 (送审题列表, 超限未送审的题目 id 列表)。
    """
    candidates = [q for q in questions if is_candidate(q)]
    if len(candidates) <= max_questions:
        return list(candidates), []
    wrong = [q for q in candidates if q.get("status") == "wrong"]
    uncertain = [q for q in candidates if q.get("status") == "uncertain"]
    sent = (wrong + uncertain)[:max_questions]
    sent_ids = {q["id"] for q in sent}
    overflow = [q["id"] for q in candidates if q["id"] not in sent_ids]
    return sent, overflow


def _normalize(result: Dict[str, Any]) -> Dict[str, Any]:
    """把首轮结果复制成复查基线：清除模型自述的复查字段，改由服务端管理。

    候选题标 unprocessed（尚无复查结果），其余题 not_applicable；
    review_summary 清零。学业判定字段原样保留。
    """
    data = copy.deepcopy(result)
    for q in data.get("questions") or []:
        candidate = is_candidate(q)
        q["review"] = {
            "state": "unprocessed" if candidate else "not_applicable",
            "note": "", "basis": "",
            "transcript_ok": None, "reread_answer": "",
        }
    data["review_summary"] = _empty_summary()
    return data


def _set_candidate_review(data: Dict[str, Any], state_by_id: Optional[Dict[str, str]],
                          default_state: str, note: str) -> None:
    for q in data.get("questions") or []:
        if not is_candidate(q):
            q["review"] = {"state": "not_applicable", "note": "", "basis": "",
                           "transcript_ok": None, "reread_answer": ""}
            continue
        state = (state_by_id or {}).get(q["id"], default_state)
        q["review"] = {"state": state, "note": note, "basis": "",
                       "transcript_ok": None, "reread_answer": ""}


def apply_not_required(result: Dict[str, Any]) -> Dict[str, Any]:
    """没有判错/存疑题：无需复查（优先于配置检查）。"""
    data = _normalize(result)
    _set_candidate_review(data, None, "not_applicable", "")
    data["review_summary"] = {**_empty_summary(), "state": "not_required",
                              "note": "本次没有判错题与存疑题，无需二次复查"}
    return data


def apply_skipped_after_staged(result: Dict[str, Any]) -> Dict[str, Any]:
    """分阶段批改已含独立求解与比对判定、且本次无错题/存疑题：跳过二次复查。

    与 apply_not_required 的区别只是如实说明跳过原因：不是"没配复查模型"，
    也不是"单次调用无需复查"，而是分阶段流水线本身已覆盖了复查想抓的问题。
    """
    data = _normalize(result)
    _set_candidate_review(data, None, "not_applicable", "")
    data["review_summary"] = {
        **_empty_summary(), "state": "not_required",
        "note": "分阶段批改已做独立求解与比对判定，本次无错题与存疑题，跳过二次复查",
    }
    return data


def apply_not_configured(result: Dict[str, Any]) -> Dict[str, Any]:
    """未配置复查模型：如实标 not_run，目标题未送审。"""
    data = _normalize(result)
    target_count = sum(1 for q in data.get("questions") or [] if is_candidate(q))
    _set_candidate_review(data, None, "unprocessed", "")
    data["review_summary"] = {
        **_empty_summary(), "state": "not_run", "target_count": target_count,
        "unprocessed": target_count,
        "note": "服务端未配置复查模型（hermes.review_model），未执行二次复查",
    }
    return data


def apply_not_run(result: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """有候选题但未派发（预算耗尽等）：如实标 not_run。"""
    data = _normalize(result)
    target_count = sum(1 for q in data.get("questions") or [] if is_candidate(q))
    _set_candidate_review(data, None, "unprocessed", "")
    data["review_summary"] = {
        **_empty_summary(), "state": "not_run", "target_count": target_count,
        "unprocessed": target_count, "note": reason,
    }
    return data


def apply_failed(result: Dict[str, Any], sent: List[Dict[str, Any]],
                 overflow: List[str], reason: str,
                 meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """复查调用失败/输出非法/身份不可信：目标题如实标注，保留首轮成果。"""
    data = _normalize(result)
    meta = meta or {}
    sent_ids = {q["id"] for q in sent}
    state_by_id = {qid: "unverified" for qid in sent_ids}
    _set_candidate_review(data, state_by_id, "unprocessed", "")
    data["review_summary"] = {
        **_empty_summary(),
        "state": "failed", "scope": len(sent_ids), "unverified": len(sent_ids),
        "unprocessed": len(overflow),
        "target_count": len(sent_ids) + len(overflow),
        "model_requested": meta.get("model_requested", ""),
        "model_reported": meta.get("model_reported", ""),
        "model_identity": meta.get("model_identity", ""),
        "coverage": meta.get("coverage", ""),
        "note": reason[:300],
    }
    return data


def check_model_identity(review_payload: Dict[str, Any],
                         first_round_payload: Dict[str, Any],
                         expected_model: str,
                         expected_provider: str) -> Tuple[str, str]:
    """核验收复查是否真的用了目标模型且不同于首轮模型。

    返回 (confirmed / model_only / mismatch / unknown, 说明)。判据：
    - 网关没报告实际模型 → unknown（不能用请求值冒充实际值）；
    - 报告的模型与首轮相同 → mismatch（不是「另一个模型」）；
    - 配置了期望模型且报告模型不符 → mismatch；
    - 报告 provider 与期望不符 → mismatch（网关确实回了 provider，属于实据）；
    - 报告模型与期望一致、且不同于首轮模型，但网关没回 provider → model_only：
      模型这一层已核对，provider 只是次级旁证，不能因为网关不回该字段让复查永久失败；
    - 模型与 provider 都核对一致 → confirmed。
    """
    reported = (review_payload.get("reported_model") or "").strip()
    reported_provider = (review_payload.get("reported_provider") or "").strip()
    first_reported = (first_round_payload.get("reported_model") or "").strip()
    if not reported:
        return IDENTITY_UNKNOWN, "网关未报告复查实际使用的模型，身份无法确认"
    if first_reported and reported == first_reported:
        return IDENTITY_MISMATCH, f"复查与首轮使用同一模型（{reported}），不是独立第二模型"
    expected = (expected_model or "").strip()
    if not expected:
        return IDENTITY_UNKNOWN, (
            f"网关报告模型为 {reported}（与首轮不同），但未配置 hermes.review_expected_model，"
            "无法确认就是目标复查模型")
    if reported != expected:
        return IDENTITY_MISMATCH, f"网关报告模型为 {reported}，与配置的期望模型 {expected} 不符"
    ep = (expected_provider or "").strip()
    if ep:
        if not reported_provider:
            return IDENTITY_MODEL_ONLY, (
                f"网关报告模型 {reported} 与期望一致且不同于首轮模型，"
                f"但网关未报告 provider，无法核对是否为 {ep}")
        if reported_provider != ep:
            return IDENTITY_MISMATCH, (
                f"provider 不符：报告 {reported_provider}，期望 {ep}")
    return IDENTITY_CONFIRMED, f"网关报告模型 {reported} 与配置的复查身份一致"


def reconcile_reviews(sent: List[Dict[str, Any]],
                      review_items: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """按实际送审 id 集合对账复查输出。

    返回 (id → 复查项, 问题列表)。漏题、未知 id、重复 id 都进问题列表，
    由调用方整体拒绝本次复查响应（送审题标 unverified），不做静默合并。
    """
    sent_ids = {q["id"] for q in sent}
    by_id: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []
    for item in review_items or []:
        qid = (item.get("id") or "").strip()
        if qid not in sent_ids:
            problems.append(f"返回了未送审的题 {qid}")
            continue
        if qid in by_id:
            problems.append(f"题 {qid} 重复返回")
            continue
        by_id[qid] = item
    missing = [qid for qid in sent_ids if qid not in by_id]
    if missing:
        problems.append(f"{len(missing)} 道送审题未返回复查结论")
    return by_id, problems


# 复查图片链路故障的信号词：复查模型明确表示没拿到原图像素
# （2026-10-08 生产事故：网关 tencent-tokenhub 把附图替换成了文字摘要，
# 14 道题全部 unverified，复查形同虚设）
_IMAGE_LINK_FAILURE_HINTS = ("未能直接读取", "无法读取原图", "文字摘要",
                             "未获得可直接读取")


def detect_image_link_failure(reviews_by_id: Dict[str, Dict[str, Any]],
                              coverage: str) -> bool:
    """检测复查图片链路故障。

    明明附了原图（coverage=reread），复查方却全部以"没拿到图"为由
    unverified（transcript_ok 全 null）→ 图片根本没送达模型，
    复查形同虚设，必须让运维去修网关/换视觉模型，而不是默默接受。
    """
    if coverage != "reread" or not reviews_by_id:
        return False
    hinted = 0
    for item in reviews_by_id.values():
        if item.get("transcript_ok") is not None or item.get("state") != "unverified":
            return False
        note = str(item.get("note") or "")
        if any(h in note for h in _IMAGE_LINK_FAILURE_HINTS):
            hinted += 1
    return hinted > 0


def apply_review_result(result: Dict[str, Any], sent: List[Dict[str, Any]],
                        reviews_by_id: Dict[str, Dict[str, Any]],
                        overflow: List[str],
                        meta: Dict[str, Any]) -> Dict[str, Any]:
    """把对账后的复查结论合并进结果，并重写 review_summary。

    只更新 review / review_summary / 异议时的 final_decision_basis；
    final_decision 等学业判定字段原样保留（异议≠改判）。
    """
    data = _normalize(result)
    disagreed = 0
    unverified = 0
    notes: List[str] = []
    coverage = meta.get("coverage", "")
    for q in data.get("questions") or []:
        if not is_candidate(q):
            continue
        item = reviews_by_id.get(q["id"])
        if item is None:
            continue  # 超限未送审：规范化时已是 unprocessed
        state = item.get("state", "unverified")
        q["review"] = {"state": state, "note": item.get("note", ""),
                       "basis": item.get("basis", ""),
                       "transcript_ok": item.get("transcript_ok"),
                       "reread_answer": item.get("reread_answer", "") or ""}
        if state == "disagreed":
            disagreed += 1
            prev_basis = (q.get("final_decision_basis") or "").strip()
            extra = ("[服务端二次复查记录到异议，尚未重新裁决] "
                     f"复查依据：{item.get('basis', '')}")
            q["final_decision_basis"] = (
                f"{extra}；首轮依据：{prev_basis}" if prev_basis else extra)
        elif state == "unverified":
            unverified += 1

    if coverage == "transcript_only":
        notes.append("复查仅依据文字转写（提取的题干/学生作答）与首轮结论核查，未读取原图")
    elif coverage == "reread":
        notes.append("复查先对照原图做转写二次确认（重读学生作答、核对转写），再核查首轮结论")
    if meta.get("model_identity") == IDENTITY_MODEL_ONLY:
        notes.append("网关未报告 provider，复查模型身份仅按模型名核对")
    if overflow:
        notes.append(f"{len(overflow)} 道题超过单次复查上限未送审")
    if unverified:
        notes.append(f"{unverified} 道题复查方无法核查")
    if detect_image_link_failure(reviews_by_id, coverage):
        notes.append("复查图片链路故障：已附原图但复查方表示未能直接读取，"
                     "图片未送达模型，本次复查形同虚设；请排查网关图片透传"
                     "或更换支持视觉的复查模型/路由")

    state = "completed"
    if unverified or overflow:
        state = "partial"

    sent_ids = {q["id"] for q in sent}
    data["review_summary"] = {
        **_empty_summary(),
        "state": state,
        "scope": len(sent_ids),
        "disagreed": disagreed,
        "unverified": unverified,
        "unprocessed": len(overflow),
        "target_count": len(sent_ids) + len(overflow),
        "model_requested": meta.get("model_requested", ""),
        "model_reported": meta.get("model_reported", ""),
        "model_identity": meta.get("model_identity", ""),
        "coverage": coverage,
        "note": "；".join(notes),
    }
    return data


def build_review_markdown(result: Dict[str, Any]) -> str:
    """由最终 review_summary 与逐题 review 生成归档用的「服务端二次复查记录」附记。

    只反映服务端真实执行的复查；首轮正文里自称的核查结论不在此列。
    无复查内容时返回空串。
    """
    summary = result.get("review_summary") or {}
    state = summary.get("state", "")
    if not state or state == "not_required":
        return ""
    # not_run / failed 也如实写附记：家长需要知道「有错题但这次没有完成第二模型复查」
    lines: List[str] = ["### 服务端二次复查记录", ""]
    state_labels = {
        "completed": "已完成", "partial": "部分完成", "failed": "未完成",
        "not_run": "未执行", "not_required": "无需复查",
    }
    parts = [f"状态：{state_labels.get(state, state)}"]
    if summary.get("target_count"):
        parts.append(f"应复查 {summary['target_count']} 题")
    if summary.get("scope"):
        parts.append(f"送审 {summary['scope']} 题")
    if summary.get("disagreed"):
        parts.append(f"异议 {summary['disagreed']} 题")
    if summary.get("unverified"):
        parts.append(f"无法核查 {summary['unverified']} 题")
    if summary.get("unprocessed"):
        parts.append(f"未送审 {summary['unprocessed']} 题")
    lines.append("；".join(parts) + "。")
    if summary.get("model_requested"):
        identity = {"confirmed": "已确认",
                    "model_only": "已核对模型名（网关未报告 provider）",
                    "mismatch": "路由不符",
                    "unknown": "身份未确认"}.get(summary.get("model_identity", ""),
                                                 summary.get("model_identity", ""))
        reported = summary.get("model_reported") or "（网关未报告）"
        lines.append(f"复查模型：请求 {summary['model_requested']}，"
                     f"网关报告 {reported}（{identity}）。")
    if summary.get("note"):
        lines.append(f"说明：{summary['note']}")
    for q in result.get("questions") or []:
        review = q.get("review") or {}
        if review.get("state") not in ("agreed", "disagreed", "unverified"):
            continue
        label = {"agreed": "未发现异议", "disagreed": "有异议",
                 "unverified": "无法核查"}.get(review["state"], review["state"])
        no = q.get("no") or q.get("id")
        detail = review.get("basis") or review.get("note") or ""
        entry = f"- 第 {no} 题：{label}"
        if detail:
            entry += f"——{detail}"
        if review.get("transcript_ok") is False:
            reread = (review.get("reread_answer") or "").strip() or "无法辨认"
            entry += f"；转写二次确认：与转写不符（原图重读作答「{reread}」）"
        lines.append(entry)
    return "\n".join(lines).strip()
