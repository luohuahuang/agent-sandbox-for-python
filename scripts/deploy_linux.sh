#!/usr/bin/env bash
# agent-sandbox deploy script for cloud Linux hosts.
#
# Verified targets:
#   - AWS EC2 Amazon Linux 2023      (dnf, python3.11, docker)
#   - AWS EC2 Ubuntu 22.04 / 24.04   (apt, python3.11 via deadsnakes if needed)
#   - Alibaba Cloud Linux 4 LTS      (dnf, docker-engine)
#   - Rocky 9 / AlmaLinux 9 / RHEL 9 (dnf)
#   - Debian 12+                     (apt)
#
# Idempotent: safe to re-run. Each phase is a function; phases short-circuit
# when the postcondition is already met.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/luohuahuang/agent-sandbox-for-python/main/scripts/deploy_linux.sh -o deploy.sh
#   chmod +x deploy.sh
#   ./deploy.sh                  # bootstraps everything
#
# Environment overrides:
#   REPO_DIR=/path/to/clone        default: ~/agent-sandbox
#   BIND_ADDR=0.0.0.0              default: 127.0.0.1  (loopback only)
#   BIND_PORT=8080                 default: 8080
#   SERVICE_NAME=agent-sandbox     systemd unit name
#   PYTHON_BIN=python3.11          which Python to use
#   SANDBOX_API_KEY=...            if unset, a random hex32 is generated
#   DOCKER_REGISTRY_MIRROR=https://docker.m.daocloud.io
#                                  for CN-region ECS where docker.io is
#                                  unreachable; writes /etc/docker/daemon.json
#                                  and restarts the daemon
#   PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
#                                  CN PyPI mirror used when building the
#                                  sandbox image (build-time only — runtime
#                                  egress is still gated by the squid proxy)

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/luohuahuang/agent-sandbox-for-python.git}"
REPO_DIR="${REPO_DIR:-$HOME/agent-sandbox}"
BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
BIND_PORT="${BIND_PORT:-8080}"
SERVICE_NAME="${SERVICE_NAME:-agent-sandbox}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

CYAN=$'\033[1;36m'
GREEN=$'\033[1;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[1;31m'
NC=$'\033[0m'

log()  { printf '%s[deploy]%s %s\n' "$CYAN"   "$NC" "$*"; }
ok()   { printf '%s[ ok ]%s   %s\n' "$GREEN"  "$NC" "$*"; }
warn() { printf '%s[warn]%s   %s\n' "$YELLOW" "$NC" "$*"; }
fail() { printf '%s[fail]%s   %s\n' "$RED"    "$NC" "$*" >&2; exit 1; }

# ───────────────────────────────────────────────────────────────────────────
# preconditions
# ───────────────────────────────────────────────────────────────────────────

[ "$EUID" -eq 0 ] && fail "do not run as root — run as a regular user; sudo is used internally where needed"
command -v sudo >/dev/null 2>&1 || fail "sudo is required"
[ -f /etc/os-release ] || fail "/etc/os-release not found — is this Linux?"

# ───────────────────────────────────────────────────────────────────────────
# distro detection — picks dnf or apt for Phase 1
# ───────────────────────────────────────────────────────────────────────────
detect_distro() {
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-unknown}"
    DISTRO_PRETTY="${PRETTY_NAME:-$DISTRO_ID}"
    case "$DISTRO_ID" in
        amzn|alinux|rhel|centos|rocky|almalinux|fedora|ol)
            PKG_MGR=dnf
            ;;
        ubuntu|debian|pop)
            PKG_MGR=apt
            ;;
        *)
            if command -v dnf >/dev/null 2>&1; then
                PKG_MGR=dnf
            elif command -v apt-get >/dev/null 2>&1; then
                PKG_MGR=apt
            else
                fail "unsupported distro '$DISTRO_ID'; only dnf and apt based systems are supported"
            fi
            ;;
    esac
    log "detected: $DISTRO_PRETTY  (pkg_mgr=$PKG_MGR)"
}

