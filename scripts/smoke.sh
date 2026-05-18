#!/usr/bin/env bash
# End-to-end smoke test for the Phase 1 walking skeleton.
#
# Prereqs:
#   - The sandbox image is built (see README).
#   - `uvicorn app.main:app --port 8080` is already running.
#   - .env defines SANDBOX_API_KEY (or set it inline below).
#
# Usage: bash scripts/smoke.sh

set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
KEY="${SANDBOX_API_KEY:-change-me-please}"
CONV="${CONV:-smoke-$$}"

echo "[1/4] healthz"
curl -fsS "$BASE/healthz" | tee /dev/stderr; echo

echo "[2/4] create session conv=$CONV"
curl -fsS -X POST "$BASE/v1/sessions" \
    -H "X-API-Key: $KEY" \
    -H "Content-Type: application/json" \
    -d "{\"conversation_id\":\"$CONV\"}" \
  | tee /dev/stderr; echo

echo "[3/4] exec 'print(1+2)' on $CONV"
curl -fsS -X POST "$BASE/v1/sessions/$CONV/exec" \
    -H "X-API-Key: $KEY" \
    -H "Content-Type: application/json" \
    -d '{"code": "x = 1 + 2\nprint(x)\nx", "timeout_s": 15}' \
  | tee /dev/stderr; echo

echo "[4/4] destroy $CONV"
curl -fsS -X DELETE "$BASE/v1/sessions/$CONV" \
    -H "X-API-Key: $KEY" \
  | tee /dev/stderr; echo

echo "smoke OK"
