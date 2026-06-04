"""SessionManager — orchestrates per-conversation sandbox sessions.

Phase 1 scope: in-memory map, idempotent get_or_create keyed on
conversation_id, per-session exec serialization, max-session cap.
Phase 3: session-lifecycle events go to the sandbox-local audit DB
(AuditWriter, optional — None in tests).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from fastapi import HTTPException, status

from app.audit import AuditWriter
from app.config import Settings
from app.models import CreateSessionRequest
from app.runtime.docker_runtime import DockerRuntime
from app.runtime.kernel_client import KernelClient
from app.runtime.session import Session, SessionStatus, utcnow

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        runtime: DockerRuntime,
        audit: AuditWriter | None = None,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._audit = audit
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

    def resolve_image(self, template: str) -> str:
        """Map template name → Docker image, falling back to default image."""
        templates = self._settings.sandbox_templates
        if template in templates:
            return templates[template]
        if "default" in templates:
            return templates["default"]
        return self._settings.sandbox_image

    async def _create_task(
        self, req: CreateSessionRequest, fut: asyncio.Future[Session]
    ) -> None:
        session_id = req.conversation_id
        try:
            image = self.resolve_image(req.template)
            handle = await self._runtime.create(session_id, image=image)
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
            if self._audit is not None:
                self._audit.enqueue_session_created(
                    {
                        "session_id": session.session_id,
                        "conversation_id": session.conversation_id,
                        "agent_id": session.agent_id,
                        "container_id": handle.container_id,
                        "workspace_path": str(handle.workspace_path),
                        "created_at": session.created_at.isoformat(),
                    }
                )
            fut.set_result(session)
        except Exception as exc:
            logger.exception("session create failed: %s", session_id)
            async with self._lock:
                self._creating.pop(session_id, None)
            if not fut.done():
                fut.set_exception(exc)

    async def destroy(self, session_id: str, reason: str = "explicit") -> bool:
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

        if self._audit is not None:
            self._audit.enqueue_session_destroyed(
                {
                    "session_id": session_id,
                    "destroyed_at": utcnow().isoformat(),
                    "destroyed_reason": reason,
                }
            )
        return True

    # ----- background reaper -------------------------------------------

    async def start_reaper(self) -> None:
        """Continuously evict sessions that have exceeded idle_ttl or max_age."""
        import time as _time

        while True:
            await asyncio.sleep(120)  # check every 2 minutes
            now = _time.monotonic()
            to_destroy: list[str] = []
            async with self._lock:
                for sid, s in list(self._sessions.items()):
                    if s.status != SessionStatus.READY:
                        continue
                    idle = s.idle_seconds()
                    age = int(now - _time.mktime(s.created_at.timetuple()))
                    if idle > self._settings.idle_ttl_s:
                        logger.info("reaper: evicting %s (idle %ds)", sid, idle)
                        to_destroy.append(sid)
                    elif age > self._settings.max_age_s:
                        logger.info("reaper: evicting %s (age %ds)", sid, age)
                        to_destroy.append(sid)
            for sid in to_destroy:
                try:
                    await self.destroy(sid, reason="ttl_expired")
                except Exception as exc:
                    logger.warning("reaper: error destroying %s: %s", sid, exc)

    async def shutdown(self) -> None:
        ids = list(self._sessions.keys())
        for sid in ids:
            await self.destroy(sid, reason="shutdown")
        await self._runtime.close()
