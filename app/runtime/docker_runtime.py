"""Thin async wrapper over docker SDK for sandbox container lifecycle.

Phase 1: no security hardening flags are applied yet — read-only rootfs,
cap_drop, no-new-privileges, network policy all land in Phase 2.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container

import docker
from app.config import Settings

logger = logging.getLogger(__name__)


def _resolve_docker_host() -> str | None:
    """Resolve the docker daemon endpoint, with Colima fallback.

    docker.from_env() reads DOCKER_HOST but does NOT read docker CLI
    contexts. Colima sets the docker CLI context but leaves DOCKER_HOST
    unset, so the SDK falls back to /var/run/docker.sock and fails.

    Order of resolution:
      1. DOCKER_HOST env var (delegated to docker.from_env)
      2. Current docker CLI context's daemon endpoint
      3. Known Colima socket paths under ~/.colima/
      4. None → docker.from_env() runs with its own defaults
    """
    if os.environ.get("DOCKER_HOST"):
        return None
    if shutil.which("docker"):
        try:
            out = subprocess.run(
                [
                    "docker",
                    "context",
                    "inspect",
                    "--format",
                    "{{.Endpoints.docker.Host}}",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            host = out.stdout.strip()
            if host and host != "<no value>":
                return host
        except Exception as exc:
            logger.debug("docker context inspect failed: %s", exc)
    home = Path.home()
    for candidate in (
        home / ".colima" / "default" / "docker.sock",
        home / ".colima" / "docker.sock",
    ):
        if candidate.exists():
            return f"unix://{candidate}"
    return None


KERNEL_PORTS = {
    "shell_port": "5555/tcp",
    "iopub_port": "5556/tcp",
    "stdin_port": "5557/tcp",
    "control_port": "5558/tcp",
    "hb_port": "5559/tcp",
}


@dataclass(slots=True)
class ContainerHandle:
    """All info the gateway needs to talk to a running sandbox container."""

    container_id: str
    kernel_key: str
    # Maps semantic port name ("shell_port", "iopub_port", ...) to the
    # host-side TCP port that Docker assigned (always bound to 127.0.0.1).
    host_ports: dict[str, int]
    workspace_path: Path


class DockerRuntime:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: docker.DockerClient | None = None

    def _client_or_connect(self) -> docker.DockerClient:
        if self._client is None:
            base_url = _resolve_docker_host()
            if base_url:
                logger.info("connecting to docker daemon at %s", base_url)
                self._client = docker.DockerClient(base_url=base_url)
            else:
                self._client = docker.from_env()
        return self._client

    async def healthcheck(self) -> bool:
        def _ping() -> bool:
            try:
                return self._client_or_connect().ping()
            except Exception as exc:
                logger.warning("docker daemon ping failed: %s", exc)
                return False

        return await asyncio.to_thread(_ping)

    async def create(self, session_id: str) -> ContainerHandle:
        """Start a sandbox container and return the handle once it's running."""
        settings = self._settings
        kernel_key = secrets.token_urlsafe(32)

        workspace_path = settings.workspace_root / session_id
        workspace_path.mkdir(parents=True, exist_ok=True)
        # uid 1000 in the container needs to write here. Phase 2 will tighten this.
        workspace_path.chmod(0o777)

        port_bindings = {
            container_port: ("127.0.0.1", None)  # None -> ephemeral host port
            for container_port in KERNEL_PORTS.values()
        }

        def _run() -> Container:
            client = self._client_or_connect()
            try:
                return client.containers.run(
                    image=settings.sandbox_image,
                    detach=True,
                    name=f"sbx-{session_id}",
                    environment={"KERNEL_KEY": kernel_key},
                    ports=port_bindings,
                    volumes={
                        str(workspace_path): {"bind": "/workspace", "mode": "rw"},
                    },
                    mem_limit=f"{settings.mem_limit_mb}m",
                    nano_cpus=settings.cpu_nanos,
                    pids_limit=settings.pids_limit,
                )
            except ImageNotFound:
                raise RuntimeError(
                    f"sandbox image {settings.sandbox_image!r} not found; "
                    f"build it with `docker build -t {settings.sandbox_image} "
                    "-f docker/Dockerfile.sandbox docker/`"
                ) from None

        container = await asyncio.to_thread(_run)

        try:
            host_ports = await self._wait_for_ports(container)
        except Exception:
            await self.destroy(container.id)
            raise

        logger.info(
            "container %s started for session %s, host ports %s",
            container.short_id,
            session_id,
            host_ports,
        )
        return ContainerHandle(
            container_id=container.id,
            kernel_key=kernel_key,
            host_ports=host_ports,
            workspace_path=workspace_path,
        )

    async def _wait_for_ports(
        self, container: Container, timeout_s: float = 15.0
    ) -> dict[str, int]:
        """Poll the container until Docker has published its kernel ports."""

        def _try_read() -> dict[str, int] | None:
            container.reload()
            if container.status != "running":
                return None
            net = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
            out: dict[str, int] = {}
            for name, container_port in KERNEL_PORTS.items():
                bindings = net.get(container_port)
                if not bindings:
                    return None
                # Prefer the loopback binding if more than one.
                host_port: int | None = None
                for b in bindings:
                    if b.get("HostIp") in {"127.0.0.1", "::1", ""}:
                        host_port = int(b["HostPort"])
                        if b.get("HostIp") == "127.0.0.1":
                            break
                if host_port is None:
                    return None
                out[name] = host_port
            return out

        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            ports = await asyncio.to_thread(_try_read)
            if ports is not None:
                return ports
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"container {container.short_id} did not publish kernel ports")
            await asyncio.sleep(0.2)

    async def destroy(self, container_id: str) -> None:
        def _kill() -> None:
            try:
                c = self._client_or_connect().containers.get(container_id)
            except NotFound:
                return
            try:
                c.stop(timeout=self._settings.kill_grace_s)
            except APIError as exc:
                logger.warning("stop failed for %s: %s; forcing remove", container_id, exc)
            with contextlib.suppress(NotFound):
                c.remove(force=True)

        await asyncio.to_thread(_kill)

    async def inspect(self, container_id: str) -> dict:
        def _do() -> dict:
            try:
                c = self._client_or_connect().containers.get(container_id)
            except NotFound:
                return {"status": "gone"}
            c.reload()
            return {"status": c.status, "attrs": c.attrs}

        return await asyncio.to_thread(_do)

    async def close(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None
