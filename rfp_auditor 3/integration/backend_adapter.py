"""
integration/backend_adapter.py
────────────────────────────────
Integration Layer — Gradio ↔ Audit Engine ↔ Excel Exporter

Responsibilities:
    1. Accept a raw PDF upload path + user page-range inputs from Gradio.
    2. Build the AuditJobConfig and call run_audit_pipeline() from backend_bridge.
    3. Wire in the DiscoveryAgent as the web_context_fn.
    4. Pipe each intermediate status message back as a generator yield (log line).
    5. After the audit, invoke ExcelExporter and return the output path.
    6. Handle and surface all exceptions without crashing Gradio.
    7. Persist job state to disk so the UI can reconnect after a disconnect.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.settings import get_logger
from core_ai.backend_bridge import run_audit_pipeline
from core_ai.discovery_agent import DiscoveryAgent
from integration.job_state import JobState
from shared.schemas import AuditJobConfig, ComplianceRow
from tools.excel_exporter import ExcelExporter

logger: logging.Logger = get_logger(__name__)

# ── Type alias for the Gradio generator yield ──────────────────────────────
# (log_line, progress_fraction, table_data, excel_path)
AuditYield = tuple[str, float, list[list[str]] | None, str | None]


# ── Table serialiser ───────────────────────────────────────────────────────


def _rows_to_table(rows: list[ComplianceRow]) -> list[list[str]]:
    """
    Convert ComplianceRow objects to a nested list suitable for
    gr.Dataframe display in Gradio.

    Returns:
        List of lists — each inner list is one row in display order:
        [#, Requirement, Recommended Model, Status, Justification, URL]
    """
    table: list[list[str]] = []
    for i, row in enumerate(rows, start=1):
        status_val = (
            row.compliance_status.value
            if hasattr(row.compliance_status, "value")
            else str(row.compliance_status)
        )
        table.append([
            str(i),
            row.requirement[:200] + ("…" if len(row.requirement) > 200 else ""),
            row.recommended_model,
            status_val,
            row.proof_justification[:300] + ("…" if len(row.proof_justification) > 300 else ""),
            row.source_url,
        ])
    return table


_TABLE_HEADERS: list[str] = [
    "#", "Requirement", "Recommended Model", "Status",
    "Proof / Justification", "Source URL",
]


# ── Main adapter function ──────────────────────────────────────────────────


def run_full_audit(
    pdf_path: str,
    start_page: int = 1,
    end_page: Optional[int] = None,
    output_dir: str = "output",
    use_search_agent: bool = True,
) -> Generator[AuditYield, None, None]:
    """
    End-to-end audit pipeline generator for Gradio.

    Yields (log_line, progress_fraction, table_data, excel_path) tuples
    at each meaningful pipeline stage. The final yield always contains
    the complete table_data and excel_path; intermediate yields have
    those fields as None.

    Job state is persisted to disk at every yield so the UI can recover
    results after a browser disconnect (laptop sleep, network drop, etc.).

    Args:
        pdf_path:          Absolute path to the uploaded PDF file.
        start_page:        First page to audit (1-based inclusive).
        end_page:          Last page to audit (1-based inclusive). None = all.
        output_dir:        Directory to write the Excel report into.
        use_search_agent:  If False, run in mock mode (empty web context).

    Yields:
        (log_line, progress 0.0→1.0, table or None, excel_path or None)
    """
    # ── Validate inputs ────────────────────────────────────────────────────
    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        yield (f"❌ File not found: {pdf_path}", 0.0, None, None)
        return
    if not pdf_file.suffix.lower() == ".pdf":
        yield (f"❌ File is not a PDF: {pdf_path}", 0.0, None, None)
        return

    start_page = max(1, start_page)
    if end_page is not None:
        end_page = max(start_page, end_page)

    # ── Create persistent job state ────────────────────────────────────────
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    short_id = uuid.uuid4().hex[:8]
    job_id = f"audit_{pdf_file.stem}_{ts}_{short_id}"
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    state = JobState.create(output_dir, job_id, pdf_file.name)

    def _yield_and_persist(line: str, progress: float, table=None, excel=None):
        """Yield a tuple to Gradio AND persist it to the state file."""
        state.update(line, progress)
        if excel:
            state.set_excel_path(excel)
        return (line, progress, table, excel)

    yield _yield_and_persist(f"📄 PDF loaded: {pdf_file.name}", 0.02)
    logger.info(
        f"[Adapter] Starting audit — PDF: {pdf_path} | "
        f"pages: {start_page}→{end_page or 'end'} | "
        f"search_agent: {use_search_agent} | job_id: {job_id}"
    )

    # ── Build config ───────────────────────────────────────────────────────
    output_path = str(
        Path(output_dir) / f"audit_{pdf_file.stem}_{ts}_{short_id}.xlsx"
    )
    config = AuditJobConfig(
        pdf_path=str(pdf_file.resolve()),
        start_page=start_page,
        end_page=end_page,
        output_path=output_path,
    )

    # ── Initialise search agent ────────────────────────────────────────────
    web_context_fn = None
    if use_search_agent:
        try:
            agent = DiscoveryAgent()
            web_context_fn = agent.search_and_get_context
            yield _yield_and_persist("🔍 Search agent initialised (Tavily + GoogleCSE fallback).", 0.05)
        except Exception as exc:
            yield _yield_and_persist(
                f"⚠️  Search agent failed to initialise: {exc}. "
                "Running in mock mode — compliance results will show No Match.",
                0.05,
            )
            logger.warning(f"[Adapter] DiscoveryAgent init failed: {exc}")
            web_context_fn = None

    # ── Pipeline progress bookkeeping ──────────────────────────────────────
    # Stage weights:
    #   init        2%
    #   pdf parse   15%
    #   queries     15%
    #   search      45%   (largest section — one call per requirement)
    #   audit       18%
    #   export      5%
    _PROGRESS_AFTER_PARSE = 0.20
    _PROGRESS_AFTER_QUERIES = 0.35
    _PROGRESS_SEARCH_START = 0.35
    _PROGRESS_SEARCH_END = 0.80
    _PROGRESS_AFTER_AUDIT = 0.93

    _search_steps: list[int] = [0]
    _total_search_steps: list[int] = [1]

    # Dev A addition: granular per-segment audit progress
    _audit_steps: list[int] = [0]
    _total_audit_steps: list[int] = [1]

    current_progress = 0.05
    final_results: list[ComplianceRow] = []

    # ── Run pipeline with progress mapping ────────────────────────────────
    try:
        for log_message, pipeline_results in run_audit_pipeline(
            config=config,
            web_context_fn=web_context_fn,
        ):
            # Map known log messages to progress fractions
            if "Parsing PDF" in log_message:
                current_progress = 0.10
            elif "Extracted" in log_message and "segments" in log_message:
                current_progress = _PROGRESS_AFTER_PARSE
            elif "Compiling search queries" in log_message:
                current_progress = _PROGRESS_AFTER_PARSE + 0.02
            elif "Generated" in log_message and "queries" in log_message:
                current_progress = _PROGRESS_AFTER_QUERIES
                try:
                    n = int(log_message.split()[1])
                    _total_search_steps[0] = max(1, n)
                except (IndexError, ValueError):
                    pass
            elif "Searching [" in log_message:
                _search_steps[0] += 1
                frac = _search_steps[0] / _total_search_steps[0]
                current_progress = (
                    _PROGRESS_SEARCH_START
                    + frac * (_PROGRESS_SEARCH_END - _PROGRESS_SEARCH_START)
                )
            elif "Auditing" in log_message and "requirements" in log_message:
                current_progress = _PROGRESS_SEARCH_END + 0.02
                # Dev A addition: parse total audit steps for granular sub-progress
                try:
                    n = int(log_message.split()[1])
                    _total_audit_steps[0] = max(1, n)
                except (IndexError, ValueError):
                    pass
            elif "Auditing segment" in log_message:
                # Dev A addition: per-segment progress within the audit phase
                _audit_steps[0] += 1
                frac = _audit_steps[0] / _total_audit_steps[0]
                current_progress = (
                    _PROGRESS_SEARCH_END + 0.02
                    + frac * (_PROGRESS_AFTER_AUDIT - _PROGRESS_SEARCH_END - 0.02)
                )
            elif "Audit complete" in log_message:
                current_progress = _PROGRESS_AFTER_AUDIT

            yield _yield_and_persist(log_message, round(current_progress, 3))

            if pipeline_results is not None:
                final_results = pipeline_results

    except Exception as exc:
        tb = traceback.format_exc()
        error_msg = f"❌ Pipeline error: {exc}"
        logger.error(f"[Adapter] Pipeline exception:\n{tb}")
        state.error(error_msg)
        yield (error_msg, current_progress, None, None)
        return

    # ── Guard: no results ──────────────────────────────────────────────────
    if not final_results:
        msg = "⚠️  Pipeline produced no results. Check the PDF content and page range."
        state.error(msg)
        yield (msg, 0.95, None, None)
        return

    # ── Export to Excel ────────────────────────────────────────────────────
    yield _yield_and_persist(f"📊 Exporting {len(final_results)} rows to Excel...", 0.94)
    saved_path = None
    try:
        exporter = ExcelExporter()
        saved_path = exporter.export(final_results, output_path)
        yield _yield_and_persist(f"✅ Excel report saved: {saved_path}", 0.97, excel=saved_path)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(f"[Adapter] Excel export failed:\n{tb}")
        yield _yield_and_persist(f"❌ Excel export failed: {exc}", 0.97)

    # ── Final yield with complete data ─────────────────────────────────────
    table_data = _rows_to_table(final_results)
    summary_line = (
        f"🏁 Audit complete — {len(final_results)} requirements processed. "
        f"Full: {sum(1 for r in final_results if str(r.compliance_status) in ('Full Match', 'ComplianceStatus.FULL_MATCH'))} | "
        f"Partial: {sum(1 for r in final_results if 'Partial' in str(r.compliance_status))} | "
        f"No Match: {sum(1 for r in final_results if 'No Match' in str(r.compliance_status))}"
    )

    # Persist the completed state before the final yield
    state.complete(table_data, saved_path)

    yield (summary_line, 1.0, table_data, saved_path)
    logger.info(f"[Adapter] {summary_line}")


# ── Exported table headers for Gradio ─────────────────────────────────────


def get_table_headers() -> list[str]:
    """Return the column header list for the Gradio Dataframe component."""
    return _TABLE_HEADERS