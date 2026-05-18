#!/usr/bin/env bash
set -euo pipefail

: "${KERNEL_KEY:?KERNEL_KEY env var is required}"
CONN_FILE="${KERNEL_CONNECTION_FILE:-/home/sbx/kernel.json}"

mkdir -p "$(dirname "$CONN_FILE")"

cat >"$CONN_FILE" <<EOF
{
  "shell_port": 5555,
  "iopub_port": 5556,
  "stdin_port": 5557,
  "control_port": 5558,
  "hb_port": 5559,
  "ip": "0.0.0.0",
  "key": "${KERNEL_KEY}",
  "transport": "tcp",
  "signature_scheme": "hmac-sha256",
  "kernel_name": "python3"
}
EOF

exec python -m ipykernel_launcher -f "$CONN_FILE"
