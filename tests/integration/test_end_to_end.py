"""End-to-end smoke against a real Docker daemon and the built sandbox image.

Skipped unless the docker daemon is reachable. Build the image first:

    docker build -t agent-sandbox:latest -f docker/Dockerfile.sandbox docker/
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        return client.ping()
    except Exception:
        return False


pytestmark = [pytest.mark.integration, pytest.mark.skipif(not _docker_available(), reason="docker daemon not reachable")]


async def test_create_exec_destroy_roundtrip() -> None:
    from app.config import Settings
    from app.models import CreateSessionRequest
    from app.runtime.docker_runtime import DockerRuntime
    from app.runtime.manager import SessionManager

    settings = Settings(
        sandbox_image=os.environ.get("SANDBOX_IMAGE", "agent-sandbox:latest"),
        workspace_root=settings_workspace_root(),
    )
    runtime = DockerRuntime(settings)
    mgr = SessionManager(settings, runtime)
    conv = f"itest-{uuid.uuid4().hex[:8]}"
    try:
        session = await mgr.get_or_create(CreateSessionRequest(conversation_id=conv))
        result = await session.kernel.execute("print('hi')", timeout_s=15)
        assert result.ok, result
        assert result.stdout.strip() == "hi"

        result2 = await session.kernel.execute("x = 41\nx + 1", timeout_s=15)
        assert result2.ok
        assert result2.result_preview == "42"
    finally:
        await mgr.shutdown()


def settings_workspace_root():
    import pathlib

    # Under $HOME so Colima's default Lima mount can see it.
    p = pathlib.Path.home() / ".agent-sandbox" / "itest-ws"
    p.mkdir(parents=True, exist_ok=True)
    return p
