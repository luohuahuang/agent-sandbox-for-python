#!/usr/bin/env bash
# run_local.sh — build & run agent-sandbox on macOS (Colima + docker CLI).
#
# What it does (idempotent — safe to re-run):
#   1. Checks prerequisites (colima, docker, python3)
#   2. Starts Colima if not already running
#   3. Builds all sandbox images found in docker/Dockerfile.sandbox*
#      (skips images that are already up to date)
#   4. Pulls ubuntu/squid:latest if missing
#   5. Creates .venv + installs deps if needed
#   6. Writes .env from .env.example if missing
#   7. Starts uvicorn in the foreground (Ctrl-C to stop)
#
# Image naming convention (mirrors deploy_linux.sh):
#   docker/Dockerfile.sandbox                 → agent-sandbox:latest
#   docker/Dockerfile.sandbox.<suffix>        → agent-sandbox:<suffix>
#
# Environment overrides:
#   BIND_PORT=8080          port uvicorn listens on (default: 8080)
#   COLIMA_CPU=4            vCPUs for the Colima VM  (default: 4)
#   COLIMA_MEM=6            GiB RAM for the VM       (default: 6)
#   REBUILD=1               force rebuild of ALL sandbox images
#   SANDBOX_API_KEY=...     written to .env on first run only
#
# Usage:
#   bash scripts/run_local.sh           # start (blocks; Ctrl-C to stop)
#   REBUILD=1 bash scripts/run_local.sh # rebuild all images then start

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BIND_PORT="${BIND_PORT:-8080}"
COLIMA_CPU="${COLIMA_CPU:-4}"
COLIMA_MEM="${COLIMA_MEM:-6}"
COLIMA_SOCK="$HOME/.colima/default/docker.sock"

CYAN=$'\033[1;36m'
GREEN=$'\033[1;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[1;31m'
NC=$'\033[0m'

log()  { printf '%s[ run ]%s %s\n' "$CYAN"   "$NC" "$*"; }
ok()   { printf '%s[  ok ]%s %s\n' "$GREEN"  "$NC" "$*"; }
warn() { printf '%s[warn ]%s %s\n' "$YELLOW" "$NC" "$*"; }
fail() { printf '%s[fail ]%s %s\n' "$RED"    "$NC" "$*" >&2; exit 1; }

# ─────────────────────────────────────────────────────────────────────────────
# 1. prerequisites
# ─────────────────────────────────────────────────────────────────────────────
check_prereqs() {
    log "checking prerequisites"
    command -v colima >/dev/null 2>&1 || fail "colima not found — install with: brew install colima docker"
    command -v docker  >/dev/null 2>&1 || fail "docker CLI not found — install with: brew install docker"
    command -v python3 >/dev/null 2>&1 || fail "python3 not found — install with: brew install python@3.11"
    ok "colima, docker, python3 present"
}

# ─────────────────────────────────────────────────────────────────────────────
# 2. Colima
# ─────────────────────────────────────────────────────────────────────────────
start_colima() {
    log "checking Colima VM"
    if colima status 2>/dev/null | grep -q "^colima is running"; then
        ok "Colima already running"
        return
    fi
    log "starting Colima (cpu=$COLIMA_CPU mem=${COLIMA_MEM}GiB) — first start may take ~30s"
    colima start --cpu "$COLIMA_CPU" --memory "$COLIMA_MEM"
    ok "Colima started"
}

# ─────────────────────────────────────────────────────────────────────────────
# 3. sandbox images — auto-scan docker/Dockerfile.sandbox*
# ─────────────────────────────────────────────────────────────────────────────
_image_epoch() {
    # Return the Unix timestamp of when <image> was last tagged, or 0.
    local image="$1"
    local ts
    ts=$(docker image inspect "$image" --format '{{.Metadata.LastTagTime}}' 2>/dev/null || echo "")
    [ -z "$ts" ] && { echo 0; return; }
    python3 -c "
from datetime import datetime, timezone
import sys
s = sys.argv[1].split('.')[0].rstrip('Z')
try:
    dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    print(int(dt.timestamp()))
except Exception:
    print(0)
" "$ts"
}

_file_epoch() {
    python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$1"
}

