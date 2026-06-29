"""
core_ai/audit_engine.py
────────────────────────
Phase 4 — Structured Auditing & Schema Enforcement (Developer A Tasks)

Runs each extracted RFP requirement through the Gemini model with structured
output bound to the ComplianceRow schema. Also manages the system prompt that
defines the scoring logic.

Design notes:
- The system prompt is the core intellectual asset of this module. Keep it
  version-controlled and iterate carefully.
- `with_structured_output(ComplianceRow)` means Gemini's response is
  automatically validated and returned as a Pydantic object — no manual
  JSON parsing needed.
- Accepts mock web context (list of strings) so Dev B can test the Excel
  exporter without a live search agent. In production, Dev B's discovery_agent
  will supply real search results.

Usage:
    from core_ai.audit_engine import AuditEngine
    from core_ai.gemini_client import GeminiClient

    client = GeminiClient()
    engine = AuditEngine(client)
    results = engine.audit_requirements(segments, web_contexts)
"""

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from config.settings import MAX_LLM_WORKERS, get_logger
from core_ai.gemini_client import GeminiClient
from shared.schemas import ComplianceRow, ComplianceStatus, ParsedSegment

logger = get_logger(__name__)

# ── System Prompt ──────────────────────────────────────────────────────────
# This is the authoritative scoring rubric. Both developers should agree on
# any changes to this prompt before committing.

_AUDIT_SYSTEM_PROMPT = """
You are a senior technical compliance analyst specialising in RFP (Request for Proposal) evaluation.

Your task is to evaluate whether a vendor product satisfies a specific technical requirement extracted from an RFP document. You will be given:
1. The REQUIREMENT text from the RFP.
2. CONTEXT — one or more web search results describing vendor products.

You must respond ONLY in structured JSON matching the required schema. Do not add any preamble or explanation outside the JSON.

Scoring rules:
- "Full Match": The context explicitly confirms the vendor product meets every aspect of the requirement.
- "Partial Match": The context shows the vendor product meets some but not all aspects, or the evidence is indirect.
- "No Match": The context provides no evidence the vendor product meets the requirement.

Be conservative: if evidence is ambiguous, prefer "Partial Match" over "Full Match".
Always cite a specific sentence or clause from the context as proof_justification — do not invent evidence.
If the context is empty or useless, set compliance_status to "No Match" and proof_justification to "Insufficient web evidence found."

CRITICAL JSON COMPLIANCE AND VALIDATION MANDATE:
You must strictly conform to the Pydantic JSON schema format. Empty text values or missing schemas will break the application pipeline.

1. "requirement" CANNOT be an empty string (""). You MUST always include the original requirement text from the input, verbatim or summarized if too long.
2. "recommended_model" CANNOT be an empty string (""). If you do not have a model recommendation or the status is "No Match", you MUST write "None" or "N/A".
3. "source_url" CANNOT be an empty string ("") and MUST begin with http:// or https://. If no web source or evidence link is available, you MUST explicitly output "https://localhost".
4. "proof_justification" CANNOT be an empty string (""). If no evidence is available, you MUST write "Insufficient web evidence found" or similar explanatory text.
""".strip()

_HUMAN_PROMPT_TEMPLATE = """
REQUIREMENT:
{requirement}

WEB SEARCH CONTEXT:
{web_context}

Evaluate this requirement against the context and return a structured compliance assessment.
"""


