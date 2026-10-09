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

from . import db, git_sync, grading, hermes, review, scope, staged, workspace, orientation, thinking, diagram
from .config import Settings, provider_chain
from .hermes import HermesClient, HermesError, HermesUncertain
from .providers import make_provider
from .schemas import (fill_question_uids, grading_result_to_v3, normalize_result,
                      request_hash)
from .staged import StageError

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
    runs = await db.list_runs(settings.db_path, task_id)
    if runs and runs[-1].get("stage") == "orientation" and task["status"] == "waiting_input":
        raise TaskError("请先确认页面方向，原图片已保留，无需补交", 409)
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
        with thinking.task_context(task["id"], int(task.get("run_count") or 1)):
            await self._execute(task)

    async def _execute(self, task: Dict[str, Any]) -> None:
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
            budget = min(s.hermes.timeout_seconds, s.limits.max_task_minutes * 60)
            deadline = time.monotonic() + budget
            log.info("开始执行 task_id=%s run_no=%d budget=%.0fs assets=%d",
                     task_id, run["run_no"], budget, len(assets))
            prev_result: Optional[Dict[str, Any]] = None
            staged_chain = provider_chain(s) if (
                (task.get("task_type") or "grading") == "grading"
                and s.staged_grading.enabled) else []
            if run.get("kind") == "followup" and (staged_chain or s.is_hermes):
                # 修订基准：上一轮已落库的批阅结果；补充轮次做增量修订，不是重新批阅
                # （legacy 保持原行为：prev_result 为 None，不做覆盖校验）
                prev_result = normalize_result(task.get("result_json"))
            if staged_chain:
                # 分阶段批改优先：提取→独立求解→比对→诊断，准确率高于单次大调用
                payload = await asyncio.wait_for(
                    self._run_staged(task, run, assets, staged_chain, prev_result),
                    timeout=budget)
            elif s.is_hermes:
                scope_info = scope.describe_scope(task, await _term_start_date(s, task["openid"]))
                messages = hermes.build_messages(
                    s, {**task, "scope_note": scope_info["note"],
                        "scope_missing": scope_info["missing"]},
                    {**run, "output_dir": str(output_dir)}, assets, prev_result)
                payload = await asyncio.wait_for(
                    self.client.run_task(messages, session_id), timeout=budget)
            else:
                payload = await asyncio.wait_for(
                    self._run_legacy(task, assets), timeout=budget)
        except asyncio.TimeoutError:
            await self._mark_uncertain(task, run, "执行超时，结果未确认")
            return
        except orientation.ConfirmationRequired as e:
            await db.update_run(s.db_path, run["id"], status="waiting_input", error=str(e))
            await db.update_task(s.db_path, task_id, status="waiting_input", error=str(e),
                                 claim_owner="", claim_expires_at=0)
            return
        except StageError as e:
            # 阶段内所有模型都失败：任务未产出结果，退款并标失败，用户可重试
            await self._mark_failed(task, run, f"分阶段批改失败: {e.message}",
                                   certain_not_executed=True)
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

        # 首轮预处理（复查与归档共用）：学科回填、uid、补充轮次覆盖校验、区间与缺口
        result = await self._prepare_result(task, run, payload, prev_result)
        if result is None:
            return

        # 服务端二次复查（第二模型）：只写复查字段，失败不吞首轮成果
        if s.is_hermes:
            if (payload.get("provider") == "staged"
                    and s.hermes.review_configured
                    and not any(review.is_candidate(q)
                                for q in result.get("questions") or [])):
                # 分阶段已做独立求解与比对判定，本次又无错题/存疑题：
                # 复查不会有新的信息增益，跳过以省一次模型调用，如实标注
                result = review.apply_skipped_after_staged(result)
            else:
                result = await self._run_review(task, run, result, deadline, payload,
                                                assets)

        await self._finish(task, run, payload, result, output_dir, prev_result)

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

    async def _prepare_result(self, task: Dict[str, Any], run: Dict[str, Any],
                              payload: Dict[str, Any],
                              prev_result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """首轮结果预处理：学科回填、uid、补充轮次覆盖校验、区间与缺口合并。

        在复查与归档之前完成，保证复查与归档看到的是同一份已回填结果。
        补充轮次覆盖校验失败时标任务失败并返回 None。
        """

        s = self.settings
        result = payload["result"]

        # 学科：优先用模型按材料判断出的学科；模型没给时回落到任务上的学科
        if not (result.get("subject") or "").strip() and (task.get("subject") or "").strip():
            result["subject"] = task["subject"]

        # 服务端回填稳定去重键，并把区间缺口与考试范围说明如实并入 missing_info
        scope_info = scope.describe_scope(task, await _term_start_date(s, task["openid"]))
        result = fill_question_uids(result, time.strftime("%Y-%m-%d"))

        if run.get("kind") == "followup" and prev_result:
            # 修订模式 uid 覆盖校验：模型丢题说明把补充材料当成了新作业，
            # 此时绝不能覆盖上一轮结果；标失败并退还次数，用户可重新补充
            gaps = revision_coverage_gaps(prev_result, result)
            if gaps:
                sample = "、".join(gaps[:5]) + ("…" if len(gaps) > 5 else "")
                await self._mark_failed(
                    task, run,
                    f"补充轮次结果缺失上一轮 {len(gaps)} 道题（{sample}），"
                    f"已拒绝覆盖写入，次数已退还",
                    certain_not_executed=True)
                return None
            # 归档沿用上一轮路径并追加章节标题，同一任务的归档保持在同一文档
            arch = result.setdefault("archive", {})
            prev_path = (prev_result.get("archive") or {}).get("suggested_path", "")
            if prev_path:
                arch["suggested_path"] = prev_path
            md = (arch.get("content_markdown") or "").strip()
            if md and not md.startswith("## 补充材料"):
                arch["content_markdown"] = f"## 补充材料（第 {run['run_no']} 轮）\n\n{md}"
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
        return result

    async def _run_staged(self, task: Dict[str, Any], run: Dict[str, Any],
                          assets: List[Dict[str, Any]], chain: List[str],
                          prev_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """分阶段批改：提取 → 独立求解 → 比对判定 → 错因诊断。

        全部图片一次送入提取阶段（legacy 只取第一张）；每阶段产出经
        on_stage 回调写入 run.stage / run.stages_json，支持断点观察与按阶段重试。
        """
        import base64

        images: List[tuple] = []
        for a in assets:
            data_url = a.get("data_url") or ""
            if "," not in data_url:
                continue
            raw = base64.b64decode(data_url.split(",", 1)[1])
            images.append((raw, a.get("mime", "image/jpeg")))
        if not images:
            raise TaskError("分阶段批改需要作业图片")
        log.info("分阶段批改 task_id=%s run_no=%d images=%d followup=%s",
                 task["id"], run["run_no"], len(images),
                 bool(prev_result) and run.get("kind") == "followup")

        stages_store = _parse_stages_json(run.get("stages_json"))

        async def on_stage(name: str, data: Any) -> None:
            stages_store[name] = data
            if name == "extract":
                # 第一步产出直接打日志：使用者可核对 AI 是否读对了题目与学生答案
                log.info("【提取阶段转写】task_id=%s run_no=%d\n%s",
                         task["id"], run["run_no"],
                         staged.format_extraction_log(data))
            await db.update_run(
                self.settings.db_path, run["id"], stage=name,
                stages_json=json.dumps(stages_store, ensure_ascii=False))

        charged_cost = sum(p.get("cost", 0) for p in stages_store.get("orientation", {}).get("pages", []))

        async def on_orientation(data):
            nonlocal charged_cost
            current_cost = sum(p.get("cost", 0) for p in data["pages"])
            if current_cost > charged_cost:
                await db.add_daily_cost(self.settings.db_path, current_cost - charged_cost)
                charged_cost = current_cost
            await on_stage("orientation", data)

        images, direction = await orientation.prepare_pages(
            images, self.settings, chain, stages_store.get("orientation"), on_orientation)

        # 补充轮次用本轮文字（e745431 修复：不用任务创建时的文字）
        run_text = (run.get("input_text") or "").strip()
        input_text = run_text or (task.get("input_text") or "").strip()
        is_followup = run.get("kind") == "followup" and prev_result is not None

        outcome = await staged.grade_staged(
            images,
            task.get("subject", "") or "", task.get("grade_level", "") or "",
            input_text, self.settings, chain=chain,
            prev_result=prev_result if is_followup else None,
            followup_no=run["run_no"] if is_followup else 0,
            on_stage=on_stage, images_prepared=True)
        await db.add_daily_cost(self.settings.db_path, outcome.cost)
        await db.update_run(self.settings.db_path, run["id"], stage="done")
        return {
            "result": outcome.result,
            "provider": "staged",
            "model": f"staged:{outcome.model}",
            "usage": {"prompt_tokens": outcome.input_tokens + sum(p.get("input_tokens", 0) for p in direction["pages"]),
                      "completion_tokens": outcome.output_tokens + sum(p.get("output_tokens", 0) for p in direction["pages"])},
        }

    async def _run_review(self, task: Dict[str, Any], run: Dict[str, Any],
                          result: Dict[str, Any], deadline: float,
                          first_payload: Dict[str, Any],
                          assets: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """服务端二次复查编排（第二模型）：每轮最多一次调用。

        复查分两步（一次调用内完成）：先对照作业原图做转写二次确认
        （重读学生作答，抓"卷面是 A、转写成 B"这类识别错误），再做逻辑核查。
        只写 review / review_summary 等服务端管理字段；调用失败、身份不可信、
        覆盖对账不过、合并校验失败都只影响复查字段（如实标注），不吞首轮成果。
        取消异常原样上抛（服务停止仍按既有「结果未确认」纪律处理）。
        """
        s = self.settings
        h = s.hermes
        task_id, run_no = task["id"], run["run_no"]

        if not h.review_configured:
            return review.apply_not_configured(result)
        targets, overflow = review.select_review_targets(
            result.get("questions") or [], h.review_max_questions)
        if not targets:
            return review.apply_not_required(result)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning("复查未派发：任务预算已耗尽 task_id=%s", task_id)
            return review.apply_not_run(result, "任务时间预算已耗尽，复查未派发")

        # 转写二次确认：把作业原图附给复查模型，先重读学生作答核对转写，
        # 再核查首轮结论；拿不到原图时退化为纯文字核查。
        # 补充轮次复查覆盖全量候选题：取任务全部图片（不止本轮新增的）。
        review_assets = assets or []
        if run.get("kind") == "followup":
            rows = await db.list_task_assets(s.db_path, task_id)
            review_assets = [{**r, "data_url": workspace.load_asset_data_url(r)}
                             for r in rows]
        # 复查读同一套已确认方向的实际图片，避免首轮转正、复查又读横向原图。
        import base64
        import hashlib
        from . import image_prep
        directions = {}
        for saved_run in await db.list_runs(s.db_path, task_id):
            for page in _parse_stages_json(saved_run.get("stages_json")).get("orientation", {}).get("pages", []):
                if page.get("confirmed"):
                    directions[page["sha256"]] = page["rotation"]
        images = []
        for asset in review_assets:
            url = asset.get("data_url")
            if not url:
                continue
            raw = base64.b64decode(url.split(",", 1)[1])
            angle = directions.get(hashlib.sha256(raw).hexdigest())
            if angle is not None:
                prepared, mime, _ = await asyncio.to_thread(
                    image_prep.prepare_extract_image, raw, asset.get("mime", "image/jpeg"),
                    confirmed_rotation=angle)
                url = f"data:{mime};base64,{base64.b64encode(prepared).decode()}"
            images.append(url)
        coverage = "reread" if images else "transcript_only"

        timeout = min(h.review_timeout_seconds, remaining)
        log.info("开始复查 task_id=%s run_no=%d 送审=%d 超限=%d timeout=%.0fs coverage=%s images=%d",
                 task_id, run_no, len(targets), len(overflow), timeout, coverage, len(images))
        try:
            messages = hermes.build_review_messages(s, task, run, targets,
                                                    images or None)
            payload = await asyncio.wait_for(
                self.client.review_questions(
                    messages, f"review-{task_id}-{run_no}", timeout=timeout,
                    coverage=coverage),
                timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 复查失败不影响首轮成果
            log.warning("复查调用失败 task_id=%s: %s", task_id, e)
            return review.apply_failed(
                result, targets, overflow, f"复查调用失败：{e}")

        # usage 累计：两次真实调用的消耗如实入账（远端没返回的不编造）
        usage = dict(first_payload.get("usage") or {})
        for key in ("prompt_tokens", "completion_tokens"):
            usage[key] = (int(usage.get(key, 0) or 0)
                          + int((payload.get("usage") or {}).get(key, 0) or 0))
        first_payload["usage"] = usage

        meta = {
            "model_requested": payload.get("model_requested", ""),
            "model_reported": payload.get("reported_model", ""),
            "model_identity": "",
            "coverage": coverage,
        }
        identity, identity_note = review.check_model_identity(
            payload, first_payload,
            h.review_expected_model, h.review_expected_provider)
        meta["model_identity"] = identity
        if identity not in review.IDENTITY_ACCEPTED:
            log.warning("复查模型身份%s task_id=%s: %s", identity, task_id, identity_note)
            label = "未确认" if identity == review.IDENTITY_UNKNOWN else "不符"
            return review.apply_failed(
                result, targets, overflow,
                f"复查模型身份{label}：{identity_note}", meta)
        if identity == review.IDENTITY_MODEL_ONLY:
            # 网关不回 provider：模型名已核对，采纳本次复查，但如实记录核验范围
            log.warning("复查模型身份仅核对到模型名 task_id=%s: %s", task_id, identity_note)

        reviews_by_id, problems = review.reconcile_reviews(
            targets, payload.get("reviews") or [])
        if problems:
            log.warning("复查输出对账失败 task_id=%s: %s", task_id, "；".join(problems))
            return review.apply_failed(
                result, targets, overflow,
                "复查输出未通过覆盖对账：" + "；".join(problems), meta)

        merged = review.apply_review_result(result, targets, reviews_by_id, overflow, meta)
        if review.detect_image_link_failure(reviews_by_id, meta.get("coverage", "")):
            log.warning("复查图片链路故障 task_id=%s：已附 %d 张原图但复查方全部"
                        "以'未能直接读取'为由 unverified，图片未送达模型；"
                        "请排查 Hermes 网关图片透传或更换支持视觉的复查模型/路由",
                        task_id, len(images))
        try:
            hermes.validate_result(merged)
        except hermes.HermesResultInvalid as e:
            # 防御式回退：合并结果不合法时退回规范化基线，不把非法数据写库
            log.warning("复查合并结果未通过校验，回退基线 task_id=%s: %s", task_id, e)
            fallback = review.apply_failed(
                result, targets, overflow,
                f"复查合并结果未通过协议校验：{e}", meta)
            hermes.validate_result(fallback)
            return fallback
        summary = merged.get("review_summary") or {}
        log.info("复查完成 task_id=%s run_no=%d state=%s disagreed=%d unverified=%d",
                 task_id, run_no, summary.get("state"),
                 summary.get("disagreed", 0), summary.get("unverified", 0))
        return merged

    async def _finish(self, task: Dict[str, Any], run: Dict[str, Any],
                      payload: Dict[str, Any], result: Dict[str, Any], output_dir,
                      prev_result: Optional[Dict[str, Any]] = None) -> None:
        s = self.settings
        task_id = task["id"]

        # 服务端二次复查附记随本轮归档一次写入：与结果 JSON 同源（同一份 result），
        # 归档正文与结构化字段不会各说各话；没有合法归档时沿用既有跳过语义。
        review_md = review.build_review_markdown(result)
        if review_md:
            arch = result.setdefault("archive", {})
            md = (arch.get("content_markdown") or "").strip()
            arch["content_markdown"] = f"{md}\n\n{review_md}" if md else review_md

        # 数学题示意图：结果定稿时按题干 AI 重绘 SVG（不依赖台账，
        # 未记入台账的题结果页也要能看）。科目为空时按题干几何关键词判断。
        # 注意：不依赖 uid（fill_question_uids 可能填的是 question_uid）。
        subject = result.get("subject") or task.get("subject") or ""
        qs = [q for q in (result.get("questions") or [])
              if not q.get("diagram_svg") and q.get("stem")
              and diagram.should_attempt_diagram(subject, q.get("stem", ""))]
        if qs:
            try:
                chain = provider_chain(s)
                if not chain:
                    log.warning("task_id=%s 示意图跳过：无可用模型（provider_chain 为空）", task_id)
                else:
                    provs = [make_provider(name, s.llm.providers[name]) for name in chain]
                    svgs = await asyncio.gather(
                        *(diagram.generate_diagram_svg(q.get("stem", ""), provs)
                          for q in qs),
                        return_exceptions=True)
                    n = 0
                    for q, r in zip(qs, svgs):
                        if isinstance(r, str) and r:
                            q["diagram_svg"] = r
                            n += 1
                    if n:
                        log.info("task_id=%s 示意图已生成 %d 张", task_id, n)
                    else:
                        log.warning("task_id=%s 示意图生成 0 张（模型返回空或清洗失败）", task_id)
            except Exception as e:
                log.warning("task_id=%s 示意图生成跳过：%s", task_id, e)

        archive = await workspace.apply_archive(s, task, run, result)

        # 归档成功后执行受控 Git 同步（仅提交本次授权文件）；未启用或失败都如实记录
        git_result: Optional[Dict[str, Any]] = None
        if s.git_sync_enabled:
            git_result = await git_sync.sync_workspace(
                s, task=task, run=run, archive=archive, result=result)
            await db.log_git_sync(s.db_path, {**git_result, "task_id": task_id})
        result["delivery"] = workspace.summarize_delivery(s, result, archive, git_result)

        # 台账：错题与存疑题按去重键写入/更新，复测事件只追加不改写历史
        ledger_count = await _write_ledger(s, task, result, archive, prev_result)

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
            provider=payload.get("provider", "hermes"), model=payload.get("model", ""),
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


def _parse_stages_json(raw: Any) -> Dict[str, Any]:
    """解析 task_runs.stages_json；空或非法时返回 {}（不抛错）。"""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


async def build_task_view(settings: Settings, task: Dict[str, Any]) -> Dict[str, Any]:
    """统一任务视图：新旧结果都能读，状态与缺口如实呈现。"""
    runs = await db.list_runs(settings.db_path, task["id"])
    artifacts = await db.list_artifacts(settings.db_path, task["id"])
    result = normalize_result(task.get("result_json"))

    # 示意图：老任务结果里没有 diagram_svg 时，前端显示"生成示意图"按钮，
    # 用户点击后调 POST /tasks/{id}/diagrams 生成并存回（不阻塞页面加载）。
    ledger = await db.list_ledger_by_task(settings.db_path, task["openid"], task["id"])
    return {
        "id": task["id"],
        "status": task["status"],
        "orientation": (_parse_stages_json(runs[-1].get("stages_json")).get("orientation")
                        if runs and task["status"] == "waiting_input"
                        and runs[-1].get("stage") == "orientation" else None),
        "task_type": task.get("task_type", "grading"),
        # 学科未指定时用模型按材料判断出的学科展示，避免页面出现空学科
        "subject": task.get("subject") or (result or {}).get("subject", "") or "",
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
                "diagram_svg": row.get("diagram_svg", ""),
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
                # 分阶段进度与各阶段产出（含 extract 转写：AI 读到的题目与学生答案）
                "stage": r.get("stage", ""),
                "stages": _parse_stages_json(r.get("stages_json")),
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


def revision_coverage_gaps(prev_result: Optional[Dict[str, Any]],
                           result: Dict[str, Any]) -> List[str]:
    """补充轮次 uid 覆盖检查：返回上一轮有、本轮缺失的题目 uid 列表。

    修订模式要求模型原样保留上一轮所有题目的 uid；缺失说明模型把补充材料
    当成了新作业从头批阅，这时绝不能用新结果覆盖旧结果。
    """
    if not prev_result:
        return []
    prev_uids = [str(q.get("uid") or "").strip()
                 for q in (prev_result.get("questions") or [])]
    prev_uids = [u for u in prev_uids if u]
    if not prev_uids:
        return []
    new_uids = {str(q.get("uid") or "").strip()
                for q in (result.get("questions") or [])}
    return [u for u in prev_uids if u not in new_uids]


async def _record_revision_corrections(settings: Settings, task: Dict[str, Any],
                                       result: Dict[str, Any],
                                       prev_result: Dict[str, Any],
                                       archive_rel: str) -> None:
    """修订边界：上一轮错题在本轮被订正为对，台账记一条订正事件并更新状态。

    只追加事件、不删历史；状态流转到 corrected_pending_retest（已订正待复测），
    避免旧的「待订正」条目变成僵尸数据。
    """
    openid = task["openid"]
    subject = result.get("subject") or task.get("subject") or ""
    prev_by_uid = {str(q.get("uid") or "").strip(): q
                   for q in (prev_result.get("questions") or [])}
    new_by_uid = {str(q.get("uid") or "").strip(): q
                  for q in (result.get("questions") or [])}
    today = time.strftime("%Y-%m-%d")
    for uid, prev_q in prev_by_uid.items():
        if not uid or prev_q.get("status") not in LEDGER_STATUSES:
            continue
        new_q = new_by_uid.get(uid)
        if not new_q:
            continue  # 缺题已被覆盖校验拦截，正常走不到这里
        corrected = (new_q.get("status") == "correct"
                     or (new_q.get("final_decision") or "") == "corrected_to_correct")
        if not corrected:
            continue
        entry = await db.get_ledger_by_uid(settings.db_path, openid, uid)
        if not entry:
            continue
        await db.add_question_event(settings.db_path, openid, {
            "question_uid": uid,
            "subject": subject,
            "event_type": "correction",
            "result": "corrected",
            "occurred_date": today,
            "student_answer": new_q.get("student_answer", ""),
            "note": "补充材料后订正为对",
            "source_task_id": task["id"],
            "archive_path": archive_rel,
        })
        state = _ledger_state_for_event("corrected")
        if state:
            await db.update_ledger_state(
                settings.db_path, openid, entry["id"],
                remediation_state=state, archive_path=archive_rel)



async def _write_ledger(settings: Settings, task: Dict[str, Any], result: Dict[str, Any],
                        archive: Dict[str, Any],
                        prev_result: Optional[Dict[str, Any]] = None) -> int:
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
            "diagram_svg": question.get("diagram_svg", ""),
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

    if prev_result:
        # 修订边界：上一轮错题在本轮被订正为对，记订正事件并更新台账状态
        await _record_revision_corrections(settings, task, result, prev_result, archive_rel)

    return written
