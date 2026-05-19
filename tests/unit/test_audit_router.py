"""Unit tests for the audit router — no docker, no live SessionManager.

Builds a minimal FastAPI app with only the audit router + an AuditWriter
attached, so we exercise auth + serialization + 404 / 200 paths cheaply.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.config as config_mod
from app.audit import AuditWriter
from app.routers import audit as audit_router


@pytest.fixture
async def app_with_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SANDBOX_API_KEY", "test-key")
    config_mod._settings = None  # type: ignore[attr-defined]

    audit = AuditWriter(tmp_path / "audit.db")
    await audit.start()

    fastapi_app = FastAPI()
    fastapi_app.state.audit = audit
    fastapi_app.include_router(audit_router.router)

    try:
        yield fastapi_app, audit
    finally:
        await audit.stop()


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _exec_row(audit_id: str, session_id: str = "s1", **extra) -> dict:
    base = {
        "audit_id": audit_id,
        "session_id": session_id,
        "conversation_id": "conv-1",
        "agent_id": "agent-x",
        "trace_id": None,
        "started_at": "2026-05-19T10:00:00+00:00",
        "duration_ms": 42,
        "code": "print(1+1)",
        "stdout": "2\n",
        "stderr": "",
        "result_preview": None,
        "ok": True,
        "error_name": None,
        "exit_reason": "ok",
        "mem_peak_kb": 2048,
        "cpu_ms": 12,
    }
    base.update(extra)
    return base


async def test_get_audit_returns_full_record(app_with_audit) -> None:
    fastapi_app, audit = app_with_audit
    audit.enqueue_exec(_exec_row("a-1"))
    await audit.flush()
    async with await _client(fastapi_app) as c:
        r = await c.get("/v1/audit/a-1", headers={"X-API-Key": "test-key"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["audit_id"] == "a-1"
        assert body["stdout"] == "2\n"
        assert body["ok"] is True
        assert body["mem_peak_kb"] == 2048
        assert body["cpu_ms"] == 12


async def test_get_audit_unknown_returns_404(app_with_audit) -> None:
    fastapi_app, _ = app_with_audit
    async with await _client(fastapi_app) as c:
        r = await c.get("/v1/audit/does-not-exist", headers={"X-API-Key": "test-key"})
        assert r.status_code == 404


async def test_get_audit_requires_api_key(app_with_audit) -> None:
    fastapi_app, audit = app_with_audit
    audit.enqueue_exec(_exec_row("a-1"))
    await audit.flush()
    async with await _client(fastapi_app) as c:
        # missing header
        r = await c.get("/v1/audit/a-1")
        assert r.status_code == 401
        # wrong key
        r = await c.get("/v1/audit/a-1", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401


async def test_list_session_execs_newest_first(app_with_audit) -> None:
    fastapi_app, audit = app_with_audit
    audit.enqueue_exec(_exec_row("x1", session_id="sA", started_at="2026-05-19T10:00:00+00:00"))
    audit.enqueue_exec(_exec_row("x2", session_id="sA", started_at="2026-05-19T10:00:05+00:00"))
    audit.enqueue_exec(_exec_row("x3", session_id="sA", started_at="2026-05-19T10:00:10+00:00"))
    audit.enqueue_exec(_exec_row("other", session_id="sB"))
    await audit.flush()

    async with await _client(fastapi_app) as c:
        r = await c.get(
            "/v1/audit/sessions/sA/execs",
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == "sA"
        assert [e["audit_id"] for e in body["execs"]] == ["x3", "x2", "x1"]


async def test_list_session_execs_empty_session(app_with_audit) -> None:
    fastapi_app, _ = app_with_audit
    async with await _client(fastapi_app) as c:
        r = await c.get(
            "/v1/audit/sessions/no-such-session/execs",
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 200
        assert r.json() == {"session_id": "no-such-session", "execs": []}
