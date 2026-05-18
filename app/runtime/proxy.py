"""Lifecycle for the sbx-net bridge and the sbx-proxy squid sidecar.

The bridge is created with `enable_ip_masquerade=false`: containers on
sbx-net can talk to each other (so the sandbox reaches sbx-proxy by
hostname) and the host can still publish their ports (so the gateway
reaches the Jupyter kernel on 127.0.0.1), but they cannot egress to
the internet directly because the host does not SNAT their packets.
The proxy container straddles sbx-net AND the default bridge, so all
allowed egress flows through squid's domain allowlist.

(We considered `--internal` but Docker silently drops port publishing
on internal networks, breaking the host→kernel ZMQ channel.)
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from docker.errors import ImageNotFound, NotFound

import docker
from app.config import Settings

logger = logging.getLogger(__name__)


PROXY_PORT = 3128
SQUID_CONF_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "docker" / "squid.conf.template"
)


def _normalize_domain(raw: str) -> str:
    """Turn 'pypi.org' / '*.pypi.org' into squid's '.pypi.org' form."""
    d = raw.strip()
    if not d:
        return ""
    if d.startswith("*."):
        d = d[1:]
    elif not d.startswith("."):
        d = "." + d
    return d


def _dedup_subdomains(domains: list[str]) -> list[str]:
    """Drop entries already covered by a broader (shorter) entry.

    Squid's dstdomain ACL refuses to parse if a more specific entry is
    listed alongside the parent (e.g. .files.pythonhosted.org vs
    .pythonhosted.org). Sort by length ascending so the broader entry
    wins; skip any later entry that ends with an already-kept domain.
    """
    seen = sorted(set(d for d in domains if d), key=len)
    kept: list[str] = []
    for d in seen:
        if any(d.endswith(parent) and d != parent for parent in kept):
            continue
        kept.append(d)
    return kept


def render_squid_conf(template: str, allowlist: list[str]) -> str:
    domains = _dedup_subdomains([_normalize_domain(x) for x in allowlist])
    if not domains:
        # squid needs at least one token, otherwise it fails to parse.
        # Use a sentinel that will never match real traffic.
        domains = [".never-allowed.invalid"]
    return template.replace("{{ALLOWED_DOMAINS}}", " ".join(domains))


class EgressProxy:
    """Manages sbx-net bridge + sbx-proxy squid container."""

    def __init__(self, settings: Settings, client: docker.DockerClient) -> None:
        self._settings = settings
        self._client = client
        self._container_id: str | None = None
        self.network_ready: bool = False
        self.proxy_ready: bool = False

    @property
    def network_name(self) -> str:
        return self._settings.network_name

    @property
    def container_name(self) -> str:
        return self._settings.proxy_container_name

    @property
    def http_proxy_url(self) -> str:
        return f"http://{self.container_name}:{PROXY_PORT}"

    @property
    def allowlist(self) -> list[str]:
        return [s.strip() for s in self._settings.egress_allowlist.split(",") if s.strip()]

    # ----- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        """Idempotent: ensure network + proxy container are up. Soft-fails.

        On full success: network_ready and proxy_ready both True; sandbox
        containers will attach to sbx-net with HTTP_PROXY env injected.

        On partial success (network ok, proxy failed): sandboxes still
        attach to sbx-net but have no working egress. HTTP_PROXY is not
        injected. This is the SAFE failure mode.

        On total failure (no docker, no network): caller (DockerRuntime)
        falls back to default bridge — logged loudly.
        """
        try:
            await asyncio.to_thread(self._ensure_network)
            self.network_ready = True
        except Exception as exc:
            logger.error("sbx-net setup failed: %s", exc)
            return

        try:
            await asyncio.to_thread(self._ensure_proxy)
            self.proxy_ready = True
            logger.info(
                "egress proxy ready at %s (allowlist=%s)",
                self.http_proxy_url,
                self.allowlist,
            )
        except Exception as exc:
            logger.warning(
                "proxy container start failed: %s — sandbox containers will "
                "run on sbx-net with NO internet access",
                exc,
            )

    async def stop(self) -> None:
        """Phase 2: leave proxy + network running across gateway restarts.

        They are shared infrastructure; keeping them up keeps restarts
        fast. Sandbox containers are owned by SessionManager and torn
        down separately on shutdown.
        """
        return

    # ----- internals ----------------------------------------------------

    def _ensure_network(self) -> None:
        try:
            self._client.networks.get(self.network_name)
            logger.debug("network %s already exists", self.network_name)
            return
        except NotFound:
            pass
        self._client.networks.create(
            self.network_name,
            driver="bridge",
            options={
                # Block direct internet egress; sandbox must use sbx-proxy.
                "com.docker.network.bridge.enable_ip_masquerade": "false",
            },
            attachable=True,
            labels={"managed-by": "agent-sandbox"},
        )
        logger.info(
            "created network %s (enable_ip_masquerade=false)", self.network_name
        )

    def _ensure_proxy(self) -> None:
        try:
            existing = self._client.containers.get(self.container_name)
            existing.reload()
            if existing.status == "running":
                self._container_id = existing.id
                logger.debug("proxy container %s already running", existing.short_id)
                return
            # Exists but not running — wipe and recreate so config is fresh.
            existing.remove(force=True)
        except NotFound:
            pass

        host_conf_path = self._write_squid_conf()

        try:
            container = self._client.containers.run(
                image=self._settings.proxy_image,
                detach=True,
                name=self.container_name,
                volumes={
                    str(host_conf_path): {
                        "bind": "/etc/squid/squid.conf",
                        "mode": "ro",
                    },
                },
                restart_policy={"Name": "unless-stopped"},
                labels={"managed-by": "agent-sandbox"},
            )
        except ImageNotFound:
            raise RuntimeError(
                f"proxy image {self._settings.proxy_image!r} not found locally; "
                f"pre-pull it with `docker pull {self._settings.proxy_image}` "
                f"or override PROXY_IMAGE in .env"
            ) from None

        net = self._client.networks.get(self.network_name)
        net.connect(container, aliases=[self.container_name])
        self._container_id = container.id

        # Give squid a moment to bind before sandboxes try to use it.
        # Proper health-check probe lands in Phase 6.
        time.sleep(2)

        logger.info(
            "started proxy container %s (image=%s)",
            container.short_id,
            self._settings.proxy_image,
        )

    def _write_squid_conf(self) -> Path:
        template = SQUID_CONF_TEMPLATE_PATH.read_text(encoding="utf-8")
        rendered = render_squid_conf(template, self.allowlist)

        host_dir = Path.home() / ".agent-sandbox"
        host_dir.mkdir(parents=True, exist_ok=True)
        host_conf = host_dir / "squid.conf"
        host_conf.write_text(rendered, encoding="utf-8")
        return host_conf
