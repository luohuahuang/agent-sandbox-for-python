"""agent-sandbox FastAPI gateway entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.audit import AuditWriter
from app.config import get_settings
from app.routers import audit as audit_router
from app.routers import exec as exec_router
from app.routers import health, sessions
from app.runtime.docker_runtime import DockerRuntime
from app.runtime.manager import SessionManager
from app.runtime.proxy import EgressProxy

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    runtime = DockerRuntime(settings)

    audit = AuditWriter(settings.audit_db_path)
    await audit.start()

    proxy: EgressProxy | None = None
    try:
        proxy = EgressProxy(settings, runtime.client())
        await proxy.start()
        runtime.attach_proxy(proxy)
    except Exception as exc:
        logger.error(
            "egress proxy bootstrap failed: %s — sandboxes will run on the "
            "default bridge with no isolation. Fix this before production.",
            exc,
        )

    manager = SessionManager(settings, runtime, audit)
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.manager = manager
    app.state.proxy = proxy
    app.state.audit = audit

    logger.info("agent-sandbox gateway starting (image=%s)", settings.sandbox_image)
    try:
        yield
    finally:
        logger.info("agent-sandbox gateway shutting down")
        await manager.shutdown()
        if proxy is not None:
            await proxy.stop()
        await audit.stop()


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
    app.include_router(audit_router.router)
    return app


app = create_app()
