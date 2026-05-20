from __future__ import annotations

"""
agent-sandbox cookbook — LLM Pipeline Demo

Full capability chain:
  1. Preview local CSV and send to LLM (Anthropic or OpenAI).
  2. LLM generates a pandas pipeline: clean → analyse → write output files.
  3. Upload CSV to sandbox /workspace/ via Files API.
  4. Execute generated code inside the sandbox container.
  5. Download result files back to local artifacts/.

This demo is intentionally structured as clear, labelled sections so it can be
walked through step-by-step in a demo setting.
"""

import json
import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv


# ============================================================================
# Environment
# ============================================================================

def bootstrap() -> None:
    """Load .env from the cookbook directory, then validate required vars."""
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

    missing = [k for k in ("SANDBOX_API_KEY", "SANDBOX_BASE_URL", "LLM_API_KEY") if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            "Missing environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example → .env and fill in the values."
        )


# ============================================================================
# Config
# ============================================================================

@dataclass
class Config:
    # sandbox
    sandbox_base_url: str
    sandbox_api_key: str
    # LLM
    llm_provider: str       # "anthropic" or "openai"
    llm_api_key: str
    llm_model: str
    llm_timeout: float
    # paths
    local_csv: Path
    remote_csv: str
    artifacts_dir: Path


def load_config() -> Config:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    default_model = (
        "claude-haiku-4-5-20251001" if provider == "anthropic" else "gpt-4o-mini"
    )

    local_csv = Path(
        os.getenv("DEMO_LOCAL_CSV", str(Path(__file__).with_name("assets") / "orders.csv"))
    ).expanduser().resolve()
    if not local_csv.exists():
        raise RuntimeError(f"Input CSV not found: {local_csv}")

    artifacts_dir = Path(os.getenv("DEMO_ARTIFACTS_DIR", "artifacts")).resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        sandbox_base_url=os.environ["SANDBOX_BASE_URL"].rstrip("/"),
        sandbox_api_key=os.environ["SANDBOX_API_KEY"],
        llm_provider=provider,
        llm_api_key=os.environ["LLM_API_KEY"],
        llm_model=os.getenv("LLM_MODEL", default_model),
        llm_timeout=float(os.getenv("LLM_TIMEOUT", "60")),
        local_csv=local_csv,
        remote_csv=os.getenv("DEMO_REMOTE_CSV", "/workspace/orders.csv"),
        artifacts_dir=artifacts_dir,
    )


# ============================================================================
# LLM: code generation
# ============================================================================

_SYSTEM_PROMPT = (
    "You are a senior data engineer. "
    "Return only executable Python code. "
    "No markdown fences, no backticks, no prose — just the code."
)


def _build_user_prompt(remote_csv: str, csv_preview: str) -> str:
    return textwrap.dedent(f"""
        Write a Python script that analyses a sales CSV inside a sandbox.

        Input file: {remote_csv}

        The CSV has these columns:
          order_id, date, region, category, product, quantity, unit_price, discount

        The script must:
        1. Read the CSV with pandas; parse the date column as datetime.
        2. Add a `revenue` column: quantity * unit_price * (1 - discount).
        3. Write to /workspace/summary_by_region.csv:
             region, order_count, total_qty, total_revenue, avg_order_value
             sorted by total_revenue descending.
        4. Write to /workspace/summary_by_product.csv:
             product, category, order_count, total_revenue, avg_unit_price
             sorted by total_revenue descending.
        5. Write to /workspace/stats.json:
             grand_total_revenue, order_count, date_min, date_max,
             top_region (highest revenue), top_product (highest revenue).
             All numeric values rounded to 2 decimal places.
        6. Print the stats.json content to stdout.
        7. Print exactly on the last line: PIPELINE_DONE

        CSV preview (first 5 rows):
        {csv_preview}
    """).strip()


def _extract_code(text: str) -> str:
    """Strip markdown fences if the model added them despite instructions."""
    match = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else text.strip()


def call_anthropic(cfg: Config, csv_preview: str) -> str:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": cfg.llm_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": cfg.llm_model,
            "max_tokens": 4096,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _build_user_prompt(cfg.remote_csv, csv_preview)}],
        },
        timeout=cfg.llm_timeout,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def call_openai(cfg: Config, csv_preview: str) -> str:
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {cfg.llm_api_key}",
            "content-type": "application/json",
        },
        json={
            "model": cfg.llm_model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(cfg.remote_csv, csv_preview)},
            ],
        },
        timeout=cfg.llm_timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def generate_pipeline_code(cfg: Config) -> str:
    preview_lines = cfg.local_csv.read_text().splitlines()[:6]
    csv_preview = "\n".join(preview_lines)

    t0 = time.perf_counter()
    if cfg.llm_provider == "anthropic":
        raw = call_anthropic(cfg, csv_preview)
    elif cfg.llm_provider == "openai":
        raw = call_openai(cfg, csv_preview)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {cfg.llm_provider!r}. Use 'anthropic' or 'openai'.")
    elapsed = time.perf_counter() - t0

    code = _extract_code(raw)
    if not code:
        raise RuntimeError("LLM returned empty code.")

    print(f"  model   : {cfg.llm_model}")
    print(f"  elapsed : {elapsed:.1f}s")
    print(f"  code len: {len(code)} chars")
    return code


# ============================================================================
# Sandbox: HTTP client helpers
# ============================================================================

