import asyncio

from fastapi import APIRouter, Depends, Request

from app.auth import require_api_key
from app.models import (
    CreateSessionRequest,
    CreateSessionResponse,
    DestroyResponse,
    SessionInfo,
)
from app.runtime.docker_runtime import DockerRuntime
from app.runtime.manager import SessionManager
from app.runtime.session import Session

router = APIRouter(
    prefix="/v1/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_api_key)],
)


def _manager(request: Request) -> SessionManager:
    return request.app.state.manager


def _runtime(request: Request) -> DockerRuntime:
    return request.app.state.runtime


async def _to_info(runtime: DockerRuntime, s: Session) -> SessionInfo:
    """Build SessionInfo, attaching a best-effort live resource sample."""
    mem_usage_mb: float | None = None
    cpu_total_ms: int | None = None
    try:
        stats = await runtime.stats(s.container.container_id)
    except Exception:
        stats = None
    if stats is not None:
        mem_usage_mb = round(stats["mem_usage_bytes"] / (1024 * 1024), 2)
        cpu_total_ms = stats["cpu_total_ns"] // 1_000_000
    return SessionInfo(
        session_id=s.session_id,
        conversation_id=s.conversation_id,
        agent_id=s.agent_id,
        container_id=s.container.container_id,
        workspace_path=str(s.container.workspace_path),
        status=s.status.value,
        created_at=s.created_at,
        idle_seconds=s.idle_seconds(),
        mem_usage_mb=mem_usage_mb,
        cpu_total_ms=cpu_total_ms,
    )


@router.post("", response_model=CreateSessionResponse, status_code=201)
async def create_session(
    payload: CreateSessionRequest, request: Request
) -> CreateSessionResponse:
    mgr = _manager(request)
    session = await mgr.get_or_create(payload)
    return CreateSessionResponse(
        session_id=session.session_id,
        container_id=session.container.container_id,
        workspace_path=str(session.container.workspace_path),
        created_at=session.created_at,
    )


@router.get("", response_model=list[SessionInfo])
async def list_sessions(request: Request) -> list[SessionInfo]:
    mgr = _manager(request)
    runtime = _runtime(request)
    # Parallelize stats sampling — each call is ~200-500ms from the daemon.
    return await asyncio.gather(*(_to_info(runtime, s) for s in mgr.list()))


@router.get("/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str, request: Request) -> SessionInfo:
    mgr = _manager(request)
    runtime = _runtime(request)
    return await _to_info(runtime, mgr.require(session_id))


@router.delete("/{session_id}", response_model=DestroyResponse)
async def destroy_session(session_id: str, request: Request) -> DestroyResponse:
    mgr = _manager(request)
    stopped = await mgr.destroy(session_id)
    return DestroyResponse(stopped=stopped)
