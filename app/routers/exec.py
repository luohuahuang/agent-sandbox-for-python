import uuid

from fastapi import APIRouter, Depends, Header, Request

from app.auth import require_api_key
from app.config import get_settings
from app.models import ExecRequest, ExecResponse
from app.runtime.manager import SessionManager

router = APIRouter(
    prefix="/v1/sessions",
    tags=["exec"],
    dependencies=[Depends(require_api_key)],
)


def _manager(request: Request) -> SessionManager:
    return request.app.state.manager


@router.post("/{session_id}/exec", response_model=ExecResponse)
async def execute_code(
    session_id: str,
    payload: ExecRequest,
    request: Request,
    x_trace_id: str | None = Header(default=None),
) -> ExecResponse:
    mgr = _manager(request)
    settings = get_settings()
    session = mgr.require(session_id)

    timeout_s = payload.timeout_s or settings.exec_timeout_s
    audit_id = str(uuid.uuid4())

    async with session.exec_lock:
        session.touch()
        result = await session.kernel.execute(payload.code, timeout_s=timeout_s)
        session.touch()

    _ = x_trace_id  # propagated to audit DB in Phase 5; ignored for now

    return ExecResponse(
        audit_id=audit_id,
        ok=result.ok,
        exit_reason=result.exit_reason,
        duration_ms=result.duration_ms,
        stdout=result.stdout,
        stderr=result.stderr,
        result_preview=result.result_preview,
        error_name=result.error_name,
        resource_peak=None,
    )