def sandbox_client(cfg: Config) -> httpx.Client:
    return httpx.Client(
        base_url=cfg.sandbox_base_url,
        headers={"X-API-Key": cfg.sandbox_api_key},
        timeout=120.0,
    )


def ensure_session(client: httpx.Client, conv_id: str) -> str:
    r = client.post("/v1/sessions", json={"conversation_id": conv_id})
    r.raise_for_status()
    return r.json()["session_id"]


def upload_file(client: httpx.Client, sid: str, local_path: Path, remote_name: str) -> dict:
    with local_path.open("rb") as fh:
        r = client.post(
            f"/v1/sessions/{sid}/files",
            files={"file": (remote_name, fh, "text/csv")},
        )
    r.raise_for_status()
    return r.json()


def exec_code(client: httpx.Client, sid: str, code: str, timeout_s: int = 60) -> dict:
    r = client.post(
        f"/v1/sessions/{sid}/exec",
        json={"code": code, "timeout_s": timeout_s},
    )
    r.raise_for_status()
    return r.json()


def list_workspace(client: httpx.Client, sid: str) -> list[dict]:
    r = client.get(f"/v1/sessions/{sid}/files")
    r.raise_for_status()
    return r.json().get("files", [])


def download_file(client: httpx.Client, sid: str, filename: str, local_dest: Path) -> None:
    r = client.get(f"/v1/sessions/{sid}/files/{filename}")
    r.raise_for_status()
    local_dest.write_bytes(r.content)


# ============================================================================
# Main flow
# ============================================================================

def _separator(title: str) -> None:
    print(f"\n{'─' * 64}")
    print(f"  {title}")
    print('─' * 64)


def main() -> None:
    bootstrap()
    cfg = load_config()
    conv_id = os.getenv("DEMO_CONV_ID", "cookbook-llm-pipeline")

    print("agent-sandbox cookbook — LLM pipeline demo")
    print(f"  sandbox : {cfg.sandbox_base_url}")
    print(f"  provider: {cfg.llm_provider}  model: {cfg.llm_model}")
    print(f"  input   : {cfg.local_csv}")

    # ------------------------------------------------------------------
    # Step 1 — LLM generates pandas pipeline code
    # ------------------------------------------------------------------
    _separator("Step 1/4  LLM → generate pipeline code")
    generated_code = generate_pipeline_code(cfg)

    generated_path = cfg.artifacts_dir / "generated_pipeline.py"
    generated_path.write_text(generated_code, encoding="utf-8")
    print(f"  saved to: {generated_path}")

    # ------------------------------------------------------------------
    # Step 2 — Upload CSV + execute generated code in sandbox
    # ------------------------------------------------------------------
    _separator("Step 2/4  Sandbox → upload CSV + execute")

    with sandbox_client(cfg) as client:
        sid = ensure_session(client, conv_id)
        print(f"  session : {sid}")

        meta = upload_file(client, sid, cfg.local_csv, Path(cfg.remote_csv).name)
        print(f"  uploaded: {meta['path']}  ({meta['size_bytes']} bytes)")

        t0 = time.perf_counter()
        result = exec_code(client, sid, generated_code, timeout_s=int(os.getenv("DEMO_EXEC_TIMEOUT", "60")))
        elapsed = time.perf_counter() - t0

        ok = result.get("ok")
        print(f"  ok={ok}  exit_reason={result.get('exit_reason')}  duration_ms={result.get('duration_ms')}")

        stdout = (result.get("stdout") or "").strip()
        stderr = (result.get("stderr") or "").strip()
        if stdout:
            print("\n[stdout]")
            print(stdout)
        if stderr:
            print("\n[stderr]")
            print(stderr)

        if not ok:
            raise RuntimeError(f"Sandbox exec failed: {result.get('exit_reason')}\n{stderr}")

        # ------------------------------------------------------------------
        # Step 3 — List and validate generated artifacts in workspace
        # ------------------------------------------------------------------
        _separator("Step 3/4  Sandbox → list workspace artifacts")

        workspace_files = list_workspace(client, sid)
        output_files = [f for f in workspace_files if f["name"] != Path(cfg.remote_csv).name]

        if not output_files:
            raise RuntimeError("No output files found in /workspace/ — check generated code.")

        for entry in output_files:
            print(f"  {entry['name']:40s}  {entry['size_bytes']:>8} bytes")

        # ------------------------------------------------------------------
        # Step 4 — Download artifacts to local artifacts/
        # ------------------------------------------------------------------
        _separator("Step 4/4  Download artifacts → local")

        manifest: dict = {
            "llm_provider": cfg.llm_provider,
            "llm_model": cfg.llm_model,
            "input_csv": str(cfg.local_csv),
            "generated_code": str(generated_path),
            "outputs": {},
        }

        for entry in output_files:
            fname = entry["name"]
            local_path = cfg.artifacts_dir / fname
            download_file(client, sid, fname, local_path)
            manifest["outputs"][fname] = str(local_path)
            print(f"  saved: {local_path}")

            if fname.endswith(".json"):
                try:
                    data = json.loads(local_path.read_text())
                    print(f"\n  {fname} contents:")
                    for k, v in data.items():
                        print(f"    {k}: {v}")
                except json.JSONDecodeError:
                    pass

        manifest_path = cfg.artifacts_dir / "run_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"\n  manifest: {manifest_path}")

    print("\ndone.")


if __name__ == "__main__":
    main()
