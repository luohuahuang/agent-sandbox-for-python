"""SessionManager — orchestrates per-conversation sandbox sessions.

Phase 1 scope: in-memory map, idempotent get_or_create keyed on
conversation_id, per-session exec serialization, max-session cap.
Audit DB and idle reaper land in later phases.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from fastapi import HTTPException, status

from app.config import Settings
from app.models import CreateSessionRequest
from app.runtime.docker_runtime import DockerRuntime
from app.runtime.kernel_client import KernelClient
from app.runtime.session import Session, SessionStatus, utcnow

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, settings: Settings, runtime: DockerRuntime) -> None:
        self._settings = settings
        self._runtime = runtime
        self._sessions: dict[str, Session] = {}
        # Pending futures keyed by session_id for idempotent in-flight create.
        self._creating: dict[str, asyncio.Future[Session]] = {}
        # Strong refs to create tasks so they aren't garbage collected mid-flight.
        self._create_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    # ----- lookups -----------------------------------------------------

    def get(self, session_id: str) -> Session | None:
        s = self._sessions.get(session_id)
        if s is None or s.status in (SessionStatus.DESTROYED, SessionStatus.DESTROYING):
            return None
        return s

    def require(self, session_id: str) -> Session:
        s = self.get(session_id)
        if s is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"session {session_id!r} not found")
        if s.status != SessionStatus.READY:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"session {session_id!r} not ready (status={s.status.value})",
            )
        return s

    def list(self) -> Iterable[Session]:
        return list(self._sessions.values())

    # ----- create / destroy --------------------------------------------

    async def get_or_create(self, req: CreateSessionRequest) -> Session:
        session_id = req.conversation_id

        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None and existing.status == SessionStatus.READY:
                return existing
            pending = self._creating.get(session_id)
            if pending is not None:
                fut = pending
            else:
                if len(self._sessions) >= self._settings.max_sessions:
                    raise HTTPException(
                        status.HTTP_429_TOO_MANY_REQUESTS,
                        f"max sessions ({self._settings.max_sessions}) reached",
                    )
                fut = asyncio.get_running_loop().create_future()
                self._creating[session_id] = fut
                task = asyncio.create_task(self._create_task(req, fut))
                self._create_tasks.add(task)
                task.add_done_callback(self._create_tasks.discard)

        try:
            return await fut
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"failed to create session: {exc}",
            ) from exc

    async def _create_task(
        self, req: CreateSessionRequest, fut: asyncio.Future[Session]
    ) -> None:
        session_id = req.conversation_id
        try:
            handle = await self._runtime.create(session_id)
            kc = KernelClient(handle.host_ports, handle.kernel_key)
            try:
                await kc.start()
            except Exception:
                await self._runtime.destroy(handle.container_id)
                raise
            session = Session(
                session_id=session_id,
                conversation_id=req.conversation_id,
                agent_id=req.agent_id,
                container=handle,
                kernel=kc,
                created_at=utcnow(),
                status=SessionStatus.READY,
            )
            async with self._lock:
                self._sessions[session_id] = session
                self._creating.pop(session_id, None)
            fut.set_result(session)
        except Exception as exc:
            logger.exception("session create failed: %s", session_id)
            async with self._lock:
                self._creating.pop(session_id, None)
            if not fut.done():
                fut.set_exception(exc)

    async def destroy(self, session_id: str) -> bool:
        async with self._lock:
            s = self._sessions.get(session_id)
            if s is None or s.status in (SessionStatus.DESTROYED, SessionStatus.DESTROYING):
                return False
            s.status = SessionStatus.DESTROYING

        try:
            await s.kernel.close()
        except Exception as exc:
            logger.warning("kernel close error for %s: %s", session_id, exc)
        try:
            await self._runtime.destroy(s.container.container_id)
        except Exception as exc:
            logger.warning("container destroy error for %s: %s", session_id, exc)
        s.status = SessionStatus.DESTROYED

        async with self._lock:
            self._sessions.pop(session_id, None)
        return True

    async def shutdown(self) -> None:
        ids = list(self._sessions.keys())
        for sid in ids:
            await self.destroy(sid)
        await self._runtime.close()