class AuditEngine:
    """
    Orchestrates the LLM-based compliance scoring of RFP requirements.
    """

    def __init__(self, client: GeminiClient) -> None:
        self._structured_llm: Runnable = client.with_structured_output(ComplianceRow)
        self._prompt = ChatPromptTemplate.from_messages([
            ("system", _AUDIT_SYSTEM_PROMPT),
            ("human", _HUMAN_PROMPT_TEMPLATE),
        ])
        self._chain = self._prompt | self._structured_llm
        logger.info("AuditEngine initialised with structured output chain.")

    def audit_single(
        self,
        requirement_text: str,
        web_context: list[str],
    ) -> ComplianceRow:
        """
        Audit a single requirement against provided web search results.

        Args:
            requirement_text: The raw RFP requirement text.
            web_context: List of strings from web search results (empty list = no evidence).

        Returns:
            A validated ComplianceRow Pydantic object.
        """
        context_block = "\n\n---\n\n".join(web_context) if web_context else "No web context provided."

        logger.debug(f"Auditing requirement: {requirement_text[:80]}...")
        try:
            result: ComplianceRow = self._chain.invoke({
                "requirement": requirement_text,
                "web_context": context_block,
            })
            logger.debug(f"  → Status: {result.compliance_status}")
            return result
        except Exception as exc:
            logger.error(f"Audit failed for requirement: {exc}")
            # Return a safe fallback rather than crashing the whole pipeline
            return ComplianceRow(
                requirement=requirement_text,
                recommended_model="AUDIT_ERROR",
                compliance_status=ComplianceStatus.NO_MATCH,
                proof_justification=f"Audit pipeline error: {exc}",
                source_url="https://example.com/error",
            )

    def audit_requirements(
        self,
        segments: list[ParsedSegment],
        web_contexts: list[list[str]],
        max_workers: int | None = None,
    ) -> Generator[tuple[str, list[ComplianceRow] | None], None, None]:
        """
        Batch-audit all extracted RFP segments.

        Audits run in parallel (bounded by MAX_LLM_WORKERS) while still yielding
        progress updates as each segment completes.

        Args:
            segments:     List of ParsedSegment from the PDF parser.
            web_contexts: Parallel list of web search result strings.
                          Index i of web_contexts corresponds to segments[i].
                          Dev B's discovery_agent populates this in production.
                          During Dev A development, pass mock strings for testing.
            max_workers:  Override for concurrent LLM calls. Defaults to MAX_LLM_WORKERS.

        Yields:
            (log_message: str, results: list[ComplianceRow] | None)
            Final yield has results populated; intermediate yields have results=None.
        """
        if len(segments) != len(web_contexts):
            raise ValueError(
                f"segments ({len(segments)}) and web_contexts ({len(web_contexts)}) "
                "must have the same length."
            )

        if not segments:
            yield ("Audit complete. 0 rows ready for export.", [])
            return

        workers = max(1, min(max_workers or MAX_LLM_WORKERS, len(segments)))
        results: list[ComplianceRow | None] = [None] * len(segments)
        completed = 0

        if workers == 1:
            for i, (segment, context) in enumerate(zip(segments, web_contexts)):
                logger.info(f"Auditing segment {i + 1}/{len(segments)} (page {segment.page_number})")
                text = segment.cleaned_text or segment.raw_text
                results[i] = self.audit_single(text, context)
                yield (
                    f"Auditing segment {i + 1}/{len(segments)} (page {segment.page_number})",
                    None,
                )
        else:
            logger.info(
                f"Auditing {len(segments)} segments with {workers} parallel workers..."
            )
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_idx = {}
                for i, (segment, context) in enumerate(zip(segments, web_contexts)):
                    text = segment.cleaned_text or segment.raw_text
                    future = executor.submit(self.audit_single, text, context)
                    future_to_idx[future] = i

                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    results[idx] = future.result()
                    completed += 1
                    segment = segments[idx]
                    logger.info(
                        f"Auditing segment {completed}/{len(segments)} "
                        f"(page {segment.page_number})"
                    )
                    yield (
                        f"Auditing segment {completed}/{len(segments)} "
                        f"(page {segment.page_number})",
                        None,
                    )

        final_results = [row for row in results if row is not None]
        logger.info(f"Audit complete. {len(final_results)} rows produced.")
        yield (f"Audit complete. {len(final_results)} rows ready for export.", final_results)
