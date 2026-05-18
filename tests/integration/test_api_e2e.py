"""End-to-end API tests — drive the FastAPI app over HTTP via TestClient.

Lifespan fires when the TestClient is entered, so the egress proxy and
docker runtime really come up; sandbox containers really start. These
tests cover the HTTP surface (auth, routes, response shapes, lifecycle)
that the security suite — which calls SessionManager directly — does not.

Requires:
  - docker daemon reachable (Colima or Docker Desktop)
  - agent-sandbox:latest image built locally
  - ubuntu/squid:latest image pulled (or PROXY_IMAGE overridden)

Module-scoped client → lifespan fires once; ~5-30 s warm-up depending on
whether the proxy container is already running. Each test creates its
own session and tears it down on exit.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import app.config as config_mod
from app.config import Settings
from app.main import create_app


def _docker_available() -> bool:
    try:
        from app.runtime.docker_runtime import DockerRuntime

        return bool(DockerRuntime(Settings()).client().ping())
    except Exception:
        return False


def _image_available(image: str) -> bool:
    try:
        from app.runtime.docker_runtime import DockerRuntime

        DockerRuntime(Settings()).client().images.get(image)
        return True
    except Exception:
        return False


def _fresh_settings() -> Settings:
    config_mod._settings = None  # type: ignore[attr-defined]
    return Settings()


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_available(), reason="docker daemon not reachable"
    ),
    pytest.mark.skipif(
        not _image_available(_fresh_settings().sandbox_image),
        reason="sandbox image not built locally (run `docker build -t agent-sandbox:latest ...`)",
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def api_key() -> str:
    return os.environ.get("SANDBOX_API_KEY", "change-me-please")


@pytest.fixture(scope="module")
def client(api_key: str) -> Iterator[TestClient]:
    """One TestClient shared by all tests in this module.

    Entering the context fires app lifespan (proxy + runtime startup).
    Exiting fires the lifespan exit (sessions destroyed, docker client closed).
    """
    config_mod._settings = None  # type: ignore[attr-defined]
    app = create_app()
    with TestClient(app) as c:
        c.headers["X-API-Key"] = api_key
        yield c


def _new_conv(prefix: str = "e2e") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@contextlib.contextmanager
def _session(client: TestClient, prefix: str = "e2e") -> Iterator[str]:
    """Create a session, hand back its id, destroy on exit."""
    conv = _new_conv(prefix)
    r = client.post("/v1/sessions", json={"conversation_id": conv})
    assert r.status_code == 201, f"create failed: {r.status_code} {r.text}"
    try:
        yield conv
    finally:
        client.delete(f"/v1/sessions/{conv}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_healthz_returns_status(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "degraded"}
    assert "docker" in body


def test_create_session_rejects_missing_api_key(client: TestClient) -> None:
    r = client.post(
        "/v1/sessions",
        json={"conversation_id": "should-not-be-created"},
        headers={"X-API-Key": ""},
    )
    assert r.status_code == 401


def test_create_exec_destroy_full_lifecycle(client: TestClient) -> None:
    conv = _new_conv("life")
    r = client.post("/v1/sessions", json={"conversation_id": conv})
    assert r.status_code == 201
    body = r.json()
    assert body["session_id"] == conv
    assert body["container_id"]
    assert body["workspace_path"]
    assert body["created_at"]

    r = client.post(
        f"/v1/sessions/{conv}/exec",
        json={"code": "print('hello from e2e')", "timeout_s": 15},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["exit_reason"] == "ok"
    assert body["stdout"] == "hello from e2e\n"
    assert body["stderr"] == ""
    assert body["error_name"] is None
    assert isinstance(body["duration_ms"], int)
    assert isinstance(body["audit_id"], str)

    r = client.delete(f"/v1/sessions/{conv}")
    assert r.status_code == 200
    assert r.json() == {"stopped": True}

    # second delete is idempotent — session is gone, but no error
    r = client.delete(f"/v1/sessions/{conv}")
    assert r.status_code == 200
    assert r.json() == {"stopped": False}


def test_create_session_is_idempotent_on_conversation_id(client: TestClient) -> None:
    conv = _new_conv("idem")
    r1 = client.post("/v1/sessions", json={"conversation_id": conv})
    r2 = client.post("/v1/sessions", json={"conversation_id": conv})
    try:
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["container_id"] == r2.json()["container_id"]
        assert r1.json()["session_id"] == r2.json()["session_id"] == conv
    finally:
        client.delete(f"/v1/sessions/{conv}")


def test_kernel_state_persists_across_execs(client: TestClient) -> None:
    with _session(client, "state") as conv:
        r1 = client.post(
            f"/v1/sessions/{conv}/exec",
            json={"code": "x = 41", "timeout_s": 10},
        )
        assert r1.json()["ok"] is True

        r2 = client.post(
            f"/v1/sessions/{conv}/exec",
            json={"code": "x + 1", "timeout_s": 10},
        )
        body = r2.json()
        assert body["ok"] is True
        # bare expression returns via execute_result → result_preview holds repr.
        assert body["result_preview"] == "42"


def test_python_exception_returns_error_shape(client: TestClient) -> None:
    with _session(client, "err") as conv:
        r = client.post(
            f"/v1/sessions/{conv}/exec",
            json={"code": "1 / 0", "timeout_s": 10},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["exit_reason"] == "error"
        assert body["error_name"] == "ZeroDivisionError"
        # The traceback is captured into stderr.
        assert "ZeroDivisionError" in body["stderr"]


def test_exec_on_unknown_session_returns_404(client: TestClient) -> None:
    r = client.post(
        "/v1/sessions/does-not-exist/exec",
        json={"code": "print(1)", "timeout_s": 5},
    )
    assert r.status_code == 404


def test_list_sessions_includes_created(client: TestClient) -> None:
    with _session(client, "list") as conv:
        r = client.get("/v1/sessions")
        assert r.status_code == 200
        ids = {s["session_id"] for s in r.json()}
        assert conv in ids


def test_get_session_detail(client: TestClient) -> None:
    with _session(client, "detail") as conv:
        r = client.get(f"/v1/sessions/{conv}")
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == conv
        assert body["status"] == "ready"
        assert body["container_id"]
        assert "idle_seconds" in body
