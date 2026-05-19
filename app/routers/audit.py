"""Audit log read endpoints.

The write side is fed by /exec and session-lifecycle hooks (see
app/audit.py and app/runtime/manager.py). These read-only endpoints
let an operator (or the upstream digital-employee Mentor UI) look up
the full execution record for a given audit_id, or page through the
recent execs of a session.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.audit import AuditWriter
from app.auth import require_api_key
from app.models import AuditRecord, SessionExecListResponse

router = APIRouter(
    prefix="/v1/audit",
    tags=["audit"],
    dependencies=[Depends(require_api_key)],
)


def _audit(request: Request) -> AuditWriter:
    audit = getattr(request.app.state, "audit", None)
    if audit is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "audit log is not initialized",
        )
    return audit


def _row_to_record(row: dict) -> AuditRecord:
    return AuditRecord(
        audit_id=row["audit_id"],
        session_id=row["session_id"],
        conversation_id=row.get("conversation_id"),
        agent_id=row.get("agent_id"),
        trace_id=row.get("trace_id"),
        started_at=row["started_at"],
        duration_ms=row["duration_ms"],
        code=row["code"],
        stdout=row.get("stdout"),
        stderr=row.get("stderr"),
        result_preview=row.get("result_preview"),
        ok=bool(row["ok"]),
        error_name=row.get("error_name"),
        exit_reason=row.get("exit_reason"),
        mem_peak_kb=row.get("mem_peak_kb"),
        cpu_ms=row.get("cpu_ms"),
    )


@router.get("/{audit_id}", response_model=AuditRecord)
async def get_audit(audit_id: str, request: Request) -> AuditRecord:
    """Look up a single exec audit record by its audit_id."""
    row = await _audit(request).get_exec(audit_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"audit_id {audit_id!r} not found",
        )
    return _row_to_record(row)


@router.get(
    "/sessions/{session_id}/execs",
    response_model=SessionExecListResponse,
)
async def list_session_execs(
    session_id: str,
    request: Request,
    limit: int = 50,
) -> SessionExecListResponse:
    """List recent exec records for a session, newest first.

    ``limit`` is clamped to [1, 500] by the underlying store.
    """
    rows = await _audit(request).list_execs(session_id, limit=limit)
    return SessionExecListResponse(
        session_id=session_id,
        execs=[_row_to_record(r) for r in rows],
    )
