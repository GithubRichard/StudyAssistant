"""REST 接口 + 后台批改任务。给小程序前端用的全部 API 都在这里。"""
from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

from . import db, grading, wechat
from .config import Settings, provider_chain
from .providers import ProviderError

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

router = APIRouter(prefix="/api")

# 由 main.create_app() 注入；测试时可直接替换
settings: Settings | None = None
_sem: asyncio.Semaphore | None = None


def get_settings() -> Settings:
    assert settings is not None, "settings 未初始化"
    return settings


def get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(get_settings().grade_concurrency)
    return _sem


def process_image(raw: bytes) -> tuple[bytes, str]:
    """校验并压缩图片：最长边压到 max_image_px，转 JPEG。省 token = 省钱。"""
    s = get_settings()
    if len(raw) > s.max_image_mb * 1024 * 1024:
        raise HTTPException(413, f"图片超过 {s.max_image_mb}MB 上限")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "不是有效的图片文件")
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, s.max_image_px / max(w, h))
    if scale < 1:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


# ---------- 登录 ----------

@router.post("/login")
async def login(code: str = Form(...)):
    """小程序 wx.login() 拿到 code 后调这里换 openid。"""
    s = get_settings()
    openid = await wechat.code2session(code, s.wechat)
    if not openid:
        # 开发模式：没配微信 appid 时用 code 派生一个假 openid，方便联调
        openid = f"dev_{code[:16]}"
        log.info("微信未配置，走开发模式 openid=%s", openid)
    await db.get_or_create_user(s.db_path, openid, s.quota.new_user_bonus)
    return {"openid": openid}


# ---------- 批改任务 ----------

@router.post("/tasks", status_code=201)
async def create_task(
    background: BackgroundTasks,
    openid: str = Form(...),
    subject: str = Form("数学"),
    grade_level: str = Form("七年级"),
    file: UploadFile = File(...),
):
    s = get_settings()
    img_bytes, mime = process_image(await file.read())

    # 配额检查
    if await db.quota_remaining(s.db_path, openid, s.quota.daily_free) <= 0:
        raise HTTPException(429, "今日批改次数已用完，明天再来")
    # 费用熔断
    if await db.get_daily_cost(s.db_path) >= s.budget.daily_max_cny:
        raise HTTPException(503, "今日服务额度已用完，请明天再试")

    # 微信内容安全（默认关闭）
    if not await wechat.img_sec_check(img_bytes, s.wechat):
        raise HTTPException(400, "图片未通过内容安全检查")

    task_id = uuid.uuid4().hex[:16]
    Path(s.upload_dir).mkdir(parents=True, exist_ok=True)
    image_path = str(Path(s.upload_dir) / f"{task_id}.jpg")
    Path(image_path).write_bytes(img_bytes)

    now = time.time()
    await db.create_task(s.db_path, {
        "id": task_id, "openid": openid, "subject": subject,
        "grade_level": grade_level, "image_path": image_path, "created_at": now,
    })
    await db.consume_quota(s.db_path, openid)
    background.add_task(run_grading, task_id)
    log.info("创建批改任务 task_id=%s openid=%s", task_id, openid)
    return {"task_id": task_id, "status": "pending"}


async def run_grading(task_id: str) -> None:
    """后台批改 worker：按 provider 链调用，成功入库，失败标记。"""
    s = get_settings()
    async with get_sem():
        task = await db.get_task(s.db_path, task_id)
        if not task:
            return
        await db.update_task(s.db_path, task_id, status="grading")
        try:
            img_bytes = Path(task["image_path"]).read_bytes()
            result, provider, model, itok, otok, cost = await grading.grade_image(
                img_bytes, "image/jpeg", task["subject"], task["grade_level"], s)
            await db.update_task(
                s.db_path, task_id, status="done",
                result_json=result.model_dump_json(),
                provider=provider, model=model,
                input_tokens=itok, output_tokens=otok, cost_cny=cost)
            await db.add_daily_cost(s.db_path, cost)
            # TODO: 配好订阅消息模板后，取消下面这行的注释
            # await wechat.send_subscribe_message(task["openid"], task_id, result.summary, s.wechat)
            log.info("任务完成 task_id=%s provider=%s", task_id, provider)
        except ProviderError as e:
            await db.update_task(s.db_path, task_id, status="failed", error=str(e)[:500])
            log.error("任务失败 task_id=%s: %s", task_id, e)
        except Exception as e:  # noqa: BLE001
            await db.update_task(s.db_path, task_id, status="failed", error=f"内部错误: {e}"[:500])
            log.exception("任务异常 task_id=%s", task_id)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    s = get_settings()
    task = await db.get_task(s.db_path, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    out = {k: task[k] for k in (
        "id", "status", "subject", "grade_level", "provider", "model",
        "input_tokens", "output_tokens", "cost_cny", "error",
        "created_at", "updated_at")}
    out["result"] = None
    if task["status"] == "done" and task["result_json"]:
        import json
        out["result"] = json.loads(task["result_json"])
    return out


@router.get("/tasks")
async def list_tasks(openid: str, limit: int = 20):
    s = get_settings()
    tasks = await db.list_tasks(s.db_path, openid, min(limit, 100))
    return [{"id": t["id"], "status": t["status"], "subject": t["subject"],
             "created_at": t["created_at"]} for t in tasks]


# ---------- 配额 / 模型 ----------

@router.get("/quota")
async def quota(openid: str):
    s = get_settings()
    return {"remaining": await db.quota_remaining(s.db_path, openid, s.quota.daily_free)}


@router.get("/providers")
async def providers():
    """当前启用的模型列表（不含密钥，供确认配置用）。"""
    s = get_settings()
    chain = provider_chain(s)
    return {
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
async def add_mistake(
    openid: str = Form(...),
    task_id: str = Form(...),
    question_no: str = Form(...),
    knowledge_point: str = Form(""),
    note: str = Form(""),
):
    s = get_settings()
    mid = await db.save_mistake(s.db_path, openid, task_id, question_no, knowledge_point, note)
    return {"id": mid}


@router.get("/mistakes")
async def get_mistakes(openid: str, limit: int = 100):
    s = get_settings()
    return await db.list_mistakes(s.db_path, openid, min(limit, 200))
