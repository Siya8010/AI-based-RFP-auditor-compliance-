"""
ui/gradio_app.py
─────────────────
Phase 5 — Form-Based UI Dashboard (Developer B)

Gradio 4.x dashboard for the RFP Compliance Auditor.

Layout:
    ┌─────────────────────────────────────────────────────┐
    │  RFP Compliance Auditor                             │
    ├─────────────────────────────────────────────────────┤
    │  [PDF Upload]        [Start Page] [End Page]        │
    │                      [☑ Use Search Agent]           │
    │  [Run Audit]                                        │
    ├─────────────────────────────────────────────────────┤
    │  Progress bar ▓▓▓▓▓░░░░░ 60%                        │
    │  ┌────────────────── Live Logs ─────────────────┐   │
    │  │ 14:03:22  Parsing PDF...                     │   │
    │  │ 14:03:24  Extracted 32 requirement segments. │   │
    │  └──────────────────────────────────────────────┘   │
    ├─────────────────────────────────────────────────────┤
    │  Results Table (sortable, filterable)               │
    ├─────────────────────────────────────────────────────┤
    │  [⬇ Download Excel Report]                          │
    └─────────────────────────────────────────────────────┘

All backend calls go through integration/backend_adapter.py.
This file contains ONLY UI wiring and no business logic.

Usage:
    python ui/gradio_app.py
    # or
    python -m ui.gradio_app
"""

from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# ── Ensure project root is on sys.path when run directly ──────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import gradio as gr

from config.settings import get_logger
from integration.backend_adapter import get_table_headers, run_full_audit

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_APP_TITLE = "RFP Compliance Auditor"
_APP_DESCRIPTION = (
    "Upload an RFP document, select the page range to analyse, "
    "and run the AI-powered compliance audit. "
    "Results are colour-coded and exportable as a formatted Excel report."
)

_OUTPUT_DIR = str(_PROJECT_ROOT / "output")

_TABLE_HEADERS = get_table_headers()

_CSS = """
#run-btn {
    background: linear-gradient(90deg, #1f3864 0%, #2e5799 100%);
    color: white;
    font-weight: 700;
    font-size: 1rem;
    border: none;
    border-radius: 6px;
}
#run-btn:hover {
    background: linear-gradient(90deg, #2e5799 0%, #1f3864 100%);
}
#download-btn {
    background: #217346;
    color: white;
    font-weight: 600;
    border-radius: 6px;
}
#log-box textarea {
    font-family: 'Courier New', monospace;
    font-size: 0.82rem;
    background: #0d1117;
    color: #58d68d;
}
.gradio-container {
    max-width: 1280px;
    margin: 0 auto;
}
"""

# ── Helpers ────────────────────────────────────────────────────────────────


def _ts() -> str:
    """Return a short HH:MM:SS timestamp string."""
    return datetime.now().strftime("%H:%M:%S")


def _append_log(existing: str, line: str) -> str:
    """Prepend a timestamped log line to the log box."""
    return f"[{_ts()}]  {line}\n" + existing


# ── Event handlers ─────────────────────────────────────────────────────────


def on_run_audit(
    pdf_file,
    start_page: int,
    end_page: int,
    use_search: bool,
):
    """
    Main audit handler — wired to the Run Audit button click.

    This is a Gradio generator function: it yields UI state updates at each
    pipeline checkpoint so the interface reflects live progress.

    Args:
        pdf_file:    Gradio file object from gr.File (has a .name attribute).
        start_page:  First page number to audit.
        end_page:    Last page number to audit (0 = all pages).
        use_search:  Whether to use the live search agent.

    Yields:
        Tuple of updated Gradio component values:
        (log_text, progress, results_df, download_file, run_btn_interactive)
    """
    # ── Input validation ───────────────────────────────────────────────────
    if pdf_file is None:
        yield (
            _append_log("", "❌ Please upload a PDF file before running the audit."),
            gr.update(value=0),
            gr.update(value=None),
            gr.update(value=None, visible=False),
            gr.update(interactive=True),
        )
        return

    pdf_path: str = pdf_file.name if hasattr(pdf_file, "name") else str(pdf_file)
    start = max(1, int(start_page))
    end = int(end_page) if end_page and int(end_page) > 0 else None

    if end is not None and end < start:
        yield (
            _append_log("", f"❌ End page ({end}) must be ≥ start page ({start})."),
            gr.update(value=0),
            gr.update(value=None),
            gr.update(value=None, visible=False),
            gr.update(interactive=True),
        )
        return

    # ── Disable button while running ───────────────────────────────────────
    yield (
        _append_log("", f"🚀 Starting audit — {Path(pdf_path).name} | pages {start}→{end or 'end'}"),
        gr.update(value=0),
        gr.update(value=None),
        gr.update(value=None, visible=False),
        gr.update(interactive=False),
    )

    log_text = f"[{_ts()}]  🚀 Starting audit — {Path(pdf_path).name} | pages {start}→{end or 'end'}\n"
    current_table = None
    excel_path = None

    # ── Stream pipeline updates ────────────────────────────────────────────
    try:
        for line, progress_val, table_data, saved_path in run_full_audit(
            pdf_path=pdf_path,
            start_page=start,
            end_page=end,
            output_dir=_OUTPUT_DIR,
            use_search_agent=bool(use_search),
        ):
            log_text = f"[{_ts()}]  {line}\n" + log_text

            if table_data is not None:
                current_table = table_data

            if saved_path is not None:
                excel_path = saved_path

            yield (
                log_text,
                gr.update(value=round(progress_val * 100)),
                gr.update(value=current_table if current_table else None),
                gr.update(value=excel_path, visible=(excel_path is not None)),
                gr.update(interactive=False),
            )

    except Exception as exc:
        log_text = f"[{_ts()}]  ❌ Unexpected error: {exc}\n" + log_text
        logger.exception(f"[GradioApp] Unhandled exception in on_run_audit: {exc}")
        yield (
            log_text,
            gr.update(value=0),
            gr.update(value=current_table),
            gr.update(value=excel_path, visible=(excel_path is not None)),
            gr.update(interactive=True),
        )
        return

    # ── Re-enable button on completion ────────────────────────────────────
    yield (
        log_text,
        gr.update(value=100),
        gr.update(value=current_table),
        gr.update(value=excel_path, visible=(excel_path is not None)),
        gr.update(interactive=True),
    )


