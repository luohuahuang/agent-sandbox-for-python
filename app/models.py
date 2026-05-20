from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    agent_id: str | None = Field(default=None, max_length=128)
    # Template name resolved to a Docker image via SANDBOX_TEMPLATES config.
    # Falls back to "default" if omitted.
    template: str = Field(default="default", max_length=64)
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
    # Phase 3: live resource snapshot from container.stats. None if the
    # sample failed (container gone, daemon timeout). Always best-effort.
    mem_usage_mb: float | None = None
    cpu_total_ms: int | None = None


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


class AuditRecord(BaseModel):
    """One row from the sandbox-local exec_audit table."""

    audit_id: str
    session_id: str
    conversation_id: str | None = None
    agent_id: str | None = None
    trace_id: str | None = None
    started_at: str
    duration_ms: int
    code: str
    stdout: str | None = None
    stderr: str | None = None
    result_preview: str | None = None
    ok: bool
    error_name: str | None = None
    exit_reason: str | None = None
    mem_peak_kb: int | None = None
    cpu_ms: int | None = None


class SessionExecListResponse(BaseModel):
    session_id: str
    execs: list[AuditRecord]


class FileEntry(BaseModel):
    name: str
    path: str
    size_bytes: int
    is_dir: bool


class FileListResponse(BaseModel):
    files: list[FileEntry]


class FileUploadResponse(BaseModel):
    path: str
    size_bytes: int


class FileDeleteResponse(BaseModel):
    deleted: bool
