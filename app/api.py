"""REST 接口：学习任务、附件、结果与运行状态。

设计要点：
- 所有业务接口以会话令牌鉴权（Depends(require_session)），并要求资源归属一致；
  不再相信客户端自称的 openid。
- 创建任务走持久化队列（幂等 + 配额预留），由执行器认领执行。
- 结果统一通过 tasks.build_task_view 输出，新旧结果都可读。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, Header, HTTPException, Query,
                     Request, Response, UploadFile)
from fastapi.responses import FileResponse

from . import auth, db, tasks, wechat, weekly, workspace, orientation_tasks, version, diagram
from .config import Settings, provider_chain, web_asset_version
from .hermes import HermesClient
from .providers import make_provider
from .schemas import (FamilySettingsUpdate, FollowupCreate, LedgerEventCreate,
                      ManualLedgerCreate, StudyTaskCreate, OrientationConfirm)
from .tasks import TaskError

log = logging.getLogger(__name__)
# 格式带 logger 名（如 app.hermes），docker 日志里才能一眼认出 Hermes 相关行。
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

router = APIRouter(prefix="/api")

# 由 main.create_app() 注入
settings: Optional[Settings] = None
hermes_client: Optional[HermesClient] = None


def get_settings() -> Settings:
    assert settings is not None, "settings 未初始化"
    return settings


def get_hermes() -> Optional[HermesClient]:
    return hermes_client


async def require_session(
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """统一鉴权依赖：返回会话信息（含 openid）。"""
    s = get_settings()
    token = auth.extract_bearer(authorization)
    try:
        return await auth.authenticate(s.db_path, token)
    except auth.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e


Session = Depends(require_session)


# ---------- 登录 ----------

@router.post("/login")
async def login(code: str = Form(...)):
    """小程序 wx.login() 的 code 换会话令牌。

    已配置微信 appid/secret 时登录失败必须拒绝，不得降级为开发身份。
    """
    s = get_settings()
    configured = bool(s.wechat.appid and s.wechat.secret)
    openid, err = await wechat.code2session_result(code, s.wechat)

    if not openid:
        if configured:
            log.warning("微信登录失败: %s", err)
            raise HTTPException(401, f"微信登录失败：{err or '未知错误'}")
        openid = f"dev_{code[:16]}"
        log.warning("未配置微信，使用开发身份 openid=%s", openid)

    try:
        session = await auth.issue_session(
            s.db_path, openid, s.auth.allowed_openids,
            ttl_seconds=s.auth.session_ttl_days * 24 * 3600)
    except auth.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e

    await db.get_or_create_user(s.db_path, openid, s.quota.new_user_bonus)
    # 首次登录就把该账号的工作区骨架建好（幂等；已存在的目录与文件不动）
    workspace.ensure_workspace(s, openids=[openid])
    return {
        "token": session["token"],
        "openid": openid,
        "expires_at": session["expires_at"],
        "dev_identity": not configured,
    }


@router.post("/logout")
async def logout(ctx: dict = Session):
    s = get_settings()
    await db.delete_session(s.db_path, ctx["session_id"])
    return {"ok": True}


# ---------- 网页版（IP 直连，不走微信）----------

# 简易防爆破：同一来源连续输错达上限后短暂锁定，避免 IP 直连被暴力试密码
_WEB_MAX_FAILURES = 10
_WEB_LOCK_SECONDS = 300
_web_failures: dict = {}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _web_locked(ip: str) -> int:
    """返回剩余锁定秒数；0 表示未锁定。"""
    count, until = _web_failures.get(ip, (0, 0.0))
    remain = until - time.time()
    return int(remain) if remain > 0 else 0


def _web_record_failure(ip: str) -> None:
    count, _ = _web_failures.get(ip, (0, 0.0))
    count += 1
    until = time.time() + _WEB_LOCK_SECONDS if count >= _WEB_MAX_FAILURES else 0.0
    _web_failures[ip] = (count, until)
    if until:
        log.warning("网页登录连续失败 %d 次，来源 %s 已临时锁定", count, ip)


@router.get("/web/meta")
async def web_meta(response: Response):
    """网页登录页所需的公开信息（不含任何密钥与账号名单）。

    `asset_version` 是当前前端资源的版本号：前端的版本探针用它发现
    "服务端已换新前端、本标签页还在跑旧脚本"（单页应用不整页刷新时
    不会重新下载 app.js）。因此这个响应本身必须禁止缓存。

    `git_version` 是服务端代码的 git 短哈希（构建时烘入，见 Dockerfile 的
    GIT_VERSION），界面顶栏与"我的"页展示，方便核对线上跑的是哪版代码。
    """
    s = get_settings()
    response.headers["Cache-Control"] = "no-store"
    return {
        "title": s.web.title,
        "enabled": s.web.enabled,
        "user_required": True,
        "password_required": True,
        "configured": s.web.configured,
        "asset_version": web_asset_version(s),
        "git_version": version.git_version(),
    }


@router.post("/web/login")
async def web_login(request: Request, username: str = Form(""), password: str = Form("")):
    """网页版登录：预设账号（用户名+密码）换取会话令牌。

    用户名必须在 web.users 预设名单中；数据按 web:<username> 隔离，
    与小程序共用同一会话体系。
    """
    s = get_settings()
    if not s.web.enabled:
        raise HTTPException(403, "网页版已被管理员关闭")
    if not s.web.configured:
        raise HTTPException(
            403, "服务端尚未配置网页账号（config.yaml 中的 web.users），网页版不可用")

    ip = _client_ip(request)
    locked = _web_locked(ip)
    if locked:
        raise HTTPException(429, f"尝试次数过多，请 {locked} 秒后重试")

    account = s.web.find_user(username)
    if not account or not auth.verify_password_hash(password, account.password_hash):
        _web_record_failure(ip)
        # 不区分"用户名不存在"与"密码错误"，避免枚举账号
        raise HTTPException(401, "用户名或密码不正确")

    _web_failures.pop(ip, None)
    openid = auth.web_openid(account.username)
    try:
        # 网页身份不再受微信 openid 白名单约束
        session = await auth.issue_session(
            s.db_path, openid, None,
            ttl_seconds=s.auth.session_ttl_days * 24 * 3600)
    except auth.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e

    await db.get_or_create_user(s.db_path, openid, s.quota.new_user_bonus)
    # 首次登录就把该账号的工作区骨架建好（幂等；已存在的目录与文件不动）
    workspace.ensure_workspace(s, openids=[openid])
    log.info("网页版登录成功: %s (来源 %s)", openid, ip)
    return {
        "token": session["token"],
        "openid": openid,
        "username": account.username,
        "display_name": account.display_name,
        "expires_at": session["expires_at"],
        "dev_identity": False,
    }


# ---------- 附件 ----------

@router.post("/assets", status_code=201)
async def upload_asset(file: UploadFile = File(...), ctx: dict = Session):
    s = get_settings()
    raw = await file.read()
    try:
        asset = workspace.store_asset(s, ctx["openid"], raw, file.filename or "")
    except workspace.WorkspaceError as e:
        raise HTTPException(400, str(e)) from e

    if not await wechat.img_sec_check(raw, s.wechat):
        raise HTTPException(400, "图片未通过内容安全检查")

    await db.create_asset(s.db_path, asset)
    return {"asset_id": asset["id"], "bytes": asset["bytes"],
            "width": asset["width"], "height": asset["height"]}


# ---------- 学习任务 ----------

@router.post("/study/tasks", status_code=201)
async def create_study_task(payload: StudyTaskCreate,
                            idempotency_key: Optional[str] = Header(
                                default=None, alias="Idempotency-Key"),
                            ctx: dict = Session):
    s = get_settings()
    try:
        return await tasks.create_study_task(
            s, ctx["openid"], payload.model_dump(), idempotency_key or "")
    except TaskError as e:
        raise HTTPException(e.status_code, e.message) from e


@router.post("/tasks/{task_id}/followups", status_code=201)
async def create_followup(task_id: str, payload: FollowupCreate, ctx: dict = Session):
    s = get_settings()
    try:
        return await tasks.add_followup(s, ctx["openid"], task_id, payload.model_dump())
    except TaskError as e:
        raise HTTPException(e.status_code, e.message) from e


@router.post("/tasks", status_code=201)
async def create_task_compat(file: UploadFile = File(...),
                             subject: str = Form("数学"),
                             grade_level: str = Form(""),
                             openid: str = Form(""),  # 兼容旧字段，一律忽略
                             ctx: dict = Session):
    """旧版单图入口：包装成同一学习任务流程，同样强制鉴权。"""
    s = get_settings()
    raw = await file.read()
    try:
        asset = workspace.store_asset(s, ctx["openid"], raw, file.filename or "")
    except workspace.WorkspaceError as e:
        raise HTTPException(400, str(e)) from e
    await db.create_asset(s.db_path, asset)
    try:
        created = await tasks.create_study_task(s, ctx["openid"], {
            "task_type": "grading", "subject": subject, "grade_level": grade_level,
            "text": "", "asset_ids": [asset["id"]],
        }, "")
    except TaskError as e:
        raise HTTPException(e.status_code, e.message) from e
    return {"task_id": created["task_id"], "status": created["status"]}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, ctx: dict = Session):
    s = get_settings()
    task = await db.get_task(s.db_path, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    try:
        auth.ensure_owner(task["openid"], ctx)
    except auth.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    return await tasks.build_task_view(s, task)


@router.post("/tasks/{task_id}/diagrams")
async def generate_task_diagrams(task_id: str, ctx: dict = Session):
    """手动为任务的数学题生成示意图（老任务补生成），结果存回 task.result_json。"""
    import asyncio as _asyncio
    import json as _json
    from . import diagram as _diagram
    from .providers import make_provider as _make_provider
    from .config import provider_chain as _chain

    s = get_settings()
    task = await db.get_task(s.db_path, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    try:
        auth.ensure_owner(task["openid"], ctx)
    except auth.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e

    from .tasks import normalize_result
    result = normalize_result(task.get("result_json"))
    if not result:
        raise HTTPException(400, "任务无可用结果")
    subj = task.get("subject") or result.get("subject", "") or ""
    questions = result.get("questions") or []
    debug = []
    missing = []
    for q in questions:
        has_svg = bool(q.get("diagram_svg"))
        has_stem = bool(q.get("stem"))
        should = _diagram.should_attempt_diagram(subj, q.get("stem", ""))
        debug.append({
            "no": q.get("no", "?"),
            "has_diagram": has_svg,
            "has_stem": has_stem,
            "should_attempt": should,
        })
        if not has_svg and has_stem and should:
            missing.append(q)
    if not missing:
        return {"generated": 0, "message": "无需生成（已有示意图或无几何题）",
                "debug": {"subject": subj, "questions": debug}}
    chain = _chain(s)
    if not chain:
        raise HTTPException(500, "无可用模型")
    prov = _make_provider(chain[0], s.llm.providers[chain[0]])
    svgs = await _asyncio.gather(
        *(_diagram.generate_diagram_svg(q["stem"], prov) for q in missing),
        return_exceptions=True)
    n = 0
    failures = []
    for q, r in zip(missing, svgs):
        if isinstance(r, str) and r:
            q["diagram_svg"] = r
            n += 1
        elif isinstance(r, Exception):
            failures.append(f"{q.get('no', '?')}: {type(r).__name__}: {r}")
    if n:
        await db.update_task(s.db_path, task_id,
                             result_json=_json.dumps(result, ensure_ascii=False))
    return {"generated": n, "total": len(missing), "failures": failures[:5]}


@router.get("/tasks/{task_id}/orientation/{page}")
async def orientation_preview(task_id: str, page: int, run_id: str, ctx: dict = Session):
    try:
        return await orientation_tasks.preview(get_settings(), ctx["openid"], task_id, run_id, page)
    except TaskError as e:
        raise HTTPException(e.status_code, e.message) from e
    except OSError as e:
        raise HTTPException(409, "原图片无法读取，请重新提交") from e


@router.post("/tasks/{task_id}/orientation")
async def confirm_orientation(task_id: str, payload: OrientationConfirm, ctx: dict = Session):
    try:
        return await orientation_tasks.confirm(get_settings(), ctx["openid"], task_id, payload)
    except TaskError as e:
        raise HTTPException(e.status_code, e.message) from e
    except OSError as e:
        raise HTTPException(409, "原图片无法读取，请重新提交") from e


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, ctx: dict = Session):
    """删除任务：允许删除失败/中断的任务，以及方向待确认的任务。

    方向待确认（最新轮次 stage='orientation'）的任务尚未产出任何批改结果与
    台账数据，删除是安全的——否则用户放弃确认时任务会永久残留。
    补充材料（missing_info）的 waiting_input 有批改结果，仍不允许删除。
    成功任务关联错题台账，误删会丢数据，不允许删除。

    连带删除轮次、附件关联、错题、事件、成果索引、归档日志；
    已无人引用的附件图片文件一并删除。
    """
    s = get_settings()
    task = await db.get_task(s.db_path, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    try:
        auth.ensure_owner(task["openid"], ctx)
    except auth.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    deletable = task["status"] in ("failed", "interrupted")
    if not deletable and task["status"] == "waiting_input":
        runs = await db.list_runs(s.db_path, task_id)
        deletable = bool(runs) and runs[-1].get("stage") == "orientation"
    if not deletable:
        raise HTTPException(400, "只有失败/中断/待确认方向的任务可以删除")
    orphan_paths = await db.delete_task(s.db_path, task_id)

    # 兼容老任务的 image_path：没有其它任务引用才删文件
    legacy = (task.get("image_path") or "").strip()
    if legacy and not any(p == legacy for p in orphan_paths):
        other = await db.list_tasks(s.db_path, task["openid"], limit=100)
        if not any(t["id"] != task_id and (t.get("image_path") or "") == legacy
                   for t in other):
            orphan_paths.append(legacy)

    data_dir = Path(s.data_dir).resolve()
    removed_files = 0
    for p in orphan_paths:
        try:
            fp = Path(p)
            if not fp.is_absolute():
                fp = data_dir / fp
            fp = fp.resolve()
            # 保险：只删数据目录内的文件
            if data_dir not in fp.parents and fp != data_dir:
                log.warning("删除任务：跳过数据目录外的文件 %s", p)
                continue
            if fp.is_file():
                fp.unlink()
                removed_files += 1
        except OSError as e:
            log.warning("删除任务：文件删除失败 %s: %s", p, e)
    log.info("任务已删除: task_id=%s openid=%s 文件=%d", task_id, task["openid"],
             removed_files)
    return {"ok": True, "removed_files": removed_files}


@router.get("/tasks")
async def list_tasks(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                     ctx: dict = Session):
    s = get_settings()
    rows = await db.list_tasks(s.db_path, ctx["openid"], limit, offset)
    run_stages = await db.latest_run_stages(s.db_path, [t["id"] for t in rows])
    out = []
    for t in rows:
        orientation_pending = (t["status"] == "waiting_input"
                               and run_stages.get(t["id"]) == "orientation")
        item = {
            "id": t["id"], "status": t["status"], "subject": t.get("subject", ""),
            "task_type": t.get("task_type", "grading"),
            "training_kind": t.get("training_kind", ""),
            "scope_start": t.get("scope_start", ""),
            "scope_end": t.get("scope_end", ""),
            "git_status": t.get("git_status", ""),
            "archive_path": workspace.workspace_relative_path(s, t.get("archive_path", "")),
            "created_at": t.get("created_at"), "run_count": t.get("run_count", 0),
            "orientation_pending": orientation_pending,
            "summary": "", "missing_info_count": 0,
        }
        if t.get("result_json"):
            try:
                data = json.loads(t["result_json"])
                item["summary"] = (data.get("overview") or {}).get("summary", "") or ""
                item["missing_info_count"] = len(data.get("missing_info") or [])
                # 学科未指定时展示模型按材料判断出的学科
                if not item["subject"]:
                    item["subject"] = data.get("subject", "") or ""
            except (ValueError, TypeError):
                item["summary"] = ""
        out.append(item)
    return out


@router.get("/tasks/{task_id}/artifacts/{artifact_id}")
async def download_artifact(task_id: str, artifact_id: str, ctx: dict = Session):
    s = get_settings()
    task = await db.get_task(s.db_path, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    try:
        auth.ensure_owner(task["openid"], ctx)
    except auth.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e

    artifact = await db.get_artifact(s.db_path, task_id, artifact_id)
    if not artifact:
        raise HTTPException(404, "成果文件不存在")
    path = Path(artifact["path"])
    if not path.exists() or path.is_symlink() or not workspace.is_inside_allowed(s, str(path)):
        raise HTTPException(410, "成果文件已不可用")
    return FileResponse(str(path), filename=path.name)


# ---------- 配额与运行状态 ----------

@router.get("/quota")
async def quota(ctx: dict = Session):
    s = get_settings()
    return {"remaining": await db.quota_remaining(
        s.db_path, ctx["openid"], s.quota.daily_free)}


@router.get("/runtime")
async def runtime(ctx: dict = Session):
    """脱敏运行状态：区分「已配置」与「实际就绪」。"""
    s = get_settings()
    client = get_hermes()
    hermes_state = (await client.readiness()).as_dict() if client is not None else None
    root = workspace.workspace_root(s)
    account_root = workspace.account_dir(s, ctx["openid"])
    subjects = workspace.workspace_subjects(s)
    return {
        "engine": {
            "mode": s.engine.mode,
            "legacy_available": bool(provider_chain(s)),
            "agent_model": s.hermes.agent_model if s.is_hermes else "",
            "hermes_base_url_configured": bool(s.hermes.base_url),
        },
        "hermes": hermes_state,
        "delivery": {
            "pdf": s.delivery.pdf_enabled,
            "email": s.delivery.email_enabled,
            "git": s.git_sync_enabled,
        },
        "git": {
            "enabled": s.git_sync_enabled,
            "remote": s.git.remote,
            "last_sync": await db.latest_git_sync(s.db_path),
        },
        "family": {
            "default_grade_level": s.family.default_grade_level,
            "subjects": list(s.family.subjects),
            "term_start_date": s.family.term_start_date,
        },
        "workspace": {
            "dir": s.workspace.dir,
            "readonly": s.workspace.readonly,
            "subjects": subjects,
            "account": workspace.account_dir_name(ctx["openid"]),
            "readme_exists": (root / "README.md").exists(),
            "gitignore_exists": (root / ".gitignore").exists(),
            "original_dir_exists": any(
                (account_root / name / workspace.ORIGINAL_SUBDIR).is_dir()
                for name in subjects),
        },
        "limits": {
            "max_assets_per_task": s.limits.max_assets_per_task,
            "max_runs_per_task": s.limits.max_runs_per_task,
            "max_task_minutes": s.limits.max_task_minutes,
        },
    }


@router.get("/providers")
async def providers():
    """legacy 引擎的配置视图（不代表 Hermes 就绪）。"""
    s = get_settings()
    chain = provider_chain(s)
    return {
        "engine_mode": s.engine.mode,
        "note": "该接口只反映 legacy 直连模型的配置，不代表 Hermes 技能已就绪",
        "default": s.llm.default_provider,
        "chain": chain,
        "providers": [
            {"name": name, "model": cfg.model, "enabled": cfg.enabled,
             "has_key": bool(cfg.api_key), "in_chain": name in chain}
            for name, cfg in s.llm.providers.items()
        ],
    }


# ---------- 家庭设置 ----------

@router.get("/settings")
async def read_family_settings(ctx: dict = Session):
    """家庭设置视图：未保存过时回落配置文件默认值，并如实标注来源。"""
    s = get_settings()
    stored = await db.get_family_settings(s.db_path, ctx["openid"])
    if stored and (stored.get("subjects") or stored.get("term_start_date")):
        return {
            "subjects": list(stored.get("subjects") or []),
            "term_start_date": stored.get("term_start_date", ""),
            "source": "saved",
            "updated_at": stored.get("updated_at"),
        }
    return {
        "subjects": list(s.family.subjects),
        "term_start_date": s.family.term_start_date,
        "source": "config_default",
        "updated_at": None,
    }


@router.put("/settings")
async def update_family_settings(payload: FamilySettingsUpdate, ctx: dict = Session):
    """保存家庭设置。未填写学期起始日期时如实保留为空，不擅自假设开学日期。"""
    s = get_settings()
    saved = await db.save_family_settings(
        s.db_path, ctx["openid"],
        subjects=payload.subjects or list(s.family.subjects),
        term_start_date=payload.term_start_date,
    )
    return {
        "subjects": saved["subjects"],
        "term_start_date": saved["term_start_date"],
        "source": "saved",
        "updated_at": saved["updated_at"],
    }


@router.get("/web/me")
async def web_me(ctx: dict = Session):
    """当前登录用户信息（含是否为管理员）。"""
    s = get_settings()
    return {
        "openid": ctx.get("openid", ""),
        "is_admin": auth.is_admin(ctx.get("openid", ""), s.auth.admin_users),
    }


@router.get("/web/overview")
async def web_overview(ctx: dict = Session):
    """今日学习台聚合：待办计数、本周正确率（全学科）、高频错因 TOP3。

    口径说明：正确率按近 7 天已完成的批改任务逐题汇总；错因按近 30 天台账统计；
    首期不做分学科拆分。
    """
    s = get_settings()
    openid = ctx["openid"]
    now = time.time()
    day = 86400.0

    counts = await db.ledger_counts(s.db_path, openid)
    cur = await db.grading_stats(s.db_path, openid, now - 7 * day, now)
    prev = await db.grading_stats(s.db_path, openid, now - 14 * day, now - 7 * day)
    causes = await db.top_error_causes(s.db_path, openid, now - 30 * day, 3)

    def _rate(st: dict):
        if st["checked"] <= 0:
            return None
        return st["correct"] / st["checked"]

    cur_rate, prev_rate = _rate(cur), _rate(prev)
    accuracy = None
    if cur_rate is not None:
        accuracy = {
            "rate": round(cur_rate, 4),
            "checked": cur["checked"],
            "delta": round(cur_rate - prev_rate, 4) if prev_rate is not None else None,
        }

    return {
        "todos": {
            "pending_correction": int(counts.get("pending_correction", 0)),
            "pending_retest": int(counts.get("corrected_pending_retest", 0)),
            "retest_failed": int(counts.get("retest_failed", 0)),
        },
        "week_accuracy": accuracy,
        "top_causes": causes,
        "retention_days": s.retention_days,
    }


# ---------- 周总结 ----------

@router.get("/web/weekly-summaries/weeks")
async def weekly_summary_weeks(ctx: dict = Session,
                               limit: int = Query(12, ge=1, le=52)):
    """已生成周总结的周列表（周一日期，倒序）。"""
    s = get_settings()
    weeks = await db.list_weekly_weeks(s.db_path, ctx["openid"], limit)
    return {"weeks": weeks}


@router.get("/web/weekly-summaries")
async def weekly_summary_detail(ctx: dict = Session,
                                week: str = Query("", description="周一日期 YYYY-MM-DD，空=最近一周")):
    """某周的周总结：按科目列出统计。不传 week 时取最近已生成的一周。"""
    s = get_settings()
    openid = ctx["openid"]
    week_start = week.strip()
    if not week_start:
        weeks = await db.list_weekly_weeks(s.db_path, openid, 1)
        if not weeks:
            return {"week_start": "", "week_end": "", "subjects": []}
        week_start = weeks[0]
    try:
        ws = datetime.strptime(week_start, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "week 格式应为 YYYY-MM-DD（周一日期）")
    rows = await db.get_weekly_summaries(s.db_path, openid, week_start)
    week_end = (ws + timedelta(days=6)).isoformat()
    return {"week_start": week_start, "week_end": week_end, "subjects": rows}


@router.post("/web/weekly-summaries/generate")
async def weekly_summary_generate(payload: dict, ctx: dict = Session):
    """手动触发为当前账号生成某周的周总结（用于测试/补看）。

    body: {"week_start": "2026-09-21"}，空则取最近一个完整周。
    只生成当前登录账号的数据，不影响其他账号。
    """
    s = get_settings()
    week_start = str((payload or {}).get("week_start") or "").strip()
    try:
        ws = (datetime.strptime(week_start, "%Y-%m-%d").date()
              if week_start else weekly.last_complete_week_monday())
    except ValueError:
        raise HTTPException(400, "week_start 格式应为 YYYY-MM-DD（周一日期）")
    subjects = await weekly.generate_for_user(s.db_path, ctx["openid"], ws,
                                              settings=s)
    return {"week_start": ws.isoformat(), "subjects": subjects}


# ---------- 错题本 ----------

@router.post("/mistakes", status_code=201)
async def add_mistake(task_id: str = Form(...), question_no: str = Form(...),
                      knowledge_point: str = Form(""), note: str = Form(""),
                      ctx: dict = Session):
    s = get_settings()
    task = await db.get_task(s.db_path, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    try:
        auth.ensure_owner(task["openid"], ctx)
    except auth.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    mid = await db.save_mistake(s.db_path, ctx["openid"], task_id, question_no,
                               knowledge_point, note)
    return {"id": mid}


@router.get("/mistakes")
async def get_mistakes(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
                       ctx: dict = Session):
    s = get_settings()
    return await db.list_mistakes(s.db_path, ctx["openid"], limit, offset)


# ---------- 错题台账与复测登记（复习页）----------

LEDGER_STATES = ("pending_correction", "corrected_pending_retest",
                 "retest_passed", "retest_failed")
_LEDGER_STATE_BY_RESULT = {
    "retest_passed": "retest_passed",
    "retest_failed": "retest_failed",
    "corrected": "corrected_pending_retest",
    # 用户点"我觉得判错了"：记异议事件的同时把条目从台账撤回（不再计入待订正/待复测）
    "disputed": "withdrawn",
}


def _ledger_view(row: dict) -> dict:
    """台账条目对外视图：不复读服务器路径，也不暴露 openid。"""
    return {
        "id": row.get("id"),
        "question_uid": row.get("question_uid", ""),
        "subject": row.get("subject", ""),
        "source": row.get("source", ""),
        "page": row.get("page", ""),
        "no": row.get("question_no", ""),
        "stem": row.get("stem", ""),
        "student_answer": row.get("student_answer", ""),
        "correct_answer": row.get("correct_answer", ""),
        "error_rule": row.get("error_rule", ""),
        "knowledge_point": row.get("knowledge_point", ""),
        "status": row.get("status", ""),
        "remediation_state": row.get("remediation_state", "pending_correction"),
        "archive_path": row.get("archive_path", ""),
        "task_id": row.get("task_id", ""),
        "note": row.get("note", ""),
        "diagram_svg": row.get("diagram_svg", ""),
        "created_at": row.get("created_at"),
        "last_event_at": row.get("last_event_at") or row.get("created_at"),
    }


@router.get("/ledger")
async def get_ledger(subject: str = Query(""), states: str = Query(""),
                     limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                     ctx: dict = Session):
    """复习台账：按学科与订正状态筛选，并给出状态计数与已有学科清单。"""
    s = get_settings()
    state_list = [item.strip() for item in (states or "").split(",") if item.strip()]
    invalid = [item for item in state_list if item not in LEDGER_STATES]
    if invalid:
        raise HTTPException(400, f"states 非法: {', '.join(invalid)}")
    rows = await db.list_ledger(s.db_path, ctx["openid"], subject=subject,
                                states=state_list, limit=limit, offset=offset)
    return {
        "entries": [_ledger_view(r) for r in rows],
        "counts": await db.ledger_counts(s.db_path, ctx["openid"]),
        "subjects": await db.ledger_subjects(s.db_path, ctx["openid"]),
        "states": list(LEDGER_STATES),
    }


@router.post("/ledger/manual", status_code=201)
async def create_manual_ledger(payload: ManualLedgerCreate, ctx: dict = Session):
    """做题页人工登记：自己判错的题直接记入复习台账（待订正）。

    按 question_uid 去重：同一题重复登记不会产生重复条目。
    """
    s = get_settings()
    uid = payload.question_uid or f"manual:{payload.source_task_id or 'na'}:{abs(hash(payload.stem)) % 10**8}"
    entry = {
        "task_id": payload.source_task_id,
        "question_no": payload.question_no,
        "knowledge_point": payload.knowledge_point,
        "note": payload.note or "做题页人工登记",
        "subject": payload.subject,
        "source": "manual",
        "question_uid": uid,
        "stem": payload.stem,
        "student_answer": payload.student_answer,
        "correct_answer": payload.correct_answer,
        "error_rule": "",
        "status": "wrong",
        "remediation_state": "pending_correction",
    }
    saved = await db.upsert_ledger_question(s.db_path, ctx["openid"], entry)
    return {"entry_id": saved["id"], "created": saved["created"]}


@router.get("/ledger/{entry_id}")
async def get_ledger_entry(entry_id: int, ctx: dict = Session):
    s = get_settings()
    row = await db.get_ledger_entry(s.db_path, ctx["openid"], entry_id)
    if not row:
        raise HTTPException(404, "台账条目不存在")
    # 懒生成示意图：老错题（升级前写入）没有 diagram_svg，查看时补上
    if (not row.get("diagram_svg") and row.get("stem")
            and diagram.should_attempt_diagram(row.get("subject", ""), row.get("stem", ""))):
        try:
            chain = provider_chain(s)
            if chain and row.get("stem"):
                prov = make_provider(chain[0], s.llm.providers[chain[0]])
                svg = await diagram.generate_diagram_svg(row["stem"], prov)
                if svg:
                    await db.update_ledger_diagram(s.db_path, ctx["openid"], entry_id, svg)
                    row["diagram_svg"] = svg
        except Exception as e:
            log.warning("示意图懒生成失败 entry=%s：%s", entry_id, e)
    events = await db.list_question_events(s.db_path, ctx["openid"],
                                           row.get("question_uid", ""))
    return {"entry": _ledger_view(row), "events": events}


@router.post("/ledger/{entry_id}/events", status_code=201)
async def create_ledger_event(entry_id: int, payload: LedgerEventCreate, ctx: dict = Session):
    """登记一次真实发生的订正/复测：追加事件、更新台账状态并追加到关联归档文件。

    result=disputed（用户点"我觉得判错了"）时：追加异议事件，并把条目状态置为
    withdrawn，从复习台账默认视图撤回（不再计入待订正/待复测）。
    """
    s = get_settings()
    row = await db.get_ledger_entry(s.db_path, ctx["openid"], entry_id)
    if not row:
        raise HTTPException(404, "台账条目不存在")

    occurred = payload.occurred_date or time.strftime("%Y-%m-%d")
    event = {
        "question_uid": row.get("question_uid", ""),
        "subject": row.get("subject", ""),
        "event_type": "retest",
        "result": payload.result,
        "occurred_date": occurred,
        "student_answer": payload.student_answer,
        "note": payload.note,
        "archive_path": row.get("archive_path", ""),
    }
    event_id = await db.add_question_event(s.db_path, ctx["openid"], event)

    state = _LEDGER_STATE_BY_RESULT.get(payload.result, "")
    if state:
        await db.update_ledger_state(s.db_path, ctx["openid"], entry_id,
                                     remediation_state=state,
                                     archive_path=row.get("archive_path", ""))

    appended = await workspace.append_retest_note(s, row, event)
    return {
        "event_id": event_id,
        "result": payload.result,
        "occurred_date": occurred,
        "remediation_state": state or row.get("remediation_state", ""),
        "archive": {
            "status": appended.get("status", "skipped"),
            "path": workspace.workspace_relative_path(s, appended.get("path", "")),
            "note": appended.get("note", ""),
        },
    }
