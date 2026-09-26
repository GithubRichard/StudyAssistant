"""持久化学习任务：幂等创建、数据库认领执行、轮次与结果落库。

可靠执行约定：
- 任务先入库再由执行器「认领」（claim + 租约），不依赖进程内后台任务存活。
- 已经开始派发的轮次在重启后标记 interrupted，不自动重放，避免重复归档或重复副作用。
- 配额预留只结算一次：确认未执行才退款，结果未确认时保持预留并如实上报。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from . import db, git_sync, grading, hermes, scope, workspace
from .config import Settings
from .hermes import HermesClient, HermesError, HermesUncertain
from .schemas import (fill_question_uids, grading_result_to_v3, normalize_result,
                      request_hash)

log = logging.getLogger(__name__)

TERMINAL_STATUSES = ("done", "failed", "waiting_input", "interrupted")
FOLLOWUP_ALLOWED = ("done", "waiting_input", "failed", "interrupted")


class TaskError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


# --------------------------- 创建 ---------------------------


async def create_study_task(settings: Settings, openid: str, payload: Dict[str, Any],
                            idempotency_key: str = "") -> Dict[str, Any]:
    """创建学习任务（含图片与文字）。幂等键相同且内容一致时返回原任务。"""
    asset_ids: List[str] = list(payload.get("asset_ids") or [])
    if not (payload.get("text") or "").strip() and not asset_ids:
        raise TaskError("必须提供文字说明或至少一张图片")
    if len(asset_ids) > settings.limits.max_assets_per_task:
        raise TaskError(f"单次任务最多 {settings.limits.max_assets_per_task} 张图片", 413)

    assets = await _load_owned_assets(settings, openid, asset_ids)
    total_bytes = sum(a["bytes"] for a in assets)
    if total_bytes > settings.limits.max_total_upload_mb * 1024 * 1024:
        raise TaskError(f"图片总大小超过 {settings.limits.max_total_upload_mb}MB", 413)

    if await db.get_daily_cost(settings.db_path) >= settings.budget.daily_max_cny:
        raise TaskError("今日服务额度已用完，请明天再试", 503)

    task_id = _new_id()
    decision = scope.compute_scope(
        task_type=payload.get("task_type") or "grading",
        training_kind=payload.get("training_kind") or "",
        scope_start=payload.get("scope_start") or "",
        scope_end=payload.get("scope_end") or "",
        term_start_date=await _term_start_date(settings, openid),
    )
    task = {
        "id": task_id,
        "openid": openid,
        # 学科未指定时保留空串，由模型按材料判断；不擅自替用户假定学科
        "subject": (payload.get("subject") or "").strip(),
        "grade_level": payload.get("grade_level") or "",
        "task_type": payload.get("task_type") or "grading",
        "input_text": payload.get("text") or "",
        "image_path": assets[0]["path"] if assets else "",
        "status": "pending",
        "created_at": time.time(),
        "exam_scope": payload.get("exam_scope") or "",
        "training_kind": payload.get("training_kind") or "",
    }
    # 区间由服务端按规范计算后持久化，执行阶段不再依赖客户端重复传参
    task["scope_start"] = decision.start_date
    task["scope_end"] = decision.end_date

    idem = None
    if idempotency_key:
        idem = {
            "key": idempotency_key[:128],
            "openid": openid,
            "endpoint": "study_task",
            "request_hash": request_hash({
                "openid": openid, "payload": payload, "assets": asset_ids,
            }),
        }

    result = await db.create_task_atomic(
        settings.db_path, task,
        daily_free=settings.quota.daily_free,
        max_per_day=settings.quota.max_per_day,
        idempotency=idem,
    )
    if result.get("conflict"):
        raise TaskError("相同幂等键提交了不同内容，请更换 Idempotency-Key", 409)
    if not result.get("ok"):
        raise TaskError(result.get("reason", "创建任务失败"), 429)

    final_task_id = result["task_id"]
    if result.get("duplicate"):
        existing = await db.get_task(settings.db_path, final_task_id)
        return {"task_id": final_task_id, "status": (existing or {}).get("status", "pending"),
                "duplicate": True, "runs": 0, "scope": decision.as_dict()}

    run = _new_run(task_id=final_task_id, run_no=1, kind="initial",
                   input_text=task["input_text"])
    await db.create_run(settings.db_path, run)
    await db.link_task_assets(settings.db_path, final_task_id, run["id"], asset_ids)
    await db.update_task(settings.db_path, final_task_id, run_count=1)
    log.info("创建学习任务 task_id=%s type=%s kind=%s scope=%s~%s assets=%d",
             final_task_id, task["task_type"], task.get("training_kind", ""),
             decision.start_date or "-", decision.end_date or "-", len(asset_ids))
    return {"task_id": final_task_id, "status": "pending", "duplicate": False, "runs": 1,
            "scope": decision.as_dict()}


async def _term_start_date(settings: Settings, openid: str) -> str:
    """学期起始日期：优先小程序设置页保存值，其次配置文件默认值；都没有则返回空。"""
    stored = await db.get_family_settings(settings.db_path, openid)
    if stored and (stored.get("term_start_date") or "").strip():
        return str(stored["term_start_date"]).strip()
    return (settings.family.term_start_date or "").strip()


async def add_followup(settings: Settings, openid: str, task_id: str,
                       payload: Dict[str, Any]) -> Dict[str, Any]:
    """对已完成或待补充任务追加材料，创建新的执行轮次。"""
    task = await db.get_task(settings.db_path, task_id)
    if not task or task["openid"] != openid:
        raise TaskError("任务不存在", 404)
    if task["status"] not in FOLLOWUP_ALLOWED:
        raise TaskError(f"任务当前状态（{task['status']}）不接受补充材料", 409)
    if await db.has_unconfirmed_run(settings.db_path, task_id):
        raise TaskError("上一次执行结果尚未确认，请稍后重试或联系管理员", 409)
    if task.get("run_count", 0) >= settings.limits.max_runs_per_task:
        raise TaskError(f"该任务最多追加 {settings.limits.max_runs_per_task} 轮", 429)

    asset_ids = list(payload.get("asset_ids") or [])
    assets = await _load_owned_assets(settings, openid, asset_ids)
    if len(asset_ids) > settings.limits.max_assets_per_task:
        raise TaskError(f"单次最多 {settings.limits.max_assets_per_task} 张图片", 413)

    run_no = int(task.get("run_count", 0)) + 1
    run = _new_run(task_id=task_id, run_no=run_no, kind="followup",
                   input_text=payload.get("text") or "")
    await db.create_run(settings.db_path, run)
    await db.link_task_assets(settings.db_path, task_id, run["id"], asset_ids)
    await db.update_task(settings.db_path, task_id, status="pending", run_count=run_no,
                         error="", claim_owner="", claim_expires_at=0)
    log.info("任务补充材料 task_id=%s run_no=%d", task_id, run_no)
    return {"task_id": task_id, "run_id": run["id"], "run_no": run_no, "status": "pending"}


def _new_run(task_id: str, run_no: int, kind: str, input_text: str) -> Dict[str, Any]:
    return {
        "id": _new_id(),
        "task_id": task_id,
        "run_no": run_no,
        "kind": kind,
        "input_text": input_text,
        "status": "queued",
        "created_at": time.time(),
    }


async def _load_owned_assets(settings: Settings, openid: str,
                             asset_ids: List[str]) -> List[Dict[str, Any]]:
    if not asset_ids:
        return []
    rows = await db.list_assets(settings.db_path, asset_ids)
    by_id = {r["id"]: r for r in rows}
    ordered: List[Dict[str, Any]] = []
    for asset_id in asset_ids:
        row = by_id.get(asset_id)
        if not row:
            raise TaskError(f"附件不存在: {asset_id}", 404)
        if row["openid"] != openid:
            raise TaskError("无权使用该附件", 404)
        ordered.append(row)
    return ordered


# --------------------------- 执行器 ---------------------------


class TaskRunner:
    """单进程单并发执行器：从数据库认领待执行任务。"""

    def __init__(self, settings: Settings, client: HermesClient) -> None:
        self.settings = settings
        self.client = client
        self.owner = f"worker-{uuid.uuid4().hex[:6]}"
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._sem = asyncio.Semaphore(1)

    async def start(self) -> None:
        if not self.settings.limits.worker_enabled:
            log.warning("执行器已按配置停用（limits.worker_enabled=false）")
            return
        recovered = await db.recover_interrupted(self.settings.db_path)
        if recovered:
            log.warning("检测到 %d 个未确认任务，已标记 interrupted: %s",
                        len(recovered), ", ".join(recovered))
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        poll = self.settings.limits.worker_poll_seconds
        while not self._stop.is_set():
            try:
                async with self._sem:
                    task = await db.claim_next_task(
                        self.settings.db_path, self.owner,
                        lease_seconds=self.settings.limits.claim_lease_seconds)
                    if task:
                        await self.execute(task)
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - worker 不应因单次异常退出
                log.exception("执行器循环异常")
            await asyncio.sleep(poll)

    async def execute(self, task: Dict[str, Any]) -> None:
        s = self.settings
        task_id = task["id"]
        run = await self._current_run(task)
        if run is None:
            await db.update_task(s.db_path, task_id, status="failed",
                                 error="任务缺少可执行轮次")
            return

        output_dir = workspace.run_output_dir(s, task_id, run["run_no"])
        await db.update_run(s.db_path, run["id"], status="running", started_at=time.time())
        session_id = f"study-{task_id}-{run['run_no']}"

        try:
            assets = await self._collect_assets(task, run)
            timeout = min(s.hermes.timeout_seconds, s.limits.max_task_minutes * 60)
            log.info("开始执行 task_id=%s run_no=%d timeout=%.0fs assets=%d",
                     task_id, run["run_no"], timeout, len(assets))
            if s.is_hermes:
                scope_info = scope.describe_scope(task, await _term_start_date(s, task["openid"]))
                messages = hermes.build_messages(
                    s, {**task, "input_text": task.get("input_text", ""),
                        "scope_note": scope_info["note"],
                        "scope_missing": scope_info["missing"]},
                    {**run, "output_dir": str(output_dir)}, assets)
                payload = await asyncio.wait_for(
                    self.client.run_task(messages, session_id), timeout=timeout)
            else:
                payload = await asyncio.wait_for(
                    self._run_legacy(task, assets), timeout=timeout)
        except asyncio.TimeoutError:
            await self._mark_uncertain(task, run, "执行超时，结果未确认")
            return
        except HermesUncertain as e:
            await self._mark_uncertain(task, run, str(e))
            return
        except HermesError as e:
            await self._mark_failed(task, run, str(e),
                                   certain_not_executed=getattr(e, "certain_not_executed", False))
            return
        except workspace.WorkspaceError as e:
            await self._mark_failed(task, run, f"材料读取失败: {e}", certain_not_executed=True)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("任务执行异常 task_id=%s", task_id)
            await self._mark_uncertain(task, run, f"内部错误，结果未确认: {e}")
            return

        await self._finish(task, run, payload, output_dir)

    async def _current_run(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        runs = await db.list_runs(self.settings.db_path, task["id"])
        for run in runs:
            if run["status"] in ("queued", "running"):
                return run
        return runs[-1] if runs else None

    async def _collect_assets(self, task: Dict[str, Any],
                              run: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = await db.list_task_assets(self.settings.db_path, task["id"], run["id"])
        if not rows and run["kind"] == "initial":
            rows = await db.list_task_assets(self.settings.db_path, task["id"])
        if not rows and task.get("image_path"):
            legacy = task["image_path"]
            rows = [{"id": "legacy", "path": legacy, "mime": "image/jpeg", "bytes": 0}]
        assets: List[Dict[str, Any]] = []
        for row in rows:
            assets.append({**row, "data_url": workspace.load_asset_data_url(row)})
        return assets

    async def _run_legacy(self, task: Dict[str, Any],
                          assets: List[Dict[str, Any]]) -> Dict[str, Any]:
        """显式的旧模式：单图 + 多模型直连。结果按新协议转换，并如实标注未执行二次核查。"""
        if not assets:
            raise TaskError("旧模式需要一张作业图片")
        data_url = assets[0]["data_url"]
        mime = assets[0].get("mime", "image/jpeg")
        import base64

        raw = base64.b64decode(data_url.split(",", 1)[1])
        result, provider, model, itok, otok, cost = await grading.grade_image(
            raw, mime, task.get("subject", ""), task.get("grade_level", ""), self.settings)
        await db.add_daily_cost(self.settings.db_path, cost)
        return {
            "result": grading_result_to_v3(
                result, task.get("subject", ""), task.get("grade_level", ""), provider),
            "model": f"{provider}/{model}",
            "usage": {"prompt_tokens": itok, "completion_tokens": otok},
        }

    async def _finish(self, task: Dict[str, Any], run: Dict[str, Any],
                      payload: Dict[str, Any], output_dir) -> None:
        s = self.settings
        task_id = task["id"]
        result = payload["result"]

        # 服务端回填稳定去重键，并把区间缺口与考试范围说明如实并入 missing_info
        scope_info = scope.describe_scope(task, await _term_start_date(s, task["openid"]))
        result = fill_question_uids(result, time.strftime("%Y-%m-%d"))
        missing = result.setdefault("missing_info", [])
        for item in scope_info["missing"]:
            if item not in missing:
                missing.append(item)
        result_scope = dict(result.get("scope") or {})
        result_scope.setdefault("sources", [])
        if not result_scope.get("start_date"):
            result_scope["start_date"] = task.get("scope_start", "")
        if not result_scope.get("end_date"):
            result_scope["end_date"] = task.get("scope_end", "")
        result["scope"] = result_scope
        if not result.get("exam_scope"):
            result["exam_scope"] = task.get("exam_scope", "")
        if not result.get("training_kind"):
            result["training_kind"] = task.get("training_kind", "")

        archive = await workspace.apply_archive(s, task, run, result)

        # 归档成功后执行受控 Git 同步（仅提交本次授权文件）；未启用或失败都如实记录
        git_result: Optional[Dict[str, Any]] = None
        if s.git_sync_enabled:
            git_result = await git_sync.sync_workspace(
                s, task=task, run=run, archive=archive, result=result)
            await db.log_git_sync(s.db_path, {**git_result, "task_id": task_id})
        result["delivery"] = workspace.summarize_delivery(s, result, archive, git_result)

        # 台账：错题与存疑题按去重键写入/更新，复测事件只追加不改写历史
        ledger_count = await _write_ledger(s, task, result, archive)

        artifact_row = workspace.archive_artifact(s, task_id, archive)
        if artifact_row:
            artifact_row["run_id"] = run["id"]
            await db.add_artifact(s.db_path, artifact_row)
        for row in workspace.collect_artifacts(s, task_id, run["run_no"]):
            row["run_id"] = run["id"]
            await db.add_artifact(s.db_path, row)

        status = "waiting_input" if result.get("missing_info") else "done"
        await db.update_run(
            s.db_path, run["id"], status=status, finished_at=time.time(),
            result_json=json.dumps(result, ensure_ascii=False),
            hermes_session_id=f"study-{task_id}-{run['run_no']}",
        )
        await db.update_task(
            s.db_path, task_id, status=status,
            result_json=json.dumps(result, ensure_ascii=False),
            provider="hermes", model=payload.get("model", ""),
            input_tokens=int((payload.get("usage") or {}).get("prompt_tokens", 0) or 0),
            output_tokens=int((payload.get("usage") or {}).get("completion_tokens", 0) or 0),
            error="", claim_owner="", claim_expires_at=0,
            archive_path=archive.get("path", "") or "",
            git_status=(git_result or {}).get("status", "") if s.git_sync_enabled else "not_configured",
        )
        await db.settle_reservation(s.db_path, task_id)
        log.info("任务完成 task_id=%s run_no=%d status=%s archive=%s git=%s ledger=%d",
                 task_id, run["run_no"], status, archive.get("status"),
                 (git_result or {}).get("status", "-"), ledger_count)


    async def _mark_failed(self, task: Dict[str, Any], run: Dict[str, Any], message: str,
                           certain_not_executed: bool) -> None:
        s = self.settings
        await db.update_run(s.db_path, run["id"], status="failed",
                            finished_at=time.time(), error=message[:500])
        await db.update_task(s.db_path, task["id"], status="failed", error=message[:500],
                            claim_owner="", claim_expires_at=0)
        if certain_not_executed:
            await db.release_reservation(s.db_path, task["id"], refund=True)
        log.error("任务失败 task_id=%s: %s", task["id"], message)

    async def _mark_uncertain(self, task: Dict[str, Any], run: Dict[str, Any],
                              message: str) -> None:
        s = self.settings
        note = f"{message}（远端可能已执行，未自动重试，也未退还次数）"
        await db.update_run(s.db_path, run["id"], status="interrupted",
                            finished_at=time.time(), error=note[:500])
        await db.update_task(s.db_path, task["id"], status="interrupted", error=note[:500],
                            claim_owner="", claim_expires_at=0)
        log.warning("任务结果未确认 task_id=%s: %s", task["id"], message)

# --------------------------- 视图 ---------------------------


async def build_task_view(settings: Settings, task: Dict[str, Any]) -> Dict[str, Any]:
    """统一任务视图：新旧结果都能读，状态与缺口如实呈现。"""
    runs = await db.list_runs(settings.db_path, task["id"])
    artifacts = await db.list_artifacts(settings.db_path, task["id"])
    result = normalize_result(task.get("result_json"))

    ledger = await db.list_ledger_by_task(settings.db_path, task["openid"], task["id"])
    return {
        "id": task["id"],
        "status": task["status"],
        "task_type": task.get("task_type", "grading"),
        "subject": task.get("subject", ""),
        "grade_level": task.get("grade_level", ""),
        "exam_scope": task.get("exam_scope", ""),
        "training_kind": task.get("training_kind", ""),
        "scope_start": task.get("scope_start", ""),
        "scope_end": task.get("scope_end", ""),
        "git_status": task.get("git_status", ""),
        "archive_path": workspace.workspace_relative_path(settings, task.get("archive_path", "")),
        "ledger": [
            {
                "id": row["id"], "question_uid": row.get("question_uid", ""),
                "no": row.get("question_no", ""), "source": row.get("source", ""),
                "page": row.get("page", ""), "status": row.get("status", ""),
                "knowledge_point": row.get("knowledge_point", ""),
                "remediation_state": row.get("remediation_state", ""),
            }
            for row in ledger
        ],
        "provider": task.get("provider", ""),
        "model": task.get("model", ""),
        "error": task.get("error", ""),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "run_count": task.get("run_count", 0),
        "runs": [
            {
                "id": r["id"], "run_no": r["run_no"], "kind": r["kind"],
                "status": r["status"], "error": r.get("error", ""),
                "started_at": r.get("started_at"), "finished_at": r.get("finished_at"),
            }
            for r in runs
        ],
        "artifacts": [
            {"id": a["id"], "kind": a["kind"], "bytes": a["bytes"],
             "download_url": f"/api/tasks/{task['id']}/artifacts/{a['id']}"}
            for a in artifacts
        ],
        "result": result,
        "result_unknown": task.get("result_json") is not None and result is None,
    }


# --------------------------- 错题台账 ---------------------------

# 记入台账的题目状态：只记需要跟进的错题与存疑题，答对题不入台账
LEDGER_STATUSES = ("wrong", "uncertain")


def _ledger_state_for_event(result: str) -> str:
    """复测/订正事件对应的台账状态；无法识别时保持原状态（不猜测）。"""
    return {
        "retest_passed": "retest_passed",
        "retest_failed": "retest_failed",
        "corrected": "corrected_pending_retest",
    }.get(result, "")


async def _write_ledger(settings: Settings, task: Dict[str, Any], result: Dict[str, Any],
                        archive: Dict[str, Any]) -> int:
    """把本次结果写入台账：题目按 uid 去重，复测事件追加并更新对应状态。"""
    subject = result.get("subject") or task.get("subject") or ""
    archive_rel = workspace.workspace_relative_path(settings, archive.get("path", ""))
    openid = task["openid"]
    written = 0

    for question in result.get("questions") or []:
        uid = str(question.get("uid") or "").strip()
        if not uid or question.get("status") not in LEDGER_STATUSES:
            continue
        await db.upsert_ledger_question(settings.db_path, openid, {
            "question_uid": uid,
            "task_id": task["id"],
            "question_no": question.get("no", ""),
            "subject": subject,
            "source": question.get("source", ""),
            "page": question.get("page", ""),
            "stem": question.get("stem", ""),
            "student_answer": question.get("student_answer", ""),
            "correct_answer": question.get("correct_answer", ""),
            "error_rule": question.get("error_rule", ""),
            "knowledge_point": question.get("knowledge_point", ""),
            "status": question.get("status", ""),
            "remediation_state": (question.get("remediation") or {}).get("state", ""),
            "archive_path": archive_rel,
        })
        written += 1

    for event in result.get("retests") or []:
        uid = str(event.get("question_uid") or "").strip()
        if not uid:
            continue
        await db.add_question_event(settings.db_path, openid, {
            "question_uid": uid,
            "subject": subject,
            "event_type": "retest",
            "result": event.get("result", ""),
            "occurred_date": event.get("occurred_date", ""),
            "student_answer": event.get("student_answer", ""),
            "note": event.get("note", ""),
            "source_task_id": task["id"],
            "archive_path": archive_rel,
        })
        state = _ledger_state_for_event(event.get("result", ""))
        entry = await db.get_ledger_by_uid(settings.db_path, openid, uid)
        if entry and state:
            await db.update_ledger_state(
                settings.db_path, openid, entry["id"],
                remediation_state=state, archive_path=archive_rel)

    return written
