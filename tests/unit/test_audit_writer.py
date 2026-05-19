"""Unit tests for the AuditWriter (no docker required)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.audit import STDOUT_CAP, AuditWriter


@pytest.fixture
async def writer(tmp_path: Path):
    db = tmp_path / "audit.db"
    w = AuditWriter(db)
    await w.start()
    yield w
    await w.stop()


def _exec_row(audit_id: str, session_id: str = "s1", **extra) -> dict:
    base = {
        "audit_id": audit_id,
        "session_id": session_id,
        "conversation_id": "conv-1",
        "agent_id": "agent-x",
        "trace_id": None,
        "started_at": "2026-05-19T10:00:00+00:00",
        "duration_ms": 12,
        "code": "print('hi')",
        "stdout": "hi\n",
        "stderr": "",
        "result_preview": None,
        "ok": True,
        "error_name": None,
        "exit_reason": "ok",
        "mem_peak_kb": 1024,
        "cpu_ms": 5,
    }
    base.update(extra)
    return base


async def test_start_creates_schema_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    w1 = AuditWriter(db)
    await w1.start()
    await w1.stop()
    # second start on the same file must not fail
    w2 = AuditWriter(db)
    await w2.start()
    await w2.stop()
    # tables present
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "exec_audit" in tables
        assert "sessions" in tables
    finally:
        conn.close()


async def test_enqueue_then_get_returns_row(writer: AuditWriter) -> None:
    writer.enqueue_exec(_exec_row("a-1"))
    await writer.flush()
    got = await writer.get_exec("a-1")
    assert got is not None
    assert got["audit_id"] == "a-1"
    assert got["stdout"] == "hi\n"
    assert got["ok"] == 1
    assert got["exit_reason"] == "ok"


async def test_get_missing_audit_returns_none(writer: AuditWriter) -> None:
    assert await writer.get_exec("does-not-exist") is None


async def test_list_execs_returns_newest_first(writer: AuditWriter) -> None:
    writer.enqueue_exec(_exec_row("a", session_id="sX", started_at="2026-05-19T10:00:00+00:00"))
    writer.enqueue_exec(_exec_row("b", session_id="sX", started_at="2026-05-19T10:00:05+00:00"))
    writer.enqueue_exec(_exec_row("c", session_id="sX", started_at="2026-05-19T10:00:10+00:00"))
    # unrelated session — must not appear
    writer.enqueue_exec(_exec_row("z", session_id="other"))
    await writer.flush()
    rows = await writer.list_execs("sX")
    assert [r["audit_id"] for r in rows] == ["c", "b", "a"]


async def test_stdout_truncated_at_cap(writer: AuditWriter) -> None:
    huge = "x" * (STDOUT_CAP + 1000)
    writer.enqueue_exec(_exec_row("big", stdout=huge))
    await writer.flush()
    got = await writer.get_exec("big")
    assert got is not None
    assert len(got["stdout"]) < len(huge)
    assert "truncated" in got["stdout"]


async def test_session_created_then_destroyed_updates_same_row(writer: AuditWriter) -> None:
    writer.enqueue_session_created(
        {
            "session_id": "s-life",
            "conversation_id": "c-life",
            "agent_id": None,
            "container_id": "cid-1",
            "workspace_path": "/tmp/ws/s-life",
            "created_at": "2026-05-19T11:00:00+00:00",
        }
    )
    writer.enqueue_session_destroyed(
        {
            "session_id": "s-life",
            "destroyed_at": "2026-05-19T11:05:00+00:00",
            "destroyed_reason": "explicit",
        }
    )
    await writer.flush()

    conn = sqlite3.connect(writer._db_path)  # type: ignore[attr-defined]
    try:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in conn.execute("SELECT * FROM sessions WHERE session_id = ?", ("s-life",))
        ]
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["created_at"] == "2026-05-19T11:00:00+00:00"
    assert rows[0]["destroyed_at"] == "2026-05-19T11:05:00+00:00"
    assert rows[0]["destroyed_reason"] == "explicit"


async def test_stop_drains_pending_rows(tmp_path: Path) -> None:
    """A row enqueued just before stop() should still land in the DB."""
    db = tmp_path / "audit.db"
    w = AuditWriter(db)
    await w.start()
    w.enqueue_exec(_exec_row("last"))
    # stop() awaits the writer task; sentinel comes after our row.
    await w.stop()
    # Reopen to read
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT audit_id FROM exec_audit WHERE audit_id='last'").fetchone()
        assert row is not None
    finally:
        conn.close()


async def test_enqueue_before_start_is_silent_noop(tmp_path: Path) -> None:
    """Calling enqueue without start() must not raise (defensive)."""
    w = AuditWriter(tmp_path / "x.db")
    # not started yet
    w.enqueue_exec(_exec_row("ghost"))
    w.enqueue_session_created({"session_id": "g", "created_at": "now"})
    w.enqueue_session_destroyed({"session_id": "g", "destroyed_at": "now"})
    # no exception → pass
