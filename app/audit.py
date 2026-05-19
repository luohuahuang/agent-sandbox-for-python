"""SQLite audit log for sandbox executions and session lifecycle.

Design:
- A single asyncio writer task drains an asyncio.Queue and writes rows to
  SQLite serially. Request handlers call `enqueue_*` which is non-blocking
  (puts on the queue, returns immediately).
- WAL mode + synchronous=NORMAL gives concurrent reads against the writer
  and acceptable durability (crash may lose the last few rows in the queue).
- Reads (`get_exec`, `list_execs`) open their own short-lived connection in
  a worker thread, so they never block on the writer task.
- Schema is bootstrapped idempotently on every `start()` so adding new
  audit fields later is a matter of `ALTER TABLE` in `_init_schema`.

This module is stdlib-only (`sqlite3`) to keep the dependency surface small.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS exec_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id        TEXT UNIQUE NOT NULL,
    session_id      TEXT NOT NULL,
    conversation_id TEXT,
    agent_id        TEXT,
    trace_id        TEXT,
    started_at      TEXT NOT NULL,
    duration_ms     INTEGER NOT NULL,
    code            TEXT NOT NULL,
    stdout          TEXT,
    stderr          TEXT,
    result_preview  TEXT,
    ok              INTEGER NOT NULL,
    error_name      TEXT,
    exit_reason     TEXT,
    mem_peak_kb     INTEGER,
    cpu_ms          INTEGER
);
CREATE INDEX IF NOT EXISTS ix_exec_session ON exec_audit(session_id, started_at);
CREATE INDEX IF NOT EXISTS ix_exec_trace   ON exec_audit(trace_id);

CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    conversation_id  TEXT,
    agent_id         TEXT,
    container_id     TEXT,
    workspace_path   TEXT,
    created_at       TEXT NOT NULL,
    destroyed_at     TEXT,
    destroyed_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_session_id ON sessions(session_id);
"""


# Truncation caps for what we store. Audit is for human review, not LLM
# context, so we keep more than the 64 KiB the kernel client returns to
# callers — but still bounded to avoid unbounded DB growth.
STDOUT_CAP = 256 * 1024
STDERR_CAP = 256 * 1024
RESULT_CAP = 8 * 1024
CODE_CAP = 1024 * 1024  # 1 MiB; defends against accidental file pastes


_KIND_EXEC = "exec"
_KIND_SESSION_CREATED = "session_created"
_KIND_SESSION_DESTROYED = "session_destroyed"


def _truncate(s: str | None, cap: int) -> str | None:
    if s is None:
        return None
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n…[truncated; {len(s) - cap} more chars]"


class AuditWriter:
    """Owns the SQLite DB; exposes non-blocking enqueue + async reads."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._queue: asyncio.Queue[dict | None] | None = None
        self._task: asyncio.Task[None] | None = None

    # ----- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._init_schema)
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._writer_loop())
        logger.info("audit writer ready at %s", self._db_path)

    async def stop(self) -> None:
        """Drain the queue and stop the writer task. Idempotent."""
        if self._queue is None:
            return
        await self._queue.put(None)  # sentinel
        if self._task is not None:
            try:
                await self._task
            except Exception as exc:
                logger.warning("audit writer exited with error: %s", exc)
        self._queue = None
        self._task = None

    async def flush(self) -> None:
        """Block until all currently-queued items are written. Tests use this."""
        if self._queue is None:
            return
        await self._queue.join()

    # ----- write side --------------------------------------------------

    def enqueue_exec(self, row: dict[str, Any]) -> None:
        if self._queue is None:
            return
        payload = {
            "__kind__": _KIND_EXEC,
            **row,
            "code": _truncate(row.get("code"), CODE_CAP),
            "stdout": _truncate(row.get("stdout"), STDOUT_CAP),
            "stderr": _truncate(row.get("stderr"), STDERR_CAP),
            "result_preview": _truncate(row.get("result_preview"), RESULT_CAP),
        }
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("audit queue full; dropping exec row %s", row.get("audit_id"))

    def enqueue_session_created(self, row: dict[str, Any]) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait({"__kind__": _KIND_SESSION_CREATED, **row})
        except asyncio.QueueFull:
            logger.warning("audit queue full; dropping session-created %s", row.get("session_id"))

    def enqueue_session_destroyed(self, row: dict[str, Any]) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait({"__kind__": _KIND_SESSION_DESTROYED, **row})
        except asyncio.QueueFull:
            logger.warning("audit queue full; dropping session-destroyed %s", row.get("session_id"))

    # ----- read side ---------------------------------------------------

    async def get_exec(self, audit_id: str) -> dict | None:
        return await asyncio.to_thread(self._get_exec_sync, audit_id)

    async def list_execs(self, session_id: str, limit: int = 50) -> list[dict]:
        return await asyncio.to_thread(self._list_execs_sync, session_id, limit)

    # ----- internals ---------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._open()
        try:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    async def _writer_loop(self) -> None:
        assert self._queue is not None
        conn = await asyncio.to_thread(self._open)
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    self._queue.task_done()
                    break
                try:
                    await asyncio.to_thread(self._write_one, conn, item)
                except Exception as exc:
                    logger.warning("audit write failed: %s", exc)
                finally:
                    self._queue.task_done()
        finally:
            await asyncio.to_thread(conn.close)

    def _write_one(self, conn: sqlite3.Connection, item: dict[str, Any]) -> None:
        kind = item["__kind__"]
        if kind == _KIND_EXEC:
            conn.execute(
                """
                INSERT OR REPLACE INTO exec_audit (
                    audit_id, session_id, conversation_id, agent_id, trace_id,
                    started_at, duration_ms, code, stdout, stderr,
                    result_preview, ok, error_name, exit_reason,
                    mem_peak_kb, cpu_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["audit_id"],
                    item["session_id"],
                    item.get("conversation_id"),
                    item.get("agent_id"),
                    item.get("trace_id"),
                    item["started_at"],
                    int(item["duration_ms"]),
                    item.get("code") or "",
                    item.get("stdout"),
                    item.get("stderr"),
                    item.get("result_preview"),
                    1 if item.get("ok") else 0,
                    item.get("error_name"),
                    item.get("exit_reason"),
                    item.get("mem_peak_kb"),
                    item.get("cpu_ms"),
                ),
            )
        elif kind == _KIND_SESSION_CREATED:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, conversation_id, agent_id, container_id,
                    workspace_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item["session_id"],
                    item.get("conversation_id"),
                    item.get("agent_id"),
                    item.get("container_id"),
                    item.get("workspace_path"),
                    item["created_at"],
                ),
            )
        elif kind == _KIND_SESSION_DESTROYED:
            # Close the most recent open session row with this session_id.
            conn.execute(
                """
                UPDATE sessions
                   SET destroyed_at = ?, destroyed_reason = ?
                 WHERE id = (
                     SELECT id FROM sessions
                      WHERE session_id = ? AND destroyed_at IS NULL
                      ORDER BY id DESC LIMIT 1
                 )
                """,
                (
                    item["destroyed_at"],
                    item.get("destroyed_reason"),
                    item["session_id"],
                ),
            )
        else:
            logger.warning("unknown audit kind: %s", kind)
            return
        conn.commit()

    def _get_exec_sync(self, audit_id: str) -> dict | None:
        conn = self._open()
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM exec_audit WHERE audit_id = ?", (audit_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _list_execs_sync(self, session_id: str, limit: int) -> list[dict]:
        conn = self._open()
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM exec_audit WHERE session_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (session_id, max(1, min(int(limit), 500))),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
