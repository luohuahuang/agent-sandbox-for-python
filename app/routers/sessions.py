
from fastapi import APIRouter, Depends, Request

from app.auth import require_api_key
from app.models import (
    CreateSessionRequest,
    CreateSessionResponse,
    DestroyResponse,
    SessionInfo,
)
from app.runtime.manager import SessionManager
from app.runtime.session import Session

router = APIRouter(
    prefix="/v1/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_api_key)],
)


def _manager(request: Request) -> SessionManager:
    return request.app.state.manager


def _to_info(s: Session) -> SessionInfo:
    return SessionInfo(
        session_id=s.session_id,
        conversation_id=s.conversation_id,
        agent_id=s.agent_id,
        container_id=s.container.container_id,
        workspace_path=str(s.container.workspace_path),
        status=s.status.value,
        created_at=s.created_at,
        idle_seconds=s.idle_seconds(),
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
    return [_to_info(s) for s in mgr.list()]


@router.get("/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str, request: Request) -> SessionInfo:
    mgr = _manager(request)
    return _to_info(mgr.require(session_id))


@router.delete("/{session_id}", response_model=DestroyResponse)
async def destroy_session(session_id: str, request: Request) -> DestroyResponse:
    mgr = _manager(request)
    stopped = await mgr.destroy(session_id)
    return DestroyResponse(stopped=stopped)
