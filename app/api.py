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
from pathlib import Path
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, Header, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import FileResponse

from . import auth, db, tasks, wechat, workspace
from .config import Settings, provider_chain
from .hermes import HermesClient
from .schemas import FollowupCreate, StudyTaskCreate
from .tasks import TaskError

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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
async def web_meta():
    """网页登录页所需的公开信息（不含任何密钥）。"""
    s = get_settings()
    return {
        "title": s.web.title,
        "enabled": s.web.enabled,
        "password_required": True,
        "configured": s.web.configured,
    }


@router.post("/web/login")
async def web_login(request: Request, password: str = Form(""), user: str = Form("")):
    """网页版登录：配置密码换取会话令牌（与小程序共用同一会话体系）。"""
    s = get_settings()
    if not s.web.enabled:
        raise HTTPException(403, "网页版已被管理员关闭")
    if not s.web.password:
        raise HTTPException(
            403, "服务端未配置网页访问密码（.env 中的 WEB_PASSWORD），网页版不可用")

    ip = _client_ip(request)
    locked = _web_locked(ip)
    if locked:
        raise HTTPException(429, f"尝试次数过多，请 {locked} 秒后重试")

    if not auth.verify_password(password, s.web.password):
        _web_record_failure(ip)
        raise HTTPException(401, "访问密码不正确")

    _web_failures.pop(ip, None)
    openid = auth.web_openid(user or s.web.user)
    try:
        # 密码已是准入凭证，网页身份不再受微信 openid 白名单约束
        session = await auth.issue_session(
            s.db_path, openid, None,
            ttl_seconds=s.auth.session_ttl_days * 24 * 3600)
    except auth.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e

    await db.get_or_create_user(s.db_path, openid, s.quota.new_user_bonus)
    log.info("网页版登录成功: %s (来源 %s)", openid, ip)
    return {
        "token": session["token"],
        "openid": openid,
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


@router.get("/tasks")
async def list_tasks(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                     ctx: dict = Session):
    s = get_settings()
    rows = await db.list_tasks(s.db_path, ctx["openid"], limit, offset)
    out = []
    for t in rows:
        item = {
            "id": t["id"], "status": t["status"], "subject": t.get("subject", ""),
            "task_type": t.get("task_type", "grading"),
            "created_at": t.get("created_at"), "run_count": t.get("run_count", 0),
            "summary": "", "missing_info_count": 0,
        }
        if t.get("result_json"):
            try:
                data = json.loads(t["result_json"])
                item["summary"] = (data.get("overview") or {}).get("summary", "") or ""
                item["missing_info_count"] = len(data.get("missing_info") or [])
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
            "git": s.delivery.git_enabled,
        },
        "workspace": {"dir": s.workspace.dir, "readonly": s.workspace.readonly},
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
