"""Phase 2 security invariants — run against a real Colima/Docker daemon.

These tests skip unless:
  - docker daemon is reachable
  - the agent-sandbox image is built locally (or pullable)
  - the proxy image (ubuntu/squid by default) is available

Each test spins its own session so failures are isolated and re-runnable.
The per-test cost is ~2-4s (container create + kernel ready).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

import app.config as config_mod
from app.config import Settings
from app.models import CreateSessionRequest
from app.runtime.docker_runtime import DockerRuntime
from app.runtime.manager import SessionManager
from app.runtime.proxy import EgressProxy


def _docker_available() -> bool:
    try:
        rt = DockerRuntime(Settings())
        client = rt.client()
        ok = client.ping()
        return bool(ok)
    except Exception:
        return False


def _image_available(image: str) -> bool:
    try:
        rt = DockerRuntime(Settings())
        rt.client().images.get(image)
        return True
    except Exception:
        return False


def _settings() -> Settings:
    config_mod._settings = None  # type: ignore[attr-defined]
    return Settings()


pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(not _docker_available(), reason="docker daemon not reachable"),
    pytest.mark.skipif(
        not _image_available(_settings().sandbox_image),
        reason="sandbox image not built locally; run `docker build -t agent-sandbox:latest ...`",
    ),
]


@asynccontextmanager
async def _session_ctx():
    settings = _settings()
    runtime = DockerRuntime(settings)
    proxy = EgressProxy(settings, runtime.client())
    await proxy.start()
    runtime.attach_proxy(proxy)
    mgr = SessionManager(settings, runtime)
    try:
        session = await mgr.get_or_create(
            CreateSessionRequest(conversation_id=f"sec-{uuid.uuid4().hex[:8]}")
        )
        yield session, proxy
    finally:
        await mgr.shutdown()


async def test_direct_socket_egress_is_blocked() -> None:
    code = (
        "import socket\n"
        "s = socket.socket()\n"
        "s.settimeout(3)\n"
        "try:\n"
        "    s.connect(('1.1.1.1', 80))\n"
        "    print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('BLOCKED:', type(e).__name__)\n"
    )
    async with _session_ctx() as (session, _):
        r = await session.kernel.execute(code, timeout_s=20)
    assert "BLOCKED" in r.stdout, r


async def test_rootfs_is_read_only() -> None:
    code = (
        "try:\n"
        "    open('/etc/hostname', 'w').write('hacked')\n"
        "    print('WROTE')\n"
        "except OSError as e:\n"
        "    print('BLOCKED:', e.errno, e.strerror)\n"
    )
    async with _session_ctx() as (session, _):
        r = await session.kernel.execute(code, timeout_s=15)
    assert "BLOCKED" in r.stdout, r


async def test_setuid_root_is_denied() -> None:
    code = (
        "import os\n"
        "try:\n"
        "    os.setuid(0)\n"
        "    print('SETUID_OK')\n"
        "except PermissionError as e:\n"
        "    print('BLOCKED:', e.errno)\n"
    )
    async with _session_ctx() as (session, _):
        r = await session.kernel.execute(code, timeout_s=15)
    assert "BLOCKED" in r.stdout, r


async def test_nproc_ulimit_applied() -> None:
    code = (
        "import re\n"
        "with open('/proc/self/limits') as f:\n"
        "    text = f.read()\n"
        "m = re.search(r'Max processes\\s+(\\d+)\\s+(\\d+)', text)\n"
        "print('nproc:', m.group(1) if m else 'missing')\n"
    )
    async with _session_ctx() as (session, _):
        r = await session.kernel.execute(code, timeout_s=15)
    # We set nproc=256 in DockerRuntime ulimits.
    assert "nproc: 256" in r.stdout, r


async def test_egress_to_unlisted_host_blocked_by_proxy() -> None:
    async with _session_ctx() as (session, proxy):
        if not proxy.proxy_ready:
            pytest.skip("egress proxy is not ready in this environment")
        code = (
            "import urllib.request, urllib.error\n"
            "try:\n"
            "    urllib.request.urlopen('https://example.com', timeout=8).read()\n"
            "    print('FETCHED')\n"
            "except Exception as e:\n"
            "    print('BLOCKED:', type(e).__name__)\n"
        )
        r = await session.kernel.execute(code, timeout_s=25)
    assert "BLOCKED" in r.stdout, r


async def test_egress_to_pypi_works_via_proxy() -> None:
    async with _session_ctx() as (session, proxy):
        if not proxy.proxy_ready:
            pytest.skip("egress proxy is not ready in this environment")
        code = (
            "import urllib.request\n"
            "try:\n"
            "    body = urllib.request.urlopen('https://pypi.org/simple/', timeout=15).read()\n"
            "    print('FETCHED', len(body) > 100)\n"
            "except Exception as e:\n"
            "    print('FAILED:', type(e).__name__, e)\n"
        )
        r = await session.kernel.execute(code, timeout_s=30)
    assert "FETCHED True" in r.stdout, r
