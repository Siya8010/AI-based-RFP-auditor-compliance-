"""
core_ai/backend_bridge.py
──────────────────────────
Phase 5 Support — Backend Pipeline Bridge (Developer A Support Tasks)

This module is the single entry point that Developer B's Gradio UI calls.
It wires together all of Dev A's components (PDF parser, query compiler,
audit engine) into one callable function that matches the interface
Developer B expects in their Gradio event handlers.

Integration contract:
- Dev B calls `run_audit_pipeline(config)` from their Gradio `gr.Button.click` handler.
- This function returns list[ComplianceRow] which Dev B feeds into excel_exporter.py.
- Progress is reported via a generator (yield) so Gradio's live log window updates.

Dev B usage in their UI:
    from core_ai.backend_bridge import run_audit_pipeline
    from shared.schemas import AuditJobConfig

    config = AuditJobConfig(pdf_path=..., start_page=..., end_page=...)
    for status, results in run_audit_pipeline(config, web_context_fn=discovery_agent.search):
        log_window.update(status)
"""

from collections.abc import Generator
from typing import Callable, Optional

from config.settings import get_logger
from core_ai.gemini_client import GeminiClient
from core_ai.pdf_parser import PDFParser
from core_ai.query_compiler import QueryCompiler
from core_ai.audit_engine import AuditEngine
from shared.schemas import AuditJobConfig, ComplianceRow, ParsedSegment

logger = get_logger(__name__)


def run_audit_pipeline(
    config: AuditJobConfig,
    web_context_fn: Optional[Callable[[str], list[str]]] = None,
) -> Generator[tuple[str, list[ComplianceRow] | None], None, None]:
    """
    Full pipeline orchestrator. Designed to be called from Dev B's Gradio UI.

    Yields (status_message, results_or_None) tuples so the UI can log progress
    in real time. On final yield, results is the complete list[ComplianceRow].

    Args:
        config:         AuditJobConfig with PDF path and page range.
        web_context_fn: A callable that takes a search query (str) and returns
                        a list of web result strings. In production, this is
                        Dev B's `discovery_agent.search`. For Dev A standalone
                        testing, pass None to use mock empty context.

    Yields:
        (log_message: str, results: list[ComplianceRow] | None)
        Final yield has results populated; intermediate yields have results=None.
    """
    yield ("Initialising Gemini client...", None)
    client = GeminiClient()
    compiler = QueryCompiler(client)
    engine = AuditEngine(client)

    # ── Phase 2: PDF Ingestion ─────────────────────────────────────────────
    yield (f"Parsing PDF: {config.pdf_path}", None)
    with PDFParser(config.pdf_path) as parser:
        segments: list[ParsedSegment] = parser.extract(
            start_page=config.start_page,
            end_page=config.end_page,
        )
    yield (f"Extracted {len(segments)} requirement segments.", None)

    if not segments:
        yield ("⚠️  No text segments found. Check page range or PDF content.", [])
        return

    # ── Phase 3: Query Compilation ─────────────────────────────────────────
    yield ("Compiling search queries for each requirement...", None)
    queries: list[str] = compiler.compile_queries(segments)
    yield (f"Generated {len(queries)} search queries.", None)

    # ── Web Search (Dev B's responsibility in production) ──────────────────
    if web_context_fn is not None:
        yield ("Fetching web context via search agent...", None)
        web_contexts: list[list[str]] = []
        for i, query in enumerate(queries):
            yield (f"  Searching [{i + 1}/{len(queries)}]: {query[:60]}...", None)
            try:
                context = web_context_fn(query)
            except Exception as exc:
                logger.warning(f"Search failed for query {i + 1}: {exc}")
                context = []
            web_contexts.append(context)
    else:
        # Standalone Dev A mode: use empty contexts (no real search results)
        logger.warning(
            "No web_context_fn provided. Running in mock mode with empty context. "
            "Results will show No Match for all requirements."
        )
        yield ("⚠️  Running in mock mode — no search agent connected.", None)
        web_contexts = [[] for _ in segments]

    # ── Phase 4: Audit Engine ──────────────────────────────────────────────
    yield (f"Auditing {len(segments)} requirements against web context...", None)
    results: list[ComplianceRow] = []
    for audit_msg, audit_results in engine.audit_requirements(segments, web_contexts):
        yield (audit_msg, None)
        if audit_results is not None:
            results = audit_results
