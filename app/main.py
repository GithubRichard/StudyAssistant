"""启动入口：uvicorn app.main:app"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import api, db
from .config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    api.settings = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.init_db(settings.db_path)
        yield

    app = FastAPI(title="作业批改服务", version="0.1.0", lifespan=lifespan)
    app.include_router(api.router)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app


app = create_app()
