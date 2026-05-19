import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request

from app.audit import AuditWriter
from app.auth import require_api_key
from app.config import get_settings
from app.models import ExecRequest, ExecResponse
from app.runtime.docker_runtime import DockerRuntime
from app.runtime.manager import SessionManager

router = APIRouter(
    prefix="/v1/sessions",
    tags=["exec"],
    dependencies=[Depends(require_api_key)],
)


def _manager(request: Request) -> SessionManager:
    return request.app.state.manager


def _runtime(request: Request) -> DockerRuntime:
    return request.app.state.runtime


def _audit(request: Request) -> AuditWriter | None:
    return getattr(request.app.state, "audit", None)


@router.post("/{session_id}/exec", response_model=ExecResponse)
async def execute_code(
    session_id: str,
    payload: ExecRequest,
    request: Request,
    x_trace_id: str | None = Header(default=None),
) -> ExecResponse:
    mgr = _manager(request)
    runtime = _runtime(request)
    audit = _audit(request)
    settings = get_settings()
    session = mgr.require(session_id)

    timeout_s = payload.timeout_s or settings.exec_timeout_s
    audit_id = str(uuid.uuid4())
    started_at = datetime.now(tz=UTC)

    async with session.exec_lock:
        session.touch()
        result = await session.kernel.execute(payload.code, timeout_s=timeout_s)
        session.touch()

    # Best-effort resource sample post-exec. Failures (container gone,
    # cgroup unavailable) leave the fields None; never raise into the
    # response path.
    mem_peak_kb: int | None = None
    cpu_ms: int | None = None
    resource_peak: dict | None = None
    try:
        stats = await runtime.stats(session.container.container_id)
    except Exception:
        stats = None
    if stats is not None:
        mem_peak_kb = stats["mem_peak_bytes"] // 1024
        cpu_ms = stats["cpu_total_ns"] // 1_000_000
        resource_peak = {"mem_peak_kb": mem_peak_kb, "cpu_ms": cpu_ms}

    if audit is not None:
        audit.enqueue_exec(
            {
                "audit_id": audit_id,
                "session_id": session_id,
                "conversation_id": session.conversation_id,
                "agent_id": session.agent_id,
                "trace_id": x_trace_id,
                "started_at": started_at.isoformat(),
                "duration_ms": result.duration_ms,
                "code": payload.code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "result_preview": result.result_preview,
                "ok": result.ok,
                "error_name": result.error_name,
                "exit_reason": result.exit_reason,
                "mem_peak_kb": mem_peak_kb,
                "cpu_ms": cpu_ms,
            }
        )

    return ExecResponse(
        audit_id=audit_id,
        ok=result.ok,
        exit_reason=result.exit_reason,
        duration_ms=result.duration_ms,
        stdout=result.stdout,
        stderr=result.stderr,
        result_preview=result.result_preview,
        error_name=result.error_name,
        resource_peak=resource_peak,
    )
