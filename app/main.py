"""启动入口：uvicorn app.main:create_app --factory

`app.main` 只导出工厂函数 `create_app()`，没有模块级 `app`，
因此必须加 `--factory`（容器与 README 均使用该方式）。

启动时依次完成：
1. SQLite 迁移（有旧数据先备份）
2. 授权学习工作区初始化（已存在的记录一律不动）
3. Hermes 客户端与持久化任务执行器启动
4. 已派发但未结束的任务标记 interrupted，不自动重放
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import api, db, workspace
from .config import Settings, load_settings, resolve_web_dir
from .hermes import HermesClient
from .tasks import TaskRunner

log = logging.getLogger(__name__)


def _configure_logging() -> None:
    """应用级日志收尾配置（docker logs 可见）。

    现状：app.api 在 import 时已调 basicConfig，给 root 加了 stderr
    handler（INFO 级），所以「调用 Hermes 技能」这类 INFO 日志本来就进
    docker 日志。这里只做两件事：
    1. 日志级别改由环境变量 LOG_LEVEL 控制（默认 INFO），想看 DEBUG 时
       不用改代码；
    2. 兜底：如果 root 意外没有 handler（比如将来 basicConfig 被移走），
       补一个 stdout handler，避免日志被静默丢弃。
    Dockerfile 已设 PYTHONUNBUFFERED=1，stdout/stderr 都不缓冲。
    """
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(),
                    logging.INFO)
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", "%m-%d %H:%M:%S"))
        root.addHandler(handler)
    root.setLevel(level)


class NoCacheStaticFiles(StaticFiles):
    """网页版静态资源：强制每次回源校验，避免部署后浏览器仍跑旧脚本。

    只补 `Cache-Control`，Starlette 原有的 `ETag` / `Last-Modified` 全部保留：
    内容没变时浏览器仍走 304（只传响应头，不传正文），变了立刻拿到新文件。
    单页应用里点底部 tab 只改 hash、不会重新请求脚本，只靠缓存头不够，
    运行期的新版本探测由 `/api/web/meta` 的 `asset_version` 兜底。
    """

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        # 404/405 等错误响应不改缓存头，避免把错误页也标记成可校验
        if response.status_code < 400:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    _configure_logging()
    settings = settings or load_settings()
    api.settings = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migration = await db.init_db(settings.db_path)
        if migration.get("backup"):
            log.warning("数据库已备份到 %s", migration["backup"])

        # 账号目录骨架：既覆盖配置里的网页账号，也补齐数据库中出现过的微信身份
        ws = workspace.ensure_workspace(
            settings, openids=await db.list_user_openids(settings.db_path))
        log.info("学习工作区: %s（账号 %s；学科 %s；新建 %d 项；README 新建=%s；"
                 ".gitignore 新建=%s；升级=%s）",
                 ws["root"], "、".join(ws.get("accounts") or []) or "（空）",
                 "、".join(ws.get("subjects") or []), len(ws["created"]),
                 ws["readme_created"], ws.get("gitignore_created"),
                 ws.get("gitignore_upgraded"))

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
        # 每周日凌晨（上海时间 02:00 后）按账号×科目生成上一周的周总结；
        # generate_missing 自带补生成，服务器周日宕机重启后也能补上。
        weekly_task = asyncio.create_task(_weekly_summary_loop(settings))
        try:
            yield
        finally:
            purge_task.cancel()
            weekly_task.cancel()
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
    web_dir = resolve_web_dir(settings)

    @app.get("/")
    async def index_web():
        if not web_dir.is_dir():
            return {"detail": "网页版静态资源目录不存在，请确认已包含 web/ 目录"}
        return RedirectResponse(url="/web/")

    if web_dir.is_dir():
        app.mount("/web", NoCacheStaticFiles(directory=str(web_dir), html=True), name="web")
        log.info("网页版入口: /web/ （目录 %s）", web_dir)
    else:
        log.warning("未找到网页版静态资源目录 %s，网页版暂不可用", web_dir)

    return app


async def _weekly_summary_loop(settings: "Settings") -> None:
    """每周日凌晨生成周总结：按账号×科目汇总上一完整自然周（上海时区）。

    每小时检查一次；generate_missing 只生成完整周且有数据的周总结，
    失败只记录日志，不影响服务。
    """
    from . import weekly
    try:
        # 启动时先补一次：覆盖"服务器整个周日都宕机"的情况
        generated = await weekly.generate_missing(settings.db_path, settings=settings)
        for openid, n in generated.items():
            log.info("周总结补生成: %s 补了 %d 周", openid, n)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("周总结启动补生成失败")
    while True:
        try:
            await asyncio.sleep(3600)
            generated = await weekly.generate_missing(settings.db_path, settings=settings)
            for openid, n in generated.items():
                log.info("周总结已生成: %s 生成 %d 周", openid, n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("周总结定时生成失败")


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
