"""FastAPI 应用装配：生命周期内构建依赖，挂载静态前端。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from reviewhive.api.routes import build_router
from reviewhive.config import Settings, load_settings
from reviewhive.core.pipeline import HiveDeps, ReviewPipeline
from reviewhive.core.store import SessionStore
from reviewhive.observability import setup as setup_tracing

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_tracing(settings.observability)
        deps = HiveDeps(settings)
        store = SessionStore(settings.app.db_path)
        app.state.settings = settings
        app.state.deps = deps
        app.state.store = store
        app.state.pipeline = ReviewPipeline(deps, store)
        app.state.event_queues = {}
        app.state.background_tasks = set()

        # 启动前健康检查
        if settings.resilience.startup_health_check:
            health = await deps.health_check()
            app.state.health = health
            for service, ok in health.items():
                if not ok:
                    logger.warning("服务不健康: %s", service)
                else:
                    logger.info("服务正常: %s", service)
        else:
            app.state.health = {}

        if settings.memory.enabled:
            await deps.memory_bank()
        yield
        for task in list(app.state.background_tasks):
            task.cancel()
        await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
        await deps.close()
        store.close()

    app = FastAPI(title=settings.app.name, version="0.1.0", lifespan=lifespan)
    app.include_router(build_router())
    static_dir = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    return app
