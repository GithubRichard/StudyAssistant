"""方向确认辅助：所有读取与恢复均限定任务归属和当前轮次。"""
import asyncio
import base64
import hashlib
import json
from pathlib import Path

from . import db, image_prep, tasks, thinking


async def pending(settings, openid, task_id, run_id=None):
    task = await db.get_task(settings.db_path, task_id)
    if not task or task["openid"] != openid:
        raise tasks.TaskError("任务不存在", 404)
    runs = await db.list_runs(settings.db_path, task_id)
    run = runs[-1] if runs else None
    if (not run or task["status"] != "waiting_input" or run.get("stage") != "orientation"
            or (run_id and run["id"] != run_id)):
        raise tasks.TaskError("该任务当前不需要确认方向，请刷新页面", 409)
    stages = tasks._parse_stages_json(run.get("stages_json"))
    return run, stages


async def preview(settings, openid, task_id, run_id, page):
    run, stages = await pending(settings, openid, task_id, run_id)
    records = stages.get("orientation", {}).get("pages", [])
    record = next((p for p in records if p["page"] == page and not p["confirmed"]), None)
    assets = await db.list_task_assets(settings.db_path, task_id, run["id"])
    if not record or page < 1 or page > len(assets):
        raise tasks.TaskError("待确认页面不存在", 404)
    raw = await asyncio.to_thread(Path(assets[page - 1]["path"]).read_bytes)
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise tasks.TaskError("图片内容发生变化，请重新提交", 409)
    prepared, mime, _ = await asyncio.to_thread(
        image_prep.prepare_extract_image, raw, assets[page - 1]["mime"], 0, 1200,
        confirmed_rotation=0)
    return {"page": page, "preview": f"data:{mime};base64,{base64.b64encode(prepared).decode()}"}


async def confirm(settings, openid, task_id, payload):
    run, stages = await pending(settings, openid, task_id, payload.run_id)
    pages = stages.get("orientation", {}).get("pages", [])
    rotations = {p.page: p.rotation for p in payload.rotations}
    expected = {p["page"] for p in pages if not p["confirmed"]}
    if not expected or len(rotations) != len(payload.rotations) or set(rotations) != expected:
        raise tasks.TaskError("请确认所有待确认页面的方向，不能重复或添加其它页", 400)
    assets = await db.list_task_assets(settings.db_path, task_id, run["id"])
    for page in pages:
        if page["page"] > len(assets):
            raise tasks.TaskError("原图片缺失，请重新提交", 409)
        raw = await asyncio.to_thread(Path(assets[page["page"] - 1]["path"]).read_bytes)
        if hashlib.sha256(raw).hexdigest() != page["sha256"]:
            raise tasks.TaskError("图片内容发生变化，请重新提交", 409)
        if page["page"] in rotations:
            page.update(rotation=rotations[page["page"]], confirmed=True, source="manual")
    if not await db.resume_orientation(settings.db_path, task_id, openid, run["id"],
                                       run["stages_json"], json.dumps(stages, ensure_ascii=False)):
        raise tasks.TaskError("任务状态已变化，请刷新页面", 409)
    with thinking.task_context(task_id, run["run_no"]):
        thinking.log_event("orientation_confirmed", {"pages": pages})
    return {"task_id": task_id, "status": "pending", "run_id": run["id"]}
