# RFP Auditor — Developer A Module

> **Internship Project** | AI-powered RFP compliance auditing pipeline  
> Developer A owns: PDF ingestion, Gemini AI client, structured audit engine, query compiler, backend bridge  
> Developer B owns: Search agent, Excel exporter, Gradio UI  

---

## Project Structure

```
rfp_auditor/
├── core_ai/                   ← Developer A's primary module
│   ├── gemini_client.py       # Phase 1: Gemini/LangChain client + health check
│   ├── pdf_parser.py          # Phase 2: PyMuPDF bounding-box text extractor
│   ├── audit_engine.py        # Phase 4: Structured compliance scoring (Pydantic + Gemini)
│   ├── query_compiler.py      # Phase 3 support: LangChain search query generator
│   └── backend_bridge.py      # Phase 5 support: orchestrator for Dev B's Gradio UI
│
├── shared/
│   └── schemas.py             ← SHARED CONTRACT — do not rename fields without sync call
│
├── config/
│   └── settings.py            # Centralised env var loading (all modules import from here)
│
├── tests/
│   ├── test_pdf_parser.py
│   ├── test_schemas.py
│   └── test_gemini_client.py
│
├── main.py                    # CLI entry point (Dev A standalone testing)
├── requirements.txt
├── .env.example               # Copy to .env and fill in keys
├── .gitignore
└── pytest.ini
```

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.10+ | Use `python --version` to check |
| pip | latest | `pip install --upgrade pip` |
| Git | any | For version control |

---

## Setup (Step by Step)

### 1 — Clone the repository

```bash
git clone <your-repo-url>
cd rfp_auditor
```

### 2 — Create a virtual environment

Using a virtual environment keeps your system Python clean and makes the project reproducible.

```bash
# Create
python -m venv venv

# Activate (Linux/macOS)
source venv/bin/activate

# Activate (Windows)
venv\Scripts\activate
```

You should see `(venv)` in your terminal prompt.

### 3 — Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

> **Note:** PyMuPDF installs as the package `pymupdf` but is imported as `fitz` — this is expected.

### 4 — Set up your environment variables

```bash
# Copy the template
cp .env.example .env
```

Open `.env` in any text editor and fill in your keys:

```
GOOGLE_API_KEY=AIza...          ← Get from Google AI Studio (aistudio.google.com)
TAVILY_API_KEY=tvly-...         ← Dev B's key; leave blank for now
GOOGLE_CSE_API_KEY=             ← Dev B's key; leave blank for now
GOOGLE_CSE_ENGINE_ID=           ← Dev B's key; leave blank for now
```

