"""Tests for SessionManager idempotency and cap, using fake runtime / kernel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import app.config as config_mod
from app.config import Settings
from app.models import CreateSessionRequest
from app.runtime.docker_runtime import ContainerHandle
from app.runtime.manager import SessionManager


@dataclass
class FakeKernel:
    started: bool = False
    closed: bool = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def execute(self, code: str, timeout_s: float) -> Any:
        raise NotImplementedError


class FakeRuntime:
    def __init__(self, settings: Settings, *, slow: bool = False) -> None:
        self.settings = settings
        self.created: list[str] = []
        self.destroyed: list[str] = []
        self._slow = slow

    async def create(self, session_id: str) -> ContainerHandle:
        if self._slow:
            await asyncio.sleep(0.05)
        self.created.append(session_id)
        return ContainerHandle(
            container_id=f"cid-{session_id}",
            kernel_key="k",
            host_ports={
                "shell_port": 1,
                "iopub_port": 2,
                "stdin_port": 3,
                "control_port": 4,
                "hb_port": 5,
            },
            workspace_path=Path("/tmp/ws") / session_id,
        )

    async def destroy(self, container_id: str) -> None:
        self.destroyed.append(container_id)

    async def healthcheck(self) -> bool:
        return True

    async def close(self) -> None:
        pass


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("SANDBOX_API_KEY", "test-key")
    monkeypatch.setenv("MAX_SESSIONS", "2")
    config_mod._settings = None  # force re-read
    s = config_mod.get_settings()
    yield s
    config_mod._settings = None


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> SessionManager:
    # Swap KernelClient with a fake that doesn't talk to ZMQ.
    import app.runtime.manager as mgr_mod

    monkeypatch.setattr(mgr_mod, "KernelClient", lambda *a, **kw: FakeKernel())  # type: ignore[arg-type]
    rt = FakeRuntime(settings)
    return SessionManager(settings, rt)  # type: ignore[arg-type]


async def test_idempotent_create_same_conversation(manager: SessionManager) -> None:
    req = CreateSessionRequest(conversation_id="conv-A")
    a = await manager.get_or_create(req)
    b = await manager.get_or_create(req)
    assert a is b
    assert a.container.container_id == "cid-conv-A"


async def test_concurrent_create_dedupes(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    import app.runtime.manager as mgr_mod

    monkeypatch.setattr(mgr_mod, "KernelClient", lambda *a, **kw: FakeKernel())  # type: ignore[arg-type]
    rt = FakeRuntime(settings, slow=True)
    mgr = SessionManager(settings, rt)  # type: ignore[arg-type]
    req = CreateSessionRequest(conversation_id="conv-B")
    a, b, c = await asyncio.gather(
        mgr.get_or_create(req), mgr.get_or_create(req), mgr.get_or_create(req)
    )
    assert a is b is c
    assert rt.created == ["conv-B"]  # underlying runtime called exactly once


async def test_cap_returns_429(manager: SessionManager) -> None:
    await manager.get_or_create(CreateSessionRequest(conversation_id="c1"))
    await manager.get_or_create(CreateSessionRequest(conversation_id="c2"))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await manager.get_or_create(CreateSessionRequest(conversation_id="c3"))
    assert exc.value.status_code == 429


async def test_destroy_cleans_up(manager: SessionManager) -> None:
    s = await manager.get_or_create(CreateSessionRequest(conversation_id="c1"))
    stopped = await manager.destroy("c1")
    assert stopped is True
    assert manager.get("c1") is None
    # Re-create after destroy should work.
    s2 = await manager.get_or_create(CreateSessionRequest(conversation_id="c1"))
    assert s2 is not s
