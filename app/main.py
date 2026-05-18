"""agent-sandbox FastAPI gateway entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.routers import exec as exec_router
from app.routers import health, sessions
from app.runtime.docker_runtime import DockerRuntime
from app.runtime.manager import SessionManager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    runtime = DockerRuntime(settings)
    manager = SessionManager(settings, runtime)
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.manager = manager

    logger.info("agent-sandbox gateway starting (image=%s)", settings.sandbox_image)
    try:
        yield
    finally:
        logger.info("agent-sandbox gateway shutting down")
        await manager.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="agent-sandbox",
        version="0.1.0",
        description="Per-session Docker sandbox with Jupyter kernel.",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(exec_router.router)
    return app


app = create_app()