def on_clear():
    """Reset all output components to their initial state."""
    return (
        "",           # log_box
        gr.update(value=0),    # progress_bar
        gr.update(value=None), # results_table
        gr.update(value=None, visible=False),  # download_btn
    )


# ── Layout builder ─────────────────────────────────────────────────────────


def build_ui() -> gr.Blocks:
    """
    Construct and return the Gradio Blocks application.
    The returned object can be launched with .launch() or served via ASGI.
    """
    with gr.Blocks(title=_APP_TITLE) as demo:

        # ── Header ─────────────────────────────────────────────────────────
        gr.Markdown(f"# 📋 {_APP_TITLE}")
        gr.Markdown(_APP_DESCRIPTION)

        # ── Input Row ──────────────────────────────────────────────────────
        with gr.Row(equal_height=True):

            with gr.Column(scale=3):
                pdf_upload = gr.File(
                    label="📁 Upload RFP PDF",
                    file_types=[".pdf"],
                    file_count="single",
                    height=120,
                )

            with gr.Column(scale=2):
                with gr.Row():
                    start_page_input = gr.Number(
                        label="Start Page",
                        value=1,
                        minimum=1,
                        precision=0,
                        info="First page to analyse (1-based).",
                    )
                    end_page_input = gr.Number(
                        label="End Page",
                        value=0,
                        minimum=0,
                        precision=0,
                        info="Last page (0 = all pages).",
                    )
                use_search_toggle = gr.Checkbox(
                    label="🔍 Use Live Search Agent (Tavily + GoogleCSE fallback)",
                    value=True,
                    info=(
                        "Disable for offline testing. "
                        "When off, all results will show 'No Match'."
                    ),
                )

        # ── Action Buttons ─────────────────────────────────────────────────
        with gr.Row():
            run_btn = gr.Button(
                "▶  Run Audit",
                variant="primary",
                elem_id="run-btn",
                scale=3,
            )
            clear_btn = gr.Button("🗑  Clear", variant="secondary", scale=1)

        # ── Progress & Logs ────────────────────────────────────────────────
        progress_bar = gr.Slider(
            label="Progress",
            minimum=0,
            maximum=100,
            value=0,
            step=1,
            interactive=False,
            info="Pipeline progress (0 – 100%)",
        )

        log_box = gr.Textbox(
            label="📡 Live Execution Log",
            value="",
            lines=10,
            max_lines=30,
            autoscroll=True,
            interactive=False,
            placeholder="Audit logs will appear here when you click Run Audit…",
            elem_id="log-box",
        )

        # ── Results Table ──────────────────────────────────────────────────
        gr.Markdown("### 📊 Audit Results")
        results_table = gr.Dataframe(
            headers=_TABLE_HEADERS,
            datatype=["number", "str", "str", "str", "str", "str"],
            label="Compliance Findings",
            value=None,
            interactive=False,
            wrap=True,
            column_widths=["4%", "25%", "14%", "9%", "30%", "18%"],
            max_height=480,
        )

        # ── Download ───────────────────────────────────────────────────────
        download_btn = gr.File(
            label="⬇  Download Excel Report",
            value=None,
            visible=False,
            file_count="single",
            elem_id="download-btn",
        )

        # ── Footer ─────────────────────────────────────────────────────────
        gr.Markdown(
            "<br><sub>RFP Compliance Auditor | "
            "Powered by Ollama + Tavily + OpenPyXL</sub>"
        )

        # ── Event Wiring ───────────────────────────────────────────────────
        run_btn.click(
            fn=on_run_audit,
            inputs=[pdf_upload, start_page_input, end_page_input, use_search_toggle],
            outputs=[log_box, progress_bar, results_table, download_btn, run_btn],
        )

        clear_btn.click(
            fn=on_clear,
            inputs=[],
            outputs=[log_box, progress_bar, results_table, download_btn],
        )

    return demo


# ── Entry point ────────────────────────────────────────────────────────────


def main() -> None:
    """Launch the Gradio app."""
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    demo = build_ui()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
        inbrowser=True,
        theme=gr.themes.Default(),
        css=_CSS,
    )


if __name__ == "__main__":
    main()