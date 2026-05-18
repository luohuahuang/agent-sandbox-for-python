#!/usr/bin/env bash
# Phase 2 security smoke tests via HTTP — curl + jq mirror of
# tests/security/test_phase2_smoke.py.
#
# Prereqs:
#   - gateway running:  uvicorn app.main:app --port 8080
#   - .env's SANDBOX_API_KEY matches what you export (or default below)
#   - the sandbox image and proxy image are pulled/built
#   - jq is installed (brew install jq)
#
# Usage: bash scripts/smoke_security.sh
# Exit code: 0 if all pass, otherwise the count of failures.

set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
KEY="${SANDBOX_API_KEY:-change-me-please}"

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
DIM=$'\033[2m'
CYAN=$'\033[0;36m'
NC=$'\033[0m'

PASS=0
FAIL=0

# --- preconditions --------------------------------------------------

command -v jq >/dev/null 2>&1 || { echo "this script needs jq (brew install jq)" >&2; exit 2; }
command -v uuidgen >/dev/null 2>&1 || { echo "this script needs uuidgen" >&2; exit 2; }
curl -fsS "$BASE/healthz" >/dev/null 2>&1 || { echo "gateway not reachable at $BASE" >&2; exit 2; }

# --- helpers --------------------------------------------------------

# Create a session. Returns 0 on success.
create_session() {
    local conv=$1
    curl -fsS -X POST "$BASE/v1/sessions" \
        -H "X-API-Key: $KEY" \
        -H "Content-Type: application/json" \
        -d "{\"conversation_id\":\"$conv\"}" >/dev/null
}

destroy_session() {
    local conv=$1
    curl -fsS -X DELETE "$BASE/v1/sessions/$conv" \
        -H "X-API-Key: $KEY" >/dev/null 2>&1 || true
}

# Submit code to a session. Echoes the full JSON response on stdout.
exec_code() {
    local conv=$1
    local code=$2
    local timeout=${3:-20}
    local payload
    payload=$(jq -nc --arg code "$code" --argjson timeout "$timeout" \
        '{code:$code, timeout_s:$timeout}')
    curl -fsS -X POST "$BASE/v1/sessions/$conv/exec" \
        -H "X-API-Key: $KEY" \
        -H "Content-Type: application/json" \
        -d "$payload"
}

# check NAME NEEDLE CODE
#   - spins a fresh session
#   - submits CODE (python)
#   - asserts NEEDLE is a substring of the kernel stdout
check() {
    local name=$1
    local needle=$2
    local code=$3
    local conv="smk-$(uuidgen | tr '[:upper:]' '[:lower:]' | cut -c1-8)"
    local resp stdout

    if ! create_session "$conv" 2>/dev/null; then
        printf '%sFAIL%s %s  %s— could not create session%s\n' "$RED" "$NC" "$name" "$DIM" "$NC"
        FAIL=$((FAIL+1)); return
    fi

    resp=$(exec_code "$conv" "$code" 2>/dev/null || echo '{}')
    destroy_session "$conv"

    stdout=$(printf '%s' "$resp" | jq -r '.stdout // ""')
    if printf '%s' "$stdout" | grep -q -- "$needle"; then
        printf '%sPASS%s %s\n' "$GREEN" "$NC" "$name"
        PASS=$((PASS+1))
    else
        printf '%sFAIL%s %s\n' "$RED" "$NC" "$name"
        printf '       expected substring: %s%s%s\n' "$CYAN" "$needle" "$NC"
        printf '       actual stdout:      %s\n' "$(printf '%s' "$stdout" | tr '\n' ' ' | head -c 240)"
        FAIL=$((FAIL+1))
    fi
}

# --- tests (mirror tests/security/test_phase2_smoke.py) -------------

check "direct socket egress is blocked" "BLOCKED" '
import socket
s = socket.socket()
s.settimeout(3)
try:
    s.connect(("1.1.1.1", 80))
    print("CONNECTED")
except OSError as e:
    print("BLOCKED:", type(e).__name__)
'

check "rootfs is read-only" "BLOCKED" '
try:
    open("/etc/hostname", "w").write("hacked")
    print("WROTE")
except OSError as e:
    print("BLOCKED:", e.errno, e.strerror)
'

check "setuid root is denied" "BLOCKED" '
import os
try:
    os.setuid(0)
    print("SETUID_OK")
except PermissionError as e:
    print("BLOCKED:", e.errno)
'

check "nproc ulimit applied" "nproc: 256" '
import re
with open("/proc/self/limits") as f:
    text = f.read()
m = re.search(r"Max processes\s+(\d+)\s+(\d+)", text)
print("nproc:", m.group(1) if m else "missing")
'

check "egress to unlisted host blocked by proxy" "BLOCKED" '
import urllib.request
try:
    urllib.request.urlopen("https://example.com", timeout=8).read()
    print("FETCHED")
except Exception as e:
    print("BLOCKED:", type(e).__name__)
'

check "egress to pypi works via proxy" "FETCHED True" '
import urllib.request
try:
    body = urllib.request.urlopen("https://pypi.org/simple/", timeout=15).read()
    print("FETCHED", len(body) > 100)
except Exception as e:
    print("FAILED:", type(e).__name__, e)
'

# --- summary --------------------------------------------------------

echo
if [ "$FAIL" -eq 0 ]; then
    printf '%sall %d passed%s\n' "$GREEN" "$PASS" "$NC"
else
    printf '%spassed: %d, failed: %d%s\n' "$RED" "$PASS" "$FAIL" "$NC"
fi
exit "$FAIL"
