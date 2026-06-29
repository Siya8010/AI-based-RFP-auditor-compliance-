"""
core_ai/query_compiler.py
──────────────────────────
Phase 3 Support — Search Query Compiler (Developer A Support Tasks)

Takes raw RFP segments and generates optimised web search query strings
using a LangChain prompt + Gemini. These queries are consumed by
Developer B's discovery_agent.py to feed the Tavily/Google CSE APIs.

The prompt instructs Gemini to produce targeted search operators
(e.g., `site:vendor.com "datasheet" filetype:pdf`) rather than
naïve keyword dumps, which would waste API quota on irrelevant results.

Integration contract with Dev B:
- Dev B calls `QueryCompiler.compile_queries(segments)` and gets back
  a list[str] of search queries — one per segment.
- Dev B's discovery_agent feeds each query into the Tavily API.

Usage:
    from core_ai.query_compiler import QueryCompiler
    from core_ai.gemini_client import GeminiClient

    client = GeminiClient()
    compiler = QueryCompiler(client)
    queries = compiler.compile_queries(segments)
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from config.settings import MAX_LLM_WORKERS, get_logger
from core_ai.gemini_client import GeminiClient
from shared.schemas import ParsedSegment

logger = get_logger(__name__)

_QUERY_COMPILER_SYSTEM_PROMPT = """
You are a technical procurement expert and expert web researcher.

Your job is to convert a raw technical requirement extracted from an RFP document into a precise, targeted web search query string. The goal is to find the exact vendor product datasheet, specification sheet, or product page that addresses this requirement.

Rules for the query:
1. Extract the core technical specification or product category from the requirement.
2. Include a vendor-scoping operator ONLY if the requirement mentions a specific vendor (e.g., `site:cisco.com`).
3. Add relevant document-type operators such as `"datasheet"`, `"spec sheet"`, or `filetype:pdf` when looking for specifications.
4. Keep the query under 12 words excluding operators.
5. Output ONLY the query string — no explanation, no quotes around the whole thing, no preamble.

Example input: "The system shall support 10GbE uplink ports with LACP bonding as per IEEE 802.3ad"
Example output: 10GbE switch LACP IEEE 802.3ad "datasheet" filetype:pdf
""".strip()

_QUERY_HUMAN_TEMPLATE = """
RFP REQUIREMENT:
{requirement}

Generate a single optimised search query string.
"""


class QueryCompiler:
    """
    Converts RFP requirement text into targeted web search query strings.
    """

    def __init__(self, client: GeminiClient) -> None:
        prompt = ChatPromptTemplate.from_messages([
            ("system", _QUERY_COMPILER_SYSTEM_PROMPT),
            ("human", _QUERY_HUMAN_TEMPLATE),
        ])
        self._chain = prompt | client.llm | StrOutputParser()
        logger.info("QueryCompiler initialised.")

    def compile_single(self, requirement_text: str) -> str:
        """
        Generate a search query for one requirement string.

        Args:
            requirement_text: Raw or cleaned RFP requirement text.

        Returns:
            A search query string ready to pass to Tavily or Google CSE.
        """
        logger.debug(f"Compiling query for: {requirement_text[:80]}...")
        try:
            query = self._chain.invoke({"requirement": requirement_text})
            query = query.strip().strip('"')
            logger.debug(f"  → Query: {query}")
            return query
        except Exception as exc:
            logger.error(f"Query compilation failed: {exc}")
            # Graceful degradation: use first 60 chars of requirement as fallback query
            return requirement_text[:60].strip()

    def compile_queries(
        self,
        segments: list[ParsedSegment],
        max_workers: int | None = None,
    ) -> list[str]:
        """
        Batch-compile search queries for all extracted PDF segments.

        Compilations run in parallel (bounded by MAX_LLM_WORKERS) to reduce
        wall-clock time on large PDFs.

        Args:
            segments: List of ParsedSegment from the PDF parser.
            max_workers: Override for concurrent LLM calls. Defaults to MAX_LLM_WORKERS.

        Returns:
            List of query strings in the same order as segments.
            Dev B's discovery_agent.py iterates this list.
        """
        if not segments:
            return []

        workers = max(1, min(max_workers or MAX_LLM_WORKERS, len(segments)))
        queries: list[str] = [""] * len(segments)

        if workers == 1:
            for i, segment in enumerate(segments):
                logger.info(f"Compiling query {i + 1}/{len(segments)}")
                text = segment.cleaned_text or segment.raw_text
                queries[i] = self.compile_single(text)
            logger.info(f"Query compilation complete. {len(queries)} queries generated.")
            return queries

        logger.info(
            f"Compiling {len(segments)} queries with {workers} parallel workers..."
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {
                executor.submit(
                    self.compile_single,
                    segment.cleaned_text or segment.raw_text,
                ): i
                for i, segment in enumerate(segments)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                queries[idx] = future.result()
                logger.debug(f"Compiled query {idx + 1}/{len(segments)}")

        logger.info(f"Query compilation complete. {len(queries)} queries generated.")
        return queries