# ───────────────────────────────────────────────────────────────────────────
# Phase 1 — system packages (git, python3.11, docker)
# ───────────────────────────────────────────────────────────────────────────
phase_system_deps() {
    log "Phase 1/7 — system packages"

    case "$PKG_MGR" in
        dnf)
            # AL2023 ships curl-minimal (provides curl); installing the full
            # `curl` package would conflict. Only install what's actually
            # missing.
            local need=()
            command -v git     >/dev/null 2>&1 || need+=(git)
            command -v curl    >/dev/null 2>&1 || need+=(curl)
            command -v openssl >/dev/null 2>&1 || need+=(openssl)
            if [ ${#need[@]} -gt 0 ]; then
                sudo dnf install -y "${need[@]}" >/dev/null
            fi
            if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
                log "  installing $PYTHON_BIN via dnf ..."
                sudo dnf install -y python3.11 >/dev/null \
                    || fail "couldn't install $PYTHON_BIN; check dnf repos"
                # python3.11-pip is a separate package on some distros, bundled on others.
                sudo dnf install -y python3.11-pip >/dev/null 2>&1 || true
            fi
            if ! command -v docker >/dev/null 2>&1; then
                log "  installing docker via dnf ..."
                # AL2023 / Rocky / Alma / RHEL 9 → package name is "docker"
                # ACL4 → package name is "docker-engine"
                sudo dnf install -y docker >/dev/null 2>&1 || \
                    sudo dnf install -y docker-engine >/dev/null || \
                    fail "couldn't install docker via dnf"
            fi
            ;;
        apt)
            export DEBIAN_FRONTEND=noninteractive
            sudo apt-get update -qq
            local need=()
            command -v git     >/dev/null 2>&1 || need+=(git)
            command -v curl    >/dev/null 2>&1 || need+=(curl)
            command -v openssl >/dev/null 2>&1 || need+=(openssl)
            need+=(ca-certificates)  # always idempotent; covers cert refreshes
            sudo apt-get install -y -q "${need[@]}" >/dev/null
            if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
                log "  installing $PYTHON_BIN via apt ..."
                # Ubuntu 24.04 has python3.12 default but python3.11 in repos;
                # Ubuntu 22.04 needs the deadsnakes PPA for 3.11.
                if ! sudo apt-get install -y -q python3.11 python3.11-venv >/dev/null 2>&1; then
                    log "  python3.11 not in default repos; adding deadsnakes PPA"
                    sudo apt-get install -y -q software-properties-common >/dev/null
                    sudo add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1
                    sudo apt-get update -qq
                    sudo apt-get install -y -q python3.11 python3.11-venv >/dev/null \
                        || fail "couldn't install $PYTHON_BIN via apt or deadsnakes"
                fi
            fi
            if ! command -v docker >/dev/null 2>&1; then
                log "  installing docker via apt ..."
                sudo apt-get install -y -q docker.io >/dev/null \
                    || fail "couldn't install docker.io via apt"
            fi
            ;;
    esac
    ok "$($PYTHON_BIN --version)"
    ok "docker present"

    sudo systemctl enable --now docker >/dev/null
    ok "docker daemon enabled + running"

    # Registry mirror — required on CN-region ECS where docker.io is blocked.
    if [ -n "${DOCKER_REGISTRY_MIRROR:-}" ]; then
        if [ ! -f /etc/docker/daemon.json ]; then
            log "  writing /etc/docker/daemon.json with mirror $DOCKER_REGISTRY_MIRROR"
            sudo mkdir -p /etc/docker
            sudo tee /etc/docker/daemon.json > /dev/null <<EOF
{
  "registry-mirrors": ["$DOCKER_REGISTRY_MIRROR"]
}
EOF
            sudo systemctl restart docker
            ok "registry mirror configured"
        elif sudo grep -q "$DOCKER_REGISTRY_MIRROR" /etc/docker/daemon.json; then
            ok "registry mirror already in daemon.json"
        else
            warn "/etc/docker/daemon.json exists and does not contain $DOCKER_REGISTRY_MIRROR"
            warn "  merge it in by hand, then 'sudo systemctl restart docker'"
        fi
    fi

    if ! id -nG "$USER" | grep -qw docker; then
        log "  adding $USER to docker group ..."
        sudo usermod -aG docker "$USER"
        warn "you were just added to the 'docker' group; if the next phase says"
        warn "  permission denied, log out + back in (or run 'newgrp docker')"
        warn "  then re-run this script — it will skip what's already done"
    fi
}

