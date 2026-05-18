# agent-sandbox

A standalone HTTP service that runs LLM-generated code inside **per-session Docker containers** with a **Jupyter kernel** inside. Designed to be called by the Digital Employee platform's tool framework as an L2 (Mentor-approval) tool, but usable from any client.

## Status

Phase 1 — walking skeleton. See `/Users/huanglh/.claude/plans/moonlit-knitting-bee.md` for the full implementation plan.

What Phase 1 ships:

- `POST /v1/sessions`, `POST /v1/sessions/{id}/exec` (non-streaming), `DELETE /v1/sessions/{id}`, `GET /healthz`.
- Per-conversation idempotent session creation.
- Jupyter kernel inside a container, talked to via `jupyter_client` over loopback-published ZMQ ports.
- Workspace bind-mount at `/workspace`.
- Hardened container flags, egress allowlist, audit DB, SSE streaming, metrics, reaper — **later phases.**

## Quickstart (developer, macOS + Colima)

We target the **Colima + docker CLI** stack (no Docker Desktop license).

```bash
# 0. One-time: install and start Colima
brew install colima docker
colima start                       # starts a Lima VM; sets the docker CLI context

# 1. Python deps
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# 2. Sandbox container image (built into the Colima VM)
docker build -t agent-sandbox:latest -f docker/Dockerfile.sandbox docker/

# 3. Environment
cp .env.example .env  # set SANDBOX_API_KEY at minimum

# 4. Run the gateway. The runtime auto-discovers Colima's docker socket
#    via `docker context inspect`; no DOCKER_HOST export needed.
uvicorn app.main:app --reload --port 8080

# 5. Smoke test (separate terminal)
SANDBOX_API_KEY=<your-key> bash scripts/smoke.sh
```

### Colima notes

- **Workspace mounts**: Colima's Lima VM only bind-mounts `/Users` writable
  by default. `WORKSPACE_ROOT` defaults to `~/.agent-sandbox/ws`, which is
  under `/Users` and thus visible inside the VM. If you change it to a path
  outside `/Users`, start Colima with an extra writable mount:
  `colima start --mount /var/lib/agent-sandbox:w`.
- **Docker socket discovery**: `docker.from_env()` (the SDK) only reads
  `DOCKER_HOST`; it does not consult docker CLI contexts. This service
  shells out to `docker context inspect` on startup to find the active
  endpoint, then falls back to `~/.colima/default/docker.sock`. If both
  fail, set `DOCKER_HOST=unix:///Users/<you>/.colima/default/docker.sock`.
- **Resource ceiling**: the per-container `mem_limit` and `nano_cpus` we
  request must fit inside Colima's VM. Default Colima gives 2 CPU / 2 GiB;
  bump it via `colima start --cpu 4 --memory 6` before running heavier
  workloads.

## API (Phase 1)

All requests need `X-API-Key: <SANDBOX_API_KEY>`.

```http
POST /v1/sessions
{ "conversation_id": "conv-123" }
-> { "session_id": "conv-123", "container_id": "...", "workspace_path": "...", "created_at": "..." }

POST /v1/sessions/conv-123/exec
{ "code": "x = 1\nprint('hello')", "timeout_s": 30 }
-> { "audit_id": "...", "ok": true, "exit_reason": "ok", "duration_ms": 42,
     "stdout": "hello\n", "stderr": "", "result_preview": null, "resource_peak": null }

DELETE /v1/sessions/conv-123
-> { "stopped": true }
```

## Layout

```
app/        FastAPI gateway code
docker/     Sandbox container image
scripts/    Smoke / dev scripts
tests/      unit / integration / security
data/       Audit DB (gitignored)
```
