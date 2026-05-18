from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    agent_id: str | None = Field(default=None, max_length=128)
    image: str | None = None
    env: dict[str, str] | None = None


class SessionInfo(BaseModel):
    session_id: str
    conversation_id: str | None = None
    agent_id: str | None = None
    container_id: str
    workspace_path: str
    status: str
    created_at: datetime
    idle_seconds: int = 0


class CreateSessionResponse(BaseModel):
    session_id: str
    container_id: str
    workspace_path: str
    created_at: datetime


class DestroyResponse(BaseModel):
    stopped: bool


class ExecRequest(BaseModel):
    code: str = Field(min_length=0, max_length=1_000_000)
    timeout_s: int | None = Field(default=None, ge=1, le=600)


class ExecResponse(BaseModel):
    audit_id: str
    ok: bool
    exit_reason: str
    duration_ms: int
    stdout: str
    stderr: str
    result_preview: str | None = None
    error_name: str | None = None
    resource_peak: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
