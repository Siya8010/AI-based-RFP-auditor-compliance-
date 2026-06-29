"""
ui/gradio_app.py
─────────────────
Phase 5 — Form-Based UI Dashboard

Gradio 6.x compatible dashboard for the RFP Compliance Auditor.

Reconnect behaviour
-------------------
If the browser disconnects while a long audit is running (laptop sleep,
network drop, tab close) the backend continues writing to disk via
integration/job_state.py. On page reload — or when the user clicks
"🔄 Reconnect / Restore" — the UI reads the latest job state file
and restores whatever results are already available.

Usage:
    python ui/gradio_app.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import gradio as gr

from config.settings import get_logger
from integration.backend_adapter import get_table_headers, run_full_audit
from integration.job_state import JobState

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

# NOTE: css is passed to launch() in Gradio 6.x, not gr.Blocks()
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
#reconnect-btn {
    background: #b8860b;
    color: white;
    font-weight: 600;
    border-radius: 6px;
}
#reconnect-btn:hover {
    background: #9a7209;
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
    return datetime.now().strftime("%H:%M:%S")


def _append_log(existing: str, line: str) -> str:
    return f"[{_ts()}]  {line}\n" + existing


def _job_banner(state: "JobState | None") -> str:
    if state is None:
        return "No previous job found."
    icon = {"complete": "✅", "running": "⏳", "error": "❌"}.get(state.status, "❓")
    parts = [
        f"{icon} Last job: **{state.job_id}**",
        f"PDF: {state.pdf_name}",
        f"Started: {state.started_at}",
    ]
    if state.finished_at:
        parts.append(f"Finished: {state.finished_at}")
    if state.is_running and state.is_stale():
        parts.append("⚠️ Job appears stale (backend may have crashed).")
    return "  |  ".join(parts)


# ── Reconnect handler ──────────────────────────────────────────────────────


def on_reconnect():
    """
    Load the latest persisted job state from disk and restore the UI.

    Returns updates for:
        (job_banner, log_box, progress_bar, results_table, download_btn)
    """
    state = JobState.load_latest(_OUTPUT_DIR)

    if state is None:
        return (
            "ℹ️ No previous job state found on disk.",
            "",
            gr.update(value=0),
            gr.update(value=None),
            gr.update(value=None, visible=False),
        )

    log_text = state.log_text
    progress = int(state.progress * 100)
    table = state.table_data
    excel = state.excel_path

    if state.is_complete:
        banner = f"✅ Restored completed job — {state.job_id}"
    elif state.is_running:
        if state.is_stale():
            banner = (
                f"⚠️ Job {state.job_id} was still 'running' but the backend "
                "appears stopped. Partial results shown below."
            )
        else:
            banner = (
                f"⏳ Job {state.job_id} is still running in the background. "
                "Click Reconnect again to refresh."
            )
    elif state.status == "error":
        banner = "❌ Last job ended with an error. Check the log below."
    else:
        banner = _job_banner(state)

    return (
        banner,
        log_text,
        gr.update(value=progress),
        gr.update(value=table),
        gr.update(value=excel, visible=(excel is not None)),
    )


# ── Run audit handler ──────────────────────────────────────────────────────


def on_run_audit(pdf_file, start_page: int, end_page: int, use_search: bool):
    """
    Main audit handler — wired to the Run Audit button click.
    Generator: yields UI updates at each pipeline checkpoint.

    Yields:
        (job_banner, log_text, progress, results_df, download_file, run_btn)
    """
    if pdf_file is None:
        yield (
            "",
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
            "",
            _append_log("", f"❌ End page ({end}) must be ≥ start page ({start})."),
            gr.update(value=0),
            gr.update(value=None),
            gr.update(value=None, visible=False),
            gr.update(interactive=True),
        )
        return

    start_msg = f"🚀 Starting audit — {Path(pdf_path).name} | pages {start}→{end or 'end'}"
    yield (
        f"⏳ Audit running — {Path(pdf_path).name}",
        _append_log("", start_msg),
        gr.update(value=0),
        gr.update(value=None),
        gr.update(value=None, visible=False),
        gr.update(interactive=False),
    )

    log_text = f"[{_ts()}]  {start_msg}\n"
    current_table = None
    excel_path = None

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
                f"⏳ Audit running — {Path(pdf_path).name}",
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
            f"❌ Audit error — {exc}",
            log_text,
            gr.update(value=0),
            gr.update(value=current_table),
            gr.update(value=excel_path, visible=(excel_path is not None)),
            gr.update(interactive=True),
        )
        return

    yield (
        "✅ Audit complete — results saved to disk",
        log_text,
        gr.update(value=100),
        gr.update(value=current_table),
        gr.update(value=excel_path, visible=(excel_path is not None)),
        gr.update(interactive=True),
    )


# ── Clear handler ──────────────────────────────────────────────────────────


def on_clear():
    return (
        "",
        "",
        gr.update(value=0),
        gr.update(value=None),
        gr.update(value=None, visible=False),
    )


# ── Layout builder ─────────────────────────────────────────────────────────


def build_ui() -> gr.Blocks:
    # css is NOT passed to gr.Blocks in Gradio 6.x — it goes to launch()
    with gr.Blocks(title=_APP_TITLE) as demo:

        gr.Markdown(f"# 📋 {_APP_TITLE}")
        gr.Markdown(_APP_DESCRIPTION)

        job_banner = gr.Markdown(value="")

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

        with gr.Row():
            run_btn = gr.Button(
                "▶  Run Audit",
                variant="primary",
                elem_id="run-btn",
                scale=3,
            )
            # tooltip removed — not supported in Gradio 6.x
            reconnect_btn = gr.Button(
                "🔄  Reconnect / Restore",
                variant="secondary",
                elem_id="reconnect-btn",
                scale=2,
            )
            clear_btn = gr.Button("🗑  Clear", variant="secondary", scale=1)

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

        download_btn = gr.File(
            label="⬇  Download Excel Report",
            value=None,
            visible=False,
            file_count="single",
            elem_id="download-btn",
        )

        gr.Markdown(
            "<br><sub>RFP Compliance Auditor | "
            "Powered by Ollama + Tavily + OpenPyXL | "
            "Results are persisted to disk — use 🔄 Reconnect to restore after a disconnect.</sub>"
        )

        # ── Event wiring ───────────────────────────────────────────────────
        _run_outputs = [job_banner, log_box, progress_bar, results_table, download_btn, run_btn]
        _restore_outputs = [job_banner, log_box, progress_bar, results_table, download_btn]

        run_btn.click(
            fn=on_run_audit,
            inputs=[pdf_upload, start_page_input, end_page_input, use_search_toggle],
            outputs=_run_outputs,
        )

        reconnect_btn.click(
            fn=on_reconnect,
            inputs=[],
            outputs=_restore_outputs,
        )

        clear_btn.click(
            fn=on_clear,
            inputs=[],
            outputs=_restore_outputs,
        )

        # Auto-restore on every page load / reconnect
        demo.load(
            fn=on_reconnect,
            inputs=[],
            outputs=_restore_outputs,
        )

    return demo


# ── Entry point ────────────────────────────────────────────────────────────


def main() -> None:
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    demo = build_ui()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
        inbrowser=True,
        theme=gr.themes.Default(),
        css=_CSS,   # css moves here in Gradio 6.x
        # Increase timeout and connection settings to handle screen lock scenarios
        max_threads=40,
        prevent_thread_lock=False,
        quiet=False,
        # Allow Gradio to access files in the output directory
        allowed_paths=[_OUTPUT_DIR],
    )


if __name__ == "__main__":
    main()