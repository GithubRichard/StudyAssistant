"""启动入口：uvicorn app.main:app

启动时依次完成：
1. SQLite 迁移（有旧数据先备份）
2. 授权学习工作区初始化（已存在的记录一律不动）
3. Hermes 客户端与持久化任务执行器启动
4. 已派发但未结束的任务标记 interrupted，不自动重放
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

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
        log.info("学习工作区: %s（新建目录 %d 个, README 新建=%s）",
                 ws["root"], len(ws["created"]), ws["readme_created"])

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
        try:
            yield
        finally:
            await runner.stop()
            await client.aclose()
            api.hermes_client = None

    app = FastAPI(title="Leo 学习任务服务", version="0.2.0", lifespan=lifespan)
    app.include_router(api.router)

    @app.get("/healthz")
    async def healthz():
        """存活探针：只说明进程可响应，不代表 Hermes 或技能就绪。"""
        return {"ok": True}

    return app
