"""Integration tests for the Files API.

Requires:
  - docker daemon reachable (Colima or Docker Desktop)
  - agent-sandbox:latest image built locally
  - ubuntu/squid:latest image pulled (or PROXY_IMAGE overridden)

Module-scoped client → lifespan fires once. Each test creates its own
session and tears it down on exit.
"""

from __future__ import annotations

import contextlib
import io
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
    pytest.mark.skipif(not _docker_available(), reason="docker daemon not reachable"),
    pytest.mark.skipif(
        not _image_available(_fresh_settings().sandbox_image),
        reason="sandbox image not built locally",
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
    config_mod._settings = None  # type: ignore[attr-defined]
    application = create_app()
    with TestClient(application) as c:
        c.headers["X-API-Key"] = api_key
        yield c


def _new_conv(prefix: str = "files") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@contextlib.contextmanager
def _session(client: TestClient, prefix: str = "files") -> Iterator[str]:
    """Create a session, yield its id, destroy on exit."""
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


def test_upload_csv_then_pandas_analysis(client: TestClient) -> None:
    """Upload CSV → exec pandas analysis → verify mean/max in stdout."""
    csv_content = b"name,score\nAlice,95\nBob,82\nCarol,88\n"
    with _session(client, "csv") as conv:
        r = client.post(
            f"/v1/sessions/{conv}/files",
            files={"file": ("data.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["path"] == "data.csv"
        assert body["size_bytes"] == len(csv_content)

        r = client.post(
            f"/v1/sessions/{conv}/exec",
            json={
                "code": (
                    "import pandas as pd\n"
                    "df = pd.read_csv('/workspace/data.csv')\n"
                    "print(df['score'].mean())\n"
                    "print(df['score'].max())\n"
                ),
                "timeout_s": 30,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True, body["stderr"]
        # mean of 95, 82, 88 = 88.333...
        assert "88.33" in body["stdout"]
        assert "95" in body["stdout"]


def test_exec_matplotlib_then_download_png(client: TestClient) -> None:
    """Exec saves chart.png → download → verify PNG magic bytes."""
    with _session(client, "png") as conv:
        r = client.post(
            f"/v1/sessions/{conv}/exec",
            json={
                "code": (
                    "import matplotlib\n"
                    "matplotlib.use('Agg')\n"
                    "import matplotlib.pyplot as plt\n"
                    "plt.plot([1, 2, 3], [4, 5, 6])\n"
                    "plt.savefig('/workspace/chart.png')\n"
                    "print('saved')\n"
                ),
                "timeout_s": 30,
            },
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True, r.json()["stderr"]

        r = client.get(f"/v1/sessions/{conv}/files/chart.png")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_upload_requirements_then_pip_install(client: TestClient) -> None:
    """Upload requirements.txt → exec pip install -r → verify returncode 0."""
    req_content = b"requests==2.32.3\n"
    with _session(client, "pip") as conv:
        r = client.post(
            f"/v1/sessions/{conv}/files",
            files={"file": ("requirements.txt", io.BytesIO(req_content), "text/plain")},
        )
        assert r.status_code == 201, r.text

        r = client.post(
            f"/v1/sessions/{conv}/exec",
            json={
                "code": (
                    "import subprocess, sys\n"
                    "result = subprocess.run(\n"
                    "    [sys.executable, '-m', 'pip', 'install', '-r',\n"
                    "     '/workspace/requirements.txt'],\n"
                    "    capture_output=True, text=True\n"
                    ")\n"
                    "print('returncode:', result.returncode)\n"
                    "print(result.stdout[-300:] if result.stdout else '')\n"
                ),
                "timeout_s": 60,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True, body["stderr"]
        assert "returncode: 0" in body["stdout"]


def test_upload_log_then_grep_analysis(client: TestClient) -> None:
    """Upload log file → exec grep for ERROR lines → verify filter."""
    log_content = (
        b"2026-01-01 ERROR: disk full\n"
        b"2026-01-01 INFO: startup\n"
        b"2026-01-02 ERROR: oom\n"
        b"2026-01-02 INFO: shutdown\n"
    )
    with _session(client, "log") as conv:
        r = client.post(
            f"/v1/sessions/{conv}/files",
            files={"file": ("app.log", io.BytesIO(log_content), "text/plain")},
        )
        assert r.status_code == 201, r.text

        r = client.post(
            f"/v1/sessions/{conv}/exec",
            json={
                "code": (
                    "with open('/workspace/app.log') as f:\n"
                    "    errors = [l for l in f if 'ERROR' in l]\n"
                    "for e in errors:\n"
                    "    print(e, end='')\n"
                ),
                "timeout_s": 15,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "disk full" in body["stdout"]
        assert "oom" in body["stdout"]
        assert "startup" not in body["stdout"]


def test_list_files_shows_uploaded(client: TestClient) -> None:
    """Upload 2 files → list → verify both appear with correct metadata."""
    with _session(client, "list") as conv:
        for name, content in [("a.txt", b"hello"), ("b.txt", b"world")]:
            r = client.post(
                f"/v1/sessions/{conv}/files",
                files={"file": (name, io.BytesIO(content), "text/plain")},
            )
            assert r.status_code == 201, r.text

        r = client.get(f"/v1/sessions/{conv}/files")
        assert r.status_code == 200
        entries = {f["name"]: f for f in r.json()["files"]}
        assert "a.txt" in entries
        assert "b.txt" in entries
        assert entries["a.txt"]["size_bytes"] == 5
        assert entries["a.txt"]["is_dir"] is False


def test_path_traversal_upload_rejected(client: TestClient) -> None:
    """Upload with dest traversal path → expect 400."""
    with _session(client, "trav-up") as conv:
        r = client.post(
            f"/v1/sessions/{conv}/files",
            files={"file": ("evil.txt", io.BytesIO(b"evil"), "text/plain")},
            data={"dest": "../../../etc/evil.txt"},
        )
        assert r.status_code == 400


def test_path_traversal_download_rejected(client: TestClient) -> None:
    """Download with traversal path → expect 400 or 404."""
    with _session(client, "trav-dl") as conv:
        # TestClient normalises URLs, so the path param arrives as
        # the decoded segment after routing — still fails safe_path check.
        r = client.get(f"/v1/sessions/{conv}/files/../../../etc/passwd")
        assert r.status_code in {400, 404}


def test_download_nonexistent_returns_404(client: TestClient) -> None:
    """Download non-existent file → expect 404."""
    with _session(client, "nofile") as conv:
        r = client.get(f"/v1/sessions/{conv}/files/does-not-exist.txt")
        assert r.status_code == 404


def test_upload_over_size_limit_returns_413(client: TestClient) -> None:
    """Upload 51 MiB (> 50 MiB default limit) → expect 413."""
    big_data = b"x" * (51 * 1024 * 1024)
    with _session(client, "big") as conv:
        r = client.post(
            f"/v1/sessions/{conv}/files",
            files={"file": ("big.bin", io.BytesIO(big_data), "application/octet-stream")},
        )
        assert r.status_code == 413


def test_download_csv_produced_by_exec(client: TestClient) -> None:
    """Exec writes output.csv → download → verify content."""
    with _session(client, "gen-csv") as conv:
        r = client.post(
            f"/v1/sessions/{conv}/exec",
            json={
                "code": (
                    "import csv, pathlib\n"
                    "rows = [['x', 'y'], [1, 2], [3, 4]]\n"
                    "p = pathlib.Path('/workspace/output.csv')\n"
                    "with p.open('w', newline='') as f:\n"
                    "    csv.writer(f).writerows(rows)\n"
                    "print('written')\n"
                ),
                "timeout_s": 15,
            },
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

        r = client.get(f"/v1/sessions/{conv}/files/output.csv")
        assert r.status_code == 200
        text = r.text
        assert "x,y" in text
        assert "1,2" in text
        assert "3,4" in text


def test_delete_file(client: TestClient) -> None:
    """Upload → DELETE → GET → expect 404."""
    with _session(client, "del") as conv:
        r = client.post(
            f"/v1/sessions/{conv}/files",
            files={"file": ("todelete.txt", io.BytesIO(b"bye"), "text/plain")},
        )
        assert r.status_code == 201

        r = client.delete(f"/v1/sessions/{conv}/files/todelete.txt")
        assert r.status_code == 200
        assert r.json() == {"deleted": True}

        r = client.get(f"/v1/sessions/{conv}/files/todelete.txt")
        assert r.status_code == 404


def test_upload_to_subdirectory_with_dest(client: TestClient) -> None:
    """Upload with dest=subdir/file.txt → list ?dir=subdir → verify appears."""
    with _session(client, "subdir") as conv:
        r = client.post(
            f"/v1/sessions/{conv}/files",
            files={"file": ("data.txt", io.BytesIO(b"nested"), "text/plain")},
            data={"dest": "reports/data.txt"},
        )
        assert r.status_code == 201
        assert r.json()["path"] == "reports/data.txt"

        r = client.get(f"/v1/sessions/{conv}/files", params={"dir": "reports"})
        assert r.status_code == 200
        names = {f["name"] for f in r.json()["files"]}
        assert "data.txt" in names
