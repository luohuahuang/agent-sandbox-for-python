# agent-sandbox

A standalone HTTP service that runs LLM-generated code inside **per-session Docker containers** with a **Jupyter kernel** inside. Designed to be called by the Digital Employee platform's tool framework as an L2 (Mentor-approval) tool, but usable from any client.

## Status

Phase 2 — hardening + egress proxy. See the implementation plan for the
full phase breakdown.

What's in today:

- `POST /v1/sessions`, `POST /v1/sessions/{id}/exec` (non-streaming),
  `DELETE /v1/sessions/{id}`, `GET /v1/sessions`, `GET /healthz`.
- Per-conversation idempotent session creation; per-session asyncio lock
  serializes execs on one kernel; max-session cap.
- Jupyter kernel inside a sandbox container; gateway talks to it over
  loopback-published ZMQ ports.
- **Phase 2 hardening per container**: read-only rootfs, tmpfs for `/tmp`
  and `/home/sbx`, `cap_drop=ALL`, `no-new-privileges`, ulimits
  (`nofile=1024`, `nproc=256`), `mem_limit`, `nano_cpus`, `pids_limit`,
  `memswap_limit` = mem_limit (no swap).
- **Network isolation**: sandbox containers attach to `sbx-net`, an
  `--internal` bridge — no direct internet route.
- **Egress proxy**: `sbx-proxy` (stock `ubuntu/squid`) sidecar straddles
  `sbx-net` + the default bridge, applies a `dstdomain` allowlist
  rendered from `EGRESS_ALLOWLIST` (default: `pypi.org`,
  `files.pythonhosted.org`, `*.pythonhosted.org`). Sandbox containers
  see `HTTP_PROXY` / `HTTPS_PROXY` env pointing at `http://sbx-proxy:3128`.

Still deferred:

- SSE streaming (Phase 3), files API (Phase 4), sandbox-local audit DB
  + reaper (Phase 5-6), proper proxy readiness probe (Phase 6), CI
  (Phase 7).

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

# 2b. Pre-pull the egress proxy image (Phase 2)
docker pull ubuntu/squid:latest

# 3. Environment
cp .env.example .env  # set SANDBOX_API_KEY at minimum

# 4. Run the gateway. The runtime auto-discovers Colima's docker socket
#    via `docker context inspect`; no DOCKER_HOST export needed.
uvicorn app.main:app --reload --port 8080

# 5. Smoke test (separate terminal)
SANDBOX_API_KEY=<your-key> bash scripts/smoke_basic.sh
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
- **Resource ceiling**: each sandbox requests 1 GiB / 2 CPU, plus the
  proxy sidecar (~50 MiB). The Lima VM defaults to 2 CPU / 2 GiB — too
  tight for even one active sandbox + proxy. **Recommend**
  `colima start --cpu 4 --memory 6` before running.
- **First run side effects**: on startup the gateway creates a docker
  network `sbx-net` (internal bridge) and a long-running container
  `sbx-proxy`. They persist across gateway restarts. Tear down manually
  with `docker rm -f sbx-proxy && docker network rm sbx-net` if you need
  a clean slate.

## Phase 2 security invariants

These are enforced per container and verified by `tests/security/test_phase2_smoke.py`:

| Invariant | Mechanism |
|---|---|
| No direct internet egress | `--network sbx-net` (internal bridge); raw `socket.connect` to external IPs fails |
| Outbound only via allowlist | `HTTP_PROXY` → squid `dstdomain` ACL; `urlopen('https://example.com')` fails, `urlopen('https://pypi.org/simple/')` succeeds |
| Read-only rootfs | `read_only=True`; only `/workspace`, `/tmp`, `/home/sbx` are writable (tmpfs) |
| No capability escalation | `cap_drop=ALL` + `no-new-privileges:true`; `os.setuid(0)` raises PermissionError |
| Process count cap | `ulimit nproc=256`, `pids_limit=256` — fork bombs hit the cap |
| Memory cap | `mem_limit` + `memswap_limit` equal; OOM kills the kernel, not the host |

Run the security suite with `pytest tests/security -q`. It requires both
`agent-sandbox:latest` and `ubuntu/squid:latest` to be present locally.

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
