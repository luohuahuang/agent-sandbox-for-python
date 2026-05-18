"""Blocking jupyter_client wrapper exposed as async via to_thread.

The kernel runs inside a sandbox container; ZMQ ports are published to
127.0.0.1 on the host. Gateway connects as a remote client using the
HMAC key the gateway generated and passed to the kernel via env.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from dataclasses import dataclass, field

from jupyter_client.blocking import BlockingKernelClient

logger = logging.getLogger(__name__)


# Truncation caps for stored / returned strings.
STDOUT_CAP = 64 * 1024
STDERR_CAP = 64 * 1024
RESULT_CAP = 8 * 1024


@dataclass(slots=True)
class ExecResult:
    ok: bool
    exit_reason: str  # ok | timeout | interrupt | kernel_dead | error
    duration_ms: int
    stdout: str
    stderr: str
    result_preview: str | None
    error_name: str | None
    error_value: str | None = None


@dataclass(slots=True)
class _Buf:
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    result_preview: str | None = None
    error_name: str | None = None
    error_value: str | None = None


def _truncate(s: str, cap: int) -> str:
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n…[truncated; original {len(s)} chars]"


class KernelClient:
    """Wraps one BlockingKernelClient bound to a single sandbox kernel."""

    def __init__(
        self,
        host_ports: dict[str, int],
        kernel_key: str,
        *,
        host: str = "127.0.0.1",
    ) -> None:
        self._host_ports = host_ports
        self._kernel_key = kernel_key
        self._host = host
        self._kc: BlockingKernelClient | None = None

    async def start(self, ready_timeout_s: float = 20.0) -> None:
        def _connect() -> BlockingKernelClient:
            kc = BlockingKernelClient()
            kc.load_connection_info(
                {
                    "shell_port": self._host_ports["shell_port"],
                    "iopub_port": self._host_ports["iopub_port"],
                    "stdin_port": self._host_ports["stdin_port"],
                    "control_port": self._host_ports["control_port"],
                    "hb_port": self._host_ports["hb_port"],
                    "ip": self._host,
                    "key": self._kernel_key.encode("utf-8"),
                    "transport": "tcp",
                    "signature_scheme": "hmac-sha256",
                }
            )
            kc.start_channels()

            # wait_for_ready can spuriously fail if the kernel binds slowly; retry.
            deadline = time.monotonic() + ready_timeout_s
            last_exc: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    kc.wait_for_ready(timeout=2.0)
                    return kc
                except Exception as exc:
                    last_exc = exc
                    time.sleep(0.3)
            kc.stop_channels()
            raise TimeoutError(f"kernel not ready within {ready_timeout_s}s: {last_exc}")

        self._kc = await asyncio.to_thread(_connect)

    async def execute(self, code: str, timeout_s: float) -> ExecResult:
        if self._kc is None:
            raise RuntimeError("KernelClient.start() not called")
        return await asyncio.to_thread(self._execute_sync, code, timeout_s)

    def _execute_sync(self, code: str, timeout_s: float) -> ExecResult:
        assert self._kc is not None
        kc = self._kc
        start = time.monotonic()
        msg_id = kc.execute(code, store_history=False, allow_stdin=False)
        buf = _Buf()
        exit_reason = "ok"

        deadline = start + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Phase 1: no interrupt. The session manager owns timeout
                # remediation (sending SIGINT / destroying the container).
                exit_reason = "timeout"
                break

            try:
                msg = kc.get_iopub_msg(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            except Exception as exc:
                logger.warning("iopub recv error: %s", exc)
                exit_reason = "kernel_dead"
                break

            parent_id = msg.get("parent_header", {}).get("msg_id")
            if parent_id != msg_id:
                # Stale message from kernel boot or a prior exec; ignore.
                continue

            msg_type = msg.get("msg_type")
            content = msg.get("content", {})

            if msg_type == "stream":
                text = content.get("text", "")
                if content.get("name") == "stderr":
                    buf.stderr.append(text)
                else:
                    buf.stdout.append(text)

            elif msg_type == "execute_result":
                data = content.get("data") or {}
                tp = data.get("text/plain")
                if isinstance(tp, str):
                    buf.result_preview = tp

            elif msg_type == "display_data":
                data = content.get("data") or {}
                tp = data.get("text/plain")
                if isinstance(tp, str) and buf.result_preview is None:
                    buf.result_preview = tp

            elif msg_type == "error":
                buf.error_name = content.get("ename")
                buf.error_value = content.get("evalue")
                tb = content.get("traceback") or []
                if tb:
                    buf.stderr.append("\n".join(tb) + "\n")

            elif msg_type == "status":
                if content.get("execution_state") == "idle":
                    break

        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = _truncate("".join(buf.stdout), STDOUT_CAP)
        stderr = _truncate("".join(buf.stderr), STDERR_CAP)
        result_preview = (
            _truncate(buf.result_preview, RESULT_CAP) if buf.result_preview is not None else None
        )
        ok = buf.error_name is None and exit_reason == "ok"
        if buf.error_name is not None and exit_reason == "ok":
            exit_reason = "error"
        return ExecResult(
            ok=ok,
            exit_reason=exit_reason,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            result_preview=result_preview,
            error_name=buf.error_name,
            error_value=buf.error_value,
        )

    async def close(self) -> None:
        if self._kc is None:
            return

        def _stop() -> None:
            try:
                self._kc.stop_channels()  # type: ignore[union-attr]
            except Exception as exc:
                logger.debug("stop_channels error (ignored): %s", exc)

        await asyncio.to_thread(_stop)
        self._kc = None