build_images() {
    export DOCKER_HOST="unix://$COLIMA_SOCK"

    local found=0
    local dockerfile fname tag image needs_build img_epoch file_epoch

    for dockerfile in "$REPO_DIR"/docker/Dockerfile.sandbox*; do
        [ -f "$dockerfile" ] || continue
        found=$((found + 1))
        fname=$(basename "$dockerfile")

        if [ "$fname" = "Dockerfile.sandbox" ]; then
            tag="latest"
        else
            tag="${fname#Dockerfile.sandbox.}"
        fi
        image="agent-sandbox:$tag"

        needs_build=0
        if [ "${REBUILD:-0}" = "1" ]; then
            log "  REBUILD=1 — forcing rebuild of $image"
            needs_build=1
        elif ! docker image inspect "$image" >/dev/null 2>&1; then
            log "  $image not found — building"
            needs_build=1
        else
            img_epoch=$(_image_epoch "$image")
            file_epoch=$(_file_epoch "$dockerfile")
            if [ "$file_epoch" -gt "$img_epoch" ]; then
                log "  $fname changed since last build — rebuilding $image"
                needs_build=1
            fi
        fi

        if [ "$needs_build" = "1" ]; then
            log "  building $image ..."
            docker build -t "$image" \
                -f "$dockerfile" \
                "$REPO_DIR/docker/"
            ok "$image built"
        else
            ok "$image up to date"
        fi
    done

    if [ "$found" -eq 0 ]; then
        warn "no Dockerfile.sandbox* found in $REPO_DIR/docker/ — skipping image builds"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 4. squid image
# ─────────────────────────────────────────────────────────────────────────────
pull_squid() {
    log "checking ubuntu/squid:latest"
    export DOCKER_HOST="unix://$COLIMA_SOCK"
    if ! docker image inspect ubuntu/squid:latest >/dev/null 2>&1; then
        log "  pulling ubuntu/squid:latest"
        docker pull ubuntu/squid:latest
    fi
    ok "ubuntu/squid:latest present"
}

# ─────────────────────────────────────────────────────────────────────────────
# 5. python venv + deps
# ─────────────────────────────────────────────────────────────────────────────
setup_venv() {
    log "checking python venv"
    cd "$REPO_DIR"
    if [ ! -x ".venv/bin/python" ]; then
        log "  creating .venv"
        python3 -m venv .venv
    fi
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -e ".[dev]"
    ok "venv at $REPO_DIR/.venv"
}

# ─────────────────────────────────────────────────────────────────────────────
# 6. .env
# ─────────────────────────────────────────────────────────────────────────────
setup_env() {
    log "checking .env"
    cd "$REPO_DIR"
    if [ ! -f ".env" ]; then
        cp ".env.example" ".env"
        local key="${SANDBOX_API_KEY:-$(python3 -c "import secrets; print(secrets.token_hex(32))")}"
        python3 -c "
import re, pathlib
p = pathlib.Path('.env')
p.write_text(re.sub(r'^SANDBOX_API_KEY=.*', 'SANDBOX_API_KEY=$key', p.read_text(), flags=re.M))
"
        ok "wrote .env with a fresh SANDBOX_API_KEY"
        warn "copy the same SANDBOX_API_KEY into digital-employee/app/.env"
    else
        ok ".env exists — leaving as-is"
    fi

    mkdir -p "$HOME/.agent-sandbox/ws"
    ok "workspace dir at $HOME/.agent-sandbox/ws"
}

# ─────────────────────────────────────────────────────────────────────────────
# 7. run
# ─────────────────────────────────────────────────────────────────────────────
run_server() {
    export DOCKER_HOST="unix://$COLIMA_SOCK"

    # Collect all built sandbox images for the summary.
    local images_list=""
    local dockerfile fname tag
    for dockerfile in "$REPO_DIR"/docker/Dockerfile.sandbox*; do
        [ -f "$dockerfile" ] || continue
        fname=$(basename "$dockerfile")
        tag=$( [ "$fname" = "Dockerfile.sandbox" ] && echo "latest" || echo "${fname#Dockerfile.sandbox.}" )
        images_list="${images_list}  agent-sandbox:${tag}\n"
    done

    cat <<EOF

${GREEN}agent-sandbox ready.${NC}

  url:       http://127.0.0.1:${BIND_PORT}
  healthz:   http://127.0.0.1:${BIND_PORT}/healthz
  api key:   $(grep SANDBOX_API_KEY "$REPO_DIR/.env" | cut -d= -f2)
  workspace: $HOME/.agent-sandbox/ws
  docker:    $DOCKER_HOST

  sandbox images:
$(printf "$images_list")
  Ctrl-C to stop.

EOF

    cd "$REPO_DIR"
    exec .venv/bin/uvicorn app.main:app \
        --host 127.0.0.1 \
        --port "$BIND_PORT" \
        --reload \
        --reload-dir app \
        --log-level info
}

# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
main() {
    check_prereqs
    start_colima
    build_images
    pull_squid
    setup_venv
    setup_env
    run_server
}

main "$@"
