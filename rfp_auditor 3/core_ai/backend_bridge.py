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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from config.settings import MAX_SEARCH_WORKERS, get_logger
from core_ai.gemini_client import GeminiClient
from core_ai.pdf_parser import PDFParser
from core_ai.query_compiler import QueryCompiler
from core_ai.audit_engine import AuditEngine
from shared.schemas import AuditJobConfig, ComplianceRow, ParsedSegment

logger = get_logger(__name__)


def _fetch_web_contexts_parallel(
    queries: list[str],
    web_context_fn: Callable[[str], list[str]],
    max_workers: int | None = None,
) -> Generator[tuple[str, list[list[str]] | None], None, None]:
    """
    Fetch web context for each query in parallel, deduplicating identical queries.

    Yields progress messages, then a final tuple with (sentinel, web_contexts).
    """
    if not queries:
        yield ("No search queries to run.", None)
        return

    workers = max(1, min(max_workers or MAX_SEARCH_WORKERS, len(queries)))
    unique_queries = list(dict.fromkeys(queries))
    cache: dict[str, list[str]] = {}

    yield (
        f"Fetching web context for {len(queries)} requirements "
        f"({len(unique_queries)} unique queries, {workers} workers)...",
        None,
    )

    if workers == 1:
        for i, query in enumerate(unique_queries):
            yield (f"  Searching [{i + 1}/{len(unique_queries)}]: {query[:60]}...", None)
            try:
                cache[query] = web_context_fn(query)
            except Exception as exc:
                logger.warning(f"Search failed for query {i + 1}: {exc}")
                cache[query] = []
    else:
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_query = {
                executor.submit(web_context_fn, query): query for query in unique_queries
            }
            for future in as_completed(future_to_query):
                query = future_to_query[future]
                try:
                    cache[query] = future.result()
                except Exception as exc:
                    logger.warning(f"Search failed for query: {exc}")
                    cache[query] = []
                completed += 1
                yield (
                    f"  Searching [{completed}/{len(unique_queries)}]: {query[:60]}...",
                    None,
                )

    web_contexts = [cache.get(query, []) for query in queries]
    if len(unique_queries) < len(queries):
        logger.info(
            f"Deduplicated search: {len(queries) - len(unique_queries)} "
            "repeated queries reused cached results."
        )
    yield ("Web context fetch complete.", web_contexts)


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
        web_contexts: list[list[str]] = []
        for status_msg, partial_contexts in _fetch_web_contexts_parallel(
            queries, web_context_fn
        ):
            if partial_contexts is not None:
                web_contexts = partial_contexts
            else:
                yield (status_msg, None)
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
    
    # Yield the final results after audit is complete
    yield (f"✅ Audit complete. {len(results)} rows ready for export.", results)
