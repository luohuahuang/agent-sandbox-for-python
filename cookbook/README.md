# agent-sandbox cookbook

Single end-to-end demo of the full sandbox capability chain:

```
Upload CSV → LLM generates pandas pipeline → Execute in sandbox → Download results
```

## What it demonstrates

1. **LLM code generation** — send a CSV preview + task description to Claude or
   GPT; receive a complete, executable pandas pipeline.
2. **Files API** — `POST /v1/sessions/{id}/files` uploads the CSV into the
   session's `/workspace/` before execution.
3. **Exec** — `POST /v1/sessions/{id}/exec` runs the generated code inside the
   sandboxed Jupyter kernel; stdout/stderr and resource usage are returned.
4. **Artifact download** — `GET /v1/sessions/{id}/files/{name}` pulls the output
   files written by the generated code back to local disk.

## Layout

```
cookbook/
  main.py               ← the demo (single file)
  .env.example          ← template — copy to .env
  assets/
    orders.csv          ← sample 30-row sales CSV
  artifacts/            ← output files written here at runtime (git-ignored)
```

## Setup

### 1. Start the gateway

```bash
# from repo root
uvicorn app.main:app --port 8080
```

### 2. Install deps

`httpx` and `python-dotenv` are the only runtime dependencies; both are
already in `pyproject.toml`'s dev extras.

```bash
pip install -e ".[dev]"   # from repo root, if not already done
```

### 3. Configure

```bash
cd cookbook
cp .env.example .env
```

Edit `.env`:

| Variable | Required | Notes |
|----------|----------|-------|
| `SANDBOX_API_KEY` | ✓ | must match gateway config |
| `SANDBOX_BASE_URL` | ✓ | default `http://localhost:8080` |
| `LLM_PROVIDER` | | `anthropic` (default) or `openai` |
| `LLM_API_KEY` | ✓ | Anthropic or OpenAI key |
| `LLM_MODEL` | | default `claude-haiku-4-5-20251001` / `gpt-4o-mini` |

### 4. Run

```bash
# from cookbook/
python main.py
```

## Expected output

```
agent-sandbox cookbook — LLM pipeline demo
  sandbox : http://localhost:8080
  provider: anthropic  model: claude-haiku-4-5-20251001
  input   : .../assets/orders.csv

────────────────────────────────────────────────────────────────
  Step 1/4  LLM → generate pipeline code
────────────────────────────────────────────────────────────────
  model   : claude-haiku-4-5-20251001
  elapsed : 3.2s
  code len: 1847 chars
  saved to: .../artifacts/generated_pipeline.py

────────────────────────────────────────────────────────────────
  Step 2/4  Sandbox → upload CSV + execute
────────────────────────────────────────────────────────────────
  session : cookbook-llm-pipeline
  uploaded: /workspace/orders.csv  (1024 bytes)
  ok=True  exit_reason=ok  duration_ms=843

[stdout]
{"grand_total_revenue": 98245.6, "order_count": 30, ...}
PIPELINE_DONE

────────────────────────────────────────────────────────────────
  Step 3/4  Sandbox → list workspace artifacts
────────────────────────────────────────────────────────────────
  summary_by_region.csv                        512 bytes
  summary_by_product.csv                       448 bytes
  stats.json                                   210 bytes

────────────────────────────────────────────────────────────────
  Step 4/4  Download artifacts → local
────────────────────────────────────────────────────────────────
  saved: .../artifacts/summary_by_region.csv
  saved: .../artifacts/summary_by_product.csv
  saved: .../artifacts/stats.json

  stats.json contents:
    grand_total_revenue: 98245.6
    order_count: 30
    date_min: 2026-01-03
    date_max: 2026-02-21
    top_region: APAC
    top_product: Laptop Pro

  manifest: .../artifacts/run_manifest.json

done.
```

## Bring your own data

Set `DEMO_LOCAL_CSV=/path/to/your.csv` in `.env`.  
Adjust the LLM prompt in `main.py` (`_build_user_prompt`) to describe your
schema and desired outputs.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| `Connection refused` at step 2 | Gateway not running |
| `401 Unauthorized` | `SANDBOX_API_KEY` mismatch |
| `ok=False, exit_reason=timeout` | Raise `DEMO_EXEC_TIMEOUT` |
| `ok=False` with pandas ImportError | Rebuild sandbox image: `docker build -t agent-sandbox:latest -f docker/Dockerfile.sandbox docker/` |
| LLM returns prose instead of code | Retry; or switch to a stronger model via `LLM_MODEL` |
