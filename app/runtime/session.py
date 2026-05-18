"""Session state — one per sandbox container."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from app.runtime.docker_runtime import ContainerHandle
from app.runtime.kernel_client import KernelClient


class SessionStatus(StrEnum):
    CREATING = "creating"
    READY = "ready"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    ERROR = "error"


@dataclass(slots=True)
class Session:
    session_id: str
    conversation_id: str | None
    agent_id: str | None

    container: ContainerHandle
    kernel: KernelClient

    created_at: datetime
    last_used_at: float = field(default_factory=time.monotonic)
    status: SessionStatus = SessionStatus.CREATING

    exec_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        self.last_used_at = time.monotonic()

    def idle_seconds(self) -> int:
        return int(time.monotonic() - self.last_used_at)


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
