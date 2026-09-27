"""启动入口：uvicorn app.main:app

启动时依次完成：
1. SQLite 迁移（有旧数据先备份）
2. 授权学习工作区初始化（已存在的记录一律不动）
3. Hermes 客户端与持久化任务执行器启动
4. 已派发但未结束的任务标记 interrupted，不自动重放
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import api, db, workspace
from .config import Settings, load_settings
from .hermes import HermesClient
from .tasks import TaskRunner

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    api.settings = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migration = await db.init_db(settings.db_path)
        if migration.get("backup"):
            log.warning("数据库已备份到 %s", migration["backup"])

        ws = workspace.ensure_workspace(settings)
        log.info("学习工作区: %s（学科 %s；新建 %d 项；README 新建=%s；.gitignore 新建=%s）",
                 ws["root"], "、".join(ws.get("subjects") or []), len(ws["created"]),
                 ws["readme_created"], ws.get("gitignore_created"))

        client = HermesClient(settings.hermes)
        api.hermes_client = client
        readiness = await client.readiness()
        if readiness.configured and readiness.reachable and readiness.skill_installed:
            log.info("Hermes 就绪（技能 %s 已安装）", settings.hermes.skill_name)
        else:
            log.warning("Hermes 未就绪: %s", readiness.detail or readiness.as_dict()["state"])

        runner = TaskRunner(settings, client)
        await runner.start()
        app.state.runner = runner
        # 网页版 v1：每日自动清理超期错题（保留两年），仅清理 mistakes 与关联事件，
        # 任务、附件、Git 归档不受影响。
        purge_task = asyncio.create_task(_retention_purge_loop(settings))
        try:
            yield
        finally:
            purge_task.cancel()
            await runner.stop()
            await client.aclose()
            api.hermes_client = None



    app = FastAPI(title="Leo 学习任务服务", version="0.2.0", lifespan=lifespan)
    app.include_router(api.router)

    @app.get("/healthz")
    async def healthz():
        """存活探针：只说明进程可响应，不代表 Hermes 或技能就绪。"""
        return {"ok": True}

    # 跨域：仅在明确配置 allowed_origins 时开启（前后端分离部署用；同源部署留空更安全）
    if settings.web.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.web.allowed_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # 网页版：与接口同源，浏览器直接访问 http://<IP>:<port>/ 即可
    web_dir = Path(settings.web_dir)
    if not web_dir.is_absolute():
        web_dir = Path(__file__).resolve().parent.parent / web_dir

    @app.get("/")
    async def index_web():
        if not web_dir.is_dir():
            return {"detail": "网页版静态资源目录不存在，请确认已包含 web/ 目录"}
        return RedirectResponse(url="/web/")

    if web_dir.is_dir():
        app.mount("/web", StaticFiles(directory=str(web_dir), html=True), name="web")
        log.info("网页版入口: /web/ （目录 %s）", web_dir)
    else:
        log.warning("未找到网页版静态资源目录 %s，网页版暂不可用", web_dir)

    return app


async def _retention_purge_loop(settings: "Settings") -> None:
    """每日一次：按账号清理超期错题台账。失败只记录日志，不影响服务。"""
    while True:
        try:
            await asyncio.sleep(24 * 3600)
            openids = await db.list_user_openids(settings.db_path)
            for openid in openids:
                removed = await db.purge_expired(
                    settings.db_path, openid, settings.retention_days)
                if removed["mistakes"]:
                    log.info("自动清理超期错题: %s 删除 %d 条，关联事件 %d 条",
                             openid, removed["mistakes"], removed["question_events"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("自动清理超期错题失败")