**Getting your Google AI Studio key:**
1. Go to [aistudio.google.com](https://aistudio.google.com)
2. Sign in with your Google account
3. Click **"Get API key"** → **"Create API key"**
4. Copy the key into `.env`

> ⚠️ **Never commit `.env` to Git.** It's in `.gitignore`. Use `.env.example` for sharing the template.

### 5 — Verify the setup

Run the health check to confirm your API key works and latency is acceptable:

```bash
python main.py --health-check
```

Expected output:
```
  status: OK
  model: gemini-1.5-flash
  latency_ms: 843.2
  response_preview: I am operational. The current model is gemini-1.5-flash.
```

---

## Running the Pipeline

### Run on a real RFP PDF

```bash
python main.py --pdf path/to/your_rfp.pdf --start 1 --end 20
```

Options:
- `--pdf`    : Path to the RFP PDF file *(required)*
- `--start`  : First page to parse, 1-based (default: 1)
- `--end`    : Last page to parse, 1-based (default: last page)
- `--output` : Output JSON path (default: `output/audit_results.json`)

### What this produces

A JSON file at `output/audit_results.json` containing an array of compliance rows:

```json
[
  {
    "requirement": "System shall support 10GbE uplink with LACP bonding.",
    "recommended_model": "AUDIT_ERROR (no search agent connected)",
    "compliance_status": "No Match",
    "proof_justification": "Insufficient web evidence found.",
    "source_url": "https://example.com/error"
  }
]
```

> **Note:** Without Dev B's search agent connected, all rows show `No Match` with no web evidence. This is expected in standalone mode. Once integrated with `discovery_agent.py`, real search results populate the evidence fields.

---

## Running Tests

```bash
pytest
```

Run with verbose output:

```bash
pytest -v
```

Run a specific test file:

```bash
pytest tests/test_schemas.py -v
```

All tests are mocked — they do **not** require a real API key or PDF file.

---

## Integration Guide for Developer B

> **Read this section before the Phase 4 sync call.**

### What Dev B needs from this codebase

| What | Where | Notes |
|------|-------|-------|
| Shared data schema | `shared/schemas.py` | Import `ComplianceRow`, `AuditJobConfig` |
| Pipeline entry point | `core_ai/backend_bridge.py` | Call `run_audit_pipeline(config, web_context_fn=...)` |
| Config loader | `config/settings.py` | Import `TAVILY_API_KEY` etc. from here |

### How to wire the Gradio UI (Dev B's task)

In Dev B's Gradio event handler:

```python
from core_ai.backend_bridge import run_audit_pipeline
from shared.schemas import AuditJobConfig

def on_run_button(pdf_file, start_page, end_page):
    config = AuditJobConfig(
        pdf_path=pdf_file.name,
        start_page=int(start_page),
        end_page=int(end_page),
    )
    # web_context_fn = Dev B's discovery_agent.search
    for log_msg, results in run_audit_pipeline(config, web_context_fn=discovery_agent.search):
        yield log_msg   # Updates the Gradio live log window
    return results      # Feed into excel_exporter
```

### Schema sync protocol

Before either developer changes `shared/schemas.py`:
1. Create a Git branch: `git checkout -b schema/update-compliance-row`
2. Hop on a call to agree the change
3. Update `schemas.py` together
4. Both developers update their downstream code
5. Merge the branch — **not** directly to main

---

## Git Workflow (Minimising Commit Clashes)

This repo is structured so Dev A and Dev B work in **non-overlapping directories**:

| Developer | Owns | Never touch |
|-----------|------|-------------|
| Dev A | `core_ai/`, `config/`, `tests/`, `main.py` | `tools/` (Dev B's space) |
| Dev B | `tools/` | `core_ai/`, `config/` |
| Both | `shared/schemas.py` | Change only via sync call |

### Branching convention

```
main                    ← stable, integration-tested only
├── deva/phase-1        ← Dev A feature branches
├── deva/phase-2
├── deva/phase-4-audit
└── devb/phase-3        ← Dev B feature branches (separate tree)
```

**Never push directly to `main`.** Use pull requests so your supervisor can review.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError: No module named 'fitz'` | Run `pip install PyMuPDF` |
| `EnvironmentError: Missing required environment variables: GOOGLE_API_KEY` | Copy `.env.example` → `.env` and add your key |
| `langchain_google_genai` import error | Run `pip install langchain-google-genai` |
| Health check returns `FAILED` with 429 | API rate limit hit; wait 60 seconds and retry |
| PDF parser returns 0 segments | Check page range; the RFP might be image-based (scanned) and needs OCR |
| Tests fail with `ImportError` | Make sure your virtual environment is activated |

---

## Environment Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_API_KEY` | **Yes** | Google AI Studio API key for Gemini |
| `GEMINI_MODEL` | No | Model name (default: `gemini-1.5-flash`) |
| `MAX_TOKENS` | No | Max output tokens (default: `8192`) |
| `TAVILY_API_KEY` | Dev B | Tavily search API key |
| `GOOGLE_CSE_API_KEY` | Dev B | Google Custom Search fallback |
| `GOOGLE_CSE_ENGINE_ID` | Dev B | Custom Search Engine ID |
| `LOG_LEVEL` | No | `DEBUG`, `INFO`, `WARNING` (default: `INFO`) |