# ───────────────────────────────────────────────────────────────────────────
# Phase 2 — docker access check
# ───────────────────────────────────────────────────────────────────────────
phase_docker_check() {
    log "Phase 2/7 — docker access"
    if docker ps >/dev/null 2>&1; then
        ok "docker reachable"
        return
    fi
    # Most common case: Phase 1 just added us to the docker group, but the
    # current shell doesn't have that membership active yet. Re-exec the
    # script under `sg docker` so we pick up the group without forcing the
    # user to log out + back in. SG_REEXEC guards against infinite loops.
    if id -nG "$USER" | grep -qw docker; then
        if [ -z "${SG_REEXEC:-}" ] && command -v sg >/dev/null 2>&1; then
            log "  current shell lacks the docker group; re-executing under 'sg docker' ..."
            export SG_REEXEC=1
            exec sg docker -c "$(readlink -f "$0")"
        fi
        fail "docker ps failed even after re-exec; log out and SSH back in, then re-run"
    fi
    fail "docker ps failed and \$USER is not in the docker group — Phase 1 incomplete?"
}

# ───────────────────────────────────────────────────────────────────────────
# Phase 3 — clone or update the repo
# ───────────────────────────────────────────────────────────────────────────
phase_repo() {
    log "Phase 3/7 — repo at $REPO_DIR"
    if [ ! -d "$REPO_DIR/.git" ]; then
        git clone "$REPO_URL" "$REPO_DIR"
    else
        (cd "$REPO_DIR" && git pull --ff-only)
    fi
    ok "repo on $(cd "$REPO_DIR" && git rev-parse --short HEAD)"
}

# ───────────────────────────────────────────────────────────────────────────
# Phase 4 — python venv + project install
# ───────────────────────────────────────────────────────────────────────────
phase_venv() {
    log "Phase 4/7 — python venv + deps"
    cd "$REPO_DIR"
    if [ ! -x ".venv/bin/python" ]; then
        "$PYTHON_BIN" -m venv .venv
    fi
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -e ".[dev]"
    ok "venv at $REPO_DIR/.venv"

    # Quick sanity: run unit tests (no docker required) — ~1s
    if .venv/bin/python -m pytest tests/unit -q >/dev/null 2>&1; then
        ok "unit tests pass"
    else
        warn "unit tests failed — check '.venv/bin/python -m pytest tests/unit -v'"
    fi
}

# ───────────────────────────────────────────────────────────────────────────
# Phase 5 — sandbox image + squid image
# ───────────────────────────────────────────────────────────────────────────
phase_images() {
    log "Phase 5/7 — docker images"

    if ! docker image inspect agent-sandbox:latest >/dev/null 2>&1; then
        log "  building agent-sandbox:latest (~2-3 min on first run)"
        local build_args=()
        if [ -n "${PIP_INDEX_URL:-}" ]; then
            build_args+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL")
            # Derive the trusted host (skip cert checks for http mirrors).
            local host
            host=$(echo "$PIP_INDEX_URL" | awk -F[/:] '{print $4}')
            [ -n "$host" ] && build_args+=(--build-arg "PIP_TRUSTED_HOST=$host")
            log "  using PyPI mirror: $PIP_INDEX_URL"
        fi
        docker build "${build_args[@]}" -t agent-sandbox:latest \
            -f "$REPO_DIR/docker/Dockerfile.sandbox" \
            "$REPO_DIR/docker/" >/dev/null
    fi
    ok "agent-sandbox:latest present"

    if ! docker image inspect ubuntu/squid:latest >/dev/null 2>&1; then
        log "  pulling ubuntu/squid:latest"
        docker pull ubuntu/squid:latest >/dev/null
    fi
    ok "ubuntu/squid:latest present"
}

