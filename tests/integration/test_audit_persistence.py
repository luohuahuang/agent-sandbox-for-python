"""End-to-end audit persistence: real docker, real SQLite.

Boots the FastAPI app via TestClient (which runs the full lifespan,
including AuditWriter), spins a real sandbox container, runs two execs
(one OK, one Python error), then asserts the audit DB has matching rows
plus a complete sessions lifecycle row.

Skipped when docker / the sandbox image isn't available, same gate as
the other integration tests.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.config as config_mod
from app.config import Settings
from app.main import create_app


def _docker_available() -> bool:
    try:
        from app.runtime.docker_runtime import DockerRuntime

        return bool(DockerRuntime(Settings()).client().ping())
    except Exception:
        return False


def _image_available(image: str) -> bool:
    try:
        from app.runtime.docker_runtime import DockerRuntime

        DockerRuntime(Settings()).client().images.get(image)
        return True
    except Exception:
        return False


def _fresh_settings() -> Settings:
    config_mod._settings = None  # type: ignore[attr-defined]
    return Settings()


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_available(), reason="docker daemon not reachable"
    ),
    pytest.mark.skipif(
        not _image_available(_fresh_settings().sandbox_image),
        reason="sandbox image not built locally",
    ),
]


@pytest.fixture
def audit_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use a tmp audit DB so this test never pollutes ./data/sandbox_audit.db."""
    db = tmp_path / "audit.db"
    monkeypatch.setenv("AUDIT_DB_PATH", str(db))
    config_mod._settings = None  # type: ignore[attr-defined]
    return db


@pytest.fixture
def api_key() -> str:
    return os.environ.get("SANDBOX_API_KEY", "change-me-please")


@pytest.fixture
def client(api_key: str, audit_db_path: Path) -> Iterator[TestClient]:
    fastapi_app = create_app()
    with TestClient(fastapi_app) as c:
        c.headers["X-API-Key"] = api_key
        yield c


def _read_sqlite(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def test_exec_writes_full_audit_rows(client: TestClient, audit_db_path: Path) -> None:
    conv = f"audit-{uuid.uuid4().hex[:8]}"

    r = client.post("/v1/sessions", json={"conversation_id": conv})
    assert r.status_code == 201

    r1 = client.post(
        f"/v1/sessions/{conv}/exec",
        json={"code": "print('hello-audit')\n42", "timeout_s": 15},
    )
    assert r1.status_code == 200, r1.text
    audit_a = r1.json()["audit_id"]

    r2 = client.post(
        f"/v1/sessions/{conv}/exec",
        json={"code": "1 / 0", "timeout_s": 10},
    )
    assert r2.status_code == 200
    audit_b = r2.json()["audit_id"]

    # The writer is async; allow the queue to drain. Polling is faster
    # than a fixed sleep here.
    for _ in range(20):
        execs = _read_sqlite(
            audit_db_path,
            "SELECT * FROM exec_audit WHERE session_id = ? ORDER BY started_at",
            (conv,),
        )
        if len(execs) >= 2:
            break
        time.sleep(0.1)
    else:
        pytest.fail("audit rows did not appear within 2s")

    assert len(execs) == 2

    ok_row = next(e for e in execs if e["audit_id"] == audit_a)
    assert ok_row["ok"] == 1
    assert ok_row["exit_reason"] == "ok"
    assert ok_row["code"].startswith("print('hello-audit')")
    assert "hello-audit" in (ok_row["stdout"] or "")
    assert ok_row["result_preview"] == "42"

    err_row = next(e for e in execs if e["audit_id"] == audit_b)
    assert err_row["ok"] == 0
    assert err_row["exit_reason"] == "error"
    assert err_row["error_name"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in (err_row["stderr"] or "")

    # /v1/audit/{audit_id} returns the same record
    r = client.get(f"/v1/audit/{audit_a}")
    assert r.status_code == 200
    assert r.json()["stdout"].strip().splitlines()[0] == "hello-audit"

    # list endpoint returns both, newest first
    r = client.get(f"/v1/audit/sessions/{conv}/execs")
    assert r.status_code == 200
    ids = [e["audit_id"] for e in r.json()["execs"]]
    assert ids == [audit_b, audit_a]

    # destroy + verify sessions lifecycle row
    r = client.delete(f"/v1/sessions/{conv}")
    assert r.status_code == 200

    for _ in range(20):
        sessions_rows = _read_sqlite(
            audit_db_path,
            "SELECT * FROM sessions WHERE session_id = ?",
            (conv,),
        )
        if sessions_rows and sessions_rows[0]["destroyed_at"] is not None:
            break
        time.sleep(0.1)
    else:
        pytest.fail("session destroy not recorded within 2s")

    assert len(sessions_rows) == 1
    s = sessions_rows[0]
    assert s["created_at"] is not None
    assert s["destroyed_at"] is not None
    assert s["destroyed_reason"] == "explicit"


def test_exec_response_resource_peak_populated(client: TestClient) -> None:
    conv = f"audit-peak-{uuid.uuid4().hex[:8]}"
    r = client.post("/v1/sessions", json={"conversation_id": conv})
    assert r.status_code == 201
    try:
        # Allocate some memory so mem_peak_kb is comfortably above the
        # idle baseline. ~4 MiB is well over the per-exec noise floor.
        r = client.post(
            f"/v1/sessions/{conv}/exec",
            json={"code": "x = bytearray(4 * 1024 * 1024); len(x)", "timeout_s": 10},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["resource_peak"] is not None
        assert body["resource_peak"]["mem_peak_kb"] > 1000
        assert body["resource_peak"]["cpu_ms"] >= 0
    finally:
        client.delete(f"/v1/sessions/{conv}")


def test_session_info_includes_live_stats(client: TestClient) -> None:
    conv = f"audit-info-{uuid.uuid4().hex[:8]}"
    r = client.post("/v1/sessions", json={"conversation_id": conv})
    assert r.status_code == 201
    try:
        r = client.get(f"/v1/sessions/{conv}")
        assert r.status_code == 200
        body = r.json()
        # Stats sample is best-effort; either it returned numbers or both Nones.
        assert "mem_usage_mb" in body
        assert "cpu_total_ms" in body
        if body["mem_usage_mb"] is not None:
            assert body["mem_usage_mb"] > 0
    finally:
        client.delete(f"/v1/sessions/{conv}")