# ───────────────────────────────────────────────────────────────────────────
# Phase 6 — .env (random API key + workspace dir)
# ───────────────────────────────────────────────────────────────────────────
phase_env() {
    log "Phase 6/7 — .env + workspace dir"
    local envfile="$REPO_DIR/.env"
    if [ ! -f "$envfile" ]; then
        cp "$REPO_DIR/.env.example" "$envfile"
        local key="${SANDBOX_API_KEY:-$(openssl rand -hex 32)}"
        # Replace the SANDBOX_API_KEY line in-place
        sed -i "s|^SANDBOX_API_KEY=.*|SANDBOX_API_KEY=$key|" "$envfile"
        ok "wrote $envfile with a fresh SANDBOX_API_KEY"
        warn "the digital-employee side must use the SAME key — see $envfile"
    else
        ok ".env exists, leaving as-is"
    fi

    # Workspace dir lives under $HOME so bind mounts work regardless of distro.
    mkdir -p "$HOME/.agent-sandbox/ws"
    ok "workspace dir at $HOME/.agent-sandbox/ws"
}

# ───────────────────────────────────────────────────────────────────────────
# Phase 7 — systemd unit
# ───────────────────────────────────────────────────────────────────────────
phase_systemd() {
    log "Phase 7/7 — systemd unit ($SERVICE_NAME.service)"
    local unit="/etc/systemd/system/$SERVICE_NAME.service"
    sudo tee "$unit" > /dev/null <<EOF
[Unit]
Description=agent-sandbox gateway (per-session Jupyter kernel in Docker)
Documentation=https://github.com/luohuahuang/agent-sandbox-for-python
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
User=$USER
Group=docker
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/uvicorn app.main:app --host $BIND_ADDR --port $BIND_PORT
Restart=on-failure
RestartSec=5
# Avoid log spam when re-creating sandbox containers on each session.
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

    # Restart to pick up any code/env changes on re-deploy.
    sudo systemctl restart "$SERVICE_NAME"
    ok "systemd service started"
}

# ───────────────────────────────────────────────────────────────────────────
# Verify
# ───────────────────────────────────────────────────────────────────────────
phase_verify() {
    log "verifying gateway ..."
    # Wait up to ~30s for lifespan (squid pull / proxy bootstrap) to finish.
    local i=0
    while ! curl -fsS "http://$BIND_ADDR:$BIND_PORT/healthz" >/dev/null 2>&1; do
        i=$((i+1))
        if [ "$i" -gt 30 ]; then
            fail "gateway not responding after 30s; check 'journalctl -u $SERVICE_NAME -n 80 --no-pager'"
        fi
        sleep 1
    done
    local body
    body=$(curl -fsS "http://$BIND_ADDR:$BIND_PORT/healthz")
    ok "healthz: $body"
}

# ───────────────────────────────────────────────────────────────────────────
# main
# ───────────────────────────────────────────────────────────────────────────
main() {
    detect_distro
    phase_system_deps
    phase_docker_check
    phase_repo
    phase_venv
    phase_images
    phase_env
    phase_systemd
    phase_verify

    cat <<EOF

${GREEN}deploy complete.${NC}

  service:   ${SERVICE_NAME}.service
  bind:      http://${BIND_ADDR}:${BIND_PORT}
  repo:      ${REPO_DIR}
  workspace: ${HOME}/.agent-sandbox/ws
  api key:   see ${REPO_DIR}/.env (SANDBOX_API_KEY=...)

useful commands:
  sudo systemctl status  ${SERVICE_NAME}
  sudo systemctl restart ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f
  curl http://${BIND_ADDR}:${BIND_PORT}/healthz

next steps:
  - put the same SANDBOX_API_KEY into digital-employee's app/.env
  - smoke test:  bash ${REPO_DIR}/scripts/smoke_basic.sh
  - security test:  bash ${REPO_DIR}/scripts/smoke_security.sh

resource tuning:
  This host has $(nproc) vCPU(s) and $(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo) MiB RAM.
  Defaults in .env assume a beefier box; on small / shared hosts adjust:
    MAX_SESSIONS    (default 32 — set to ~ (RAM_MiB - 2048) / MEM_LIMIT_MB)
    MEM_LIMIT_MB    (default 1024 per sandbox)
    CPU_NANOS       (default 2000000000 = 2 vCPU per sandbox)
  After editing .env: sudo systemctl restart ${SERVICE_NAME}

EOF
}

main "$@"
