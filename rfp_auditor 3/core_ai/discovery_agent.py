"""
core_ai/discovery_agent.py
───────────────────────────
Phase 3 — Search Discovery Agent (Developer B)

Accepts a list of search query strings, executes them against the Tavily
Search API, and falls back to Google Custom Search on rate-limit or failure.
Returns structured SearchResponse objects that the backend bridge injects
into the audit engine as web context.

Fallback chain:
    1. Tavily Search API  (primary)
    2. Google Custom Search API  (fallback on 429 / timeout / error)
    3. Empty result  (if both fail, pipeline continues gracefully)

Usage:
    from core_ai.discovery_agent import DiscoveryAgent

    agent = DiscoveryAgent()
    response = agent.search("10GbE switch LACP IEEE 802.3ad datasheet")
    context_strings = [r.snippet for r in response.results]
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from typing import Optional

import requests
from pydantic import BaseModel, Field, HttpUrl, field_validator
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import (
    GOOGLE_CSE_API_KEY,
    GOOGLE_CSE_ENGINE_ID,
    MAX_SEARCH_WORKERS,
    SEARCH_INTER_REQUEST_DELAY_MS,
    TAVILY_API_KEY,
    TAVILY_SEARCH_DEPTH,
    get_logger,
)

logger: logging.Logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_GOOGLE_CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
_REQUEST_TIMEOUT_SECONDS = 15
_MAX_RESULTS_PER_QUERY = 5


# ── Enums ──────────────────────────────────────────────────────────────────


class SearchProvider(str, Enum):
    TAVILY = "tavily"
    GOOGLE_CSE = "google_cse"
    NONE = "none"


# ── Pydantic Models ────────────────────────────────────────────────────────


class SearchQuery(BaseModel):
    """Validated input query for the discovery agent."""

    query: str = Field(..., min_length=1, description="The search query string to execute.")
    max_results: int = Field(
        default=_MAX_RESULTS_PER_QUERY,
        ge=1,
        le=10,
        description="Maximum number of results to return.",
    )

    @field_validator("query")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Query must not be empty after stripping whitespace.")
        return stripped


class SearchResult(BaseModel):
    """
    A single search result from any provider.
    Both Tavily and Google CSE results are normalised into this shape.
    """

    title: str = Field(..., description="Page title from the search result.")
    url: str = Field(..., description="The canonical URL of the result page.")
    snippet: str = Field(..., description="Short excerpt / description from the result.")
    provider: SearchProvider = Field(..., description="Which API returned this result.")
    score: Optional[float] = Field(
        None, description="Relevance score returned by the provider (Tavily only)."
    )

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https://: got {v!r}")
        return v

    def to_context_string(self) -> str:
        """
        Serialise this result into a plain-text block suitable for injection
        into the audit engine's LLM context window.
        """
        return (
            f"Title: {self.title}\n"
            f"URL: {self.url}\n"
            f"Excerpt: {self.snippet}"
        )


class SearchResponse(BaseModel):
    """
    Aggregated response for a single query execution.
    Carries provenance metadata alongside the result list.
    """

    query: str = Field(..., description="The original query string that was searched.")
    results: list[SearchResult] = Field(
        default_factory=list, description="Ordered list of search results."
    )
    provider_used: SearchProvider = Field(
        ..., description="The provider that actually returned usable results."
    )
    fallback_triggered: bool = Field(
        default=False,
        description="True if the primary provider failed and fallback was used.",
    )
    error_message: Optional[str] = Field(
        None, description="Error detail if both providers failed."
    )
    latency_ms: float = Field(
        default=0.0, description="Wall-clock time for the search in milliseconds."
    )

    def to_context_list(self) -> list[str]:
        """
        Convert all results to context strings for the audit engine.
        Returns an empty list if no results were found.
        """
        return [r.to_context_string() for r in self.results]


# ── Internal HTTP helpers ──────────────────────────────────────────────────


class _RateLimitError(Exception):
    """Raised when a 429 response is received from any provider."""


class _SearchProviderError(Exception):
    """Raised for non-429 HTTP errors or malformed responses."""


# ── Tavily Provider ────────────────────────────────────────────────────────


def _call_tavily(query: SearchQuery) -> list[SearchResult]:
    """
    Execute a search against the Tavily Search API.

    Args:
        query: Validated SearchQuery object.

    Returns:
        List of SearchResult objects parsed from the Tavily response.

    Raises:
        _RateLimitError: On HTTP 429.
        _SearchProviderError: On other HTTP errors or malformed payloads.
    """
    if not TAVILY_API_KEY:
        raise _SearchProviderError("TAVILY_API_KEY is not configured.")

    payload: dict = {
        "api_key": TAVILY_API_KEY,
        "query": query.query,
        "search_depth": TAVILY_SEARCH_DEPTH,
        "include_answer": False,
        "max_results": query.max_results,
    }

    logger.debug(f"[Tavily] Executing query: {query.query!r}")

    try:
        response = requests.post(
            _TAVILY_ENDPOINT,
            json=payload,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise _SearchProviderError(f"Tavily request timed out after {_REQUEST_TIMEOUT_SECONDS}s: {exc}") from exc
    except requests.ConnectionError as exc:
        raise _SearchProviderError(f"Tavily connection error: {exc}") from exc

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "unknown")
        logger.warning(f"[Tavily] Rate limit hit (429). Retry-After: {retry_after}")
        raise _RateLimitError(f"Tavily rate limit exceeded. Retry-After: {retry_after}")

    if not response.ok:
        raise _SearchProviderError(
            f"Tavily HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        data: dict = response.json()
    except ValueError as exc:
        raise _SearchProviderError(f"Tavily response is not valid JSON: {exc}") from exc

    raw_results: list[dict] = data.get("results", [])
    if not isinstance(raw_results, list):
        raise _SearchProviderError("Tavily response missing 'results' list.")

    parsed: list[SearchResult] = []
    for item in raw_results:
        try:
            parsed.append(
                SearchResult(
                    title=str(item.get("title", "No title")),
                    url=str(item.get("url", "https://example.com")),
                    snippet=str(item.get("content", item.get("snippet", "No excerpt available."))),
                    provider=SearchProvider.TAVILY,
                    score=float(item["score"]) if "score" in item else None,
                )
            )
        except Exception as exc:
            logger.warning(f"[Tavily] Skipping malformed result item: {exc} | item={item}")

    logger.debug(f"[Tavily] Returned {len(parsed)} results for query: {query.query!r}")
    return parsed


# ── Google Custom Search Provider ─────────────────────────────────────────


def _call_google_cse(query: SearchQuery) -> list[SearchResult]:
    """
    Execute a search against the Google Custom Search JSON API.
    Acts as the fallback when Tavily fails or rate-limits.

    Args:
        query: Validated SearchQuery object.

    Returns:
        List of SearchResult objects parsed from the Google CSE response.

    Raises:
        _RateLimitError: On HTTP 429.
        _SearchProviderError: On other HTTP errors or malformed payloads.
    """
    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_ENGINE_ID:
        raise _SearchProviderError(
            "GOOGLE_CSE_API_KEY or GOOGLE_CSE_ENGINE_ID is not configured."
        )

    params: dict = {
        "key": GOOGLE_CSE_API_KEY,
        "cx": GOOGLE_CSE_ENGINE_ID,
        "q": query.query,
        "num": min(query.max_results, 10),  # Google CSE max is 10
    }

    logger.debug(f"[GoogleCSE] Executing fallback query: {query.query!r}")

    try:
        response = requests.get(
            _GOOGLE_CSE_ENDPOINT,
            params=params,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise _SearchProviderError(f"Google CSE request timed out: {exc}") from exc
    except requests.ConnectionError as exc:
        raise _SearchProviderError(f"Google CSE connection error: {exc}") from exc

    if response.status_code == 429:
        logger.warning("[GoogleCSE] Rate limit hit (429).")
        raise _RateLimitError("Google CSE rate limit exceeded.")

    if not response.ok:
        raise _SearchProviderError(
            f"Google CSE HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        data: dict = response.json()
    except ValueError as exc:
        raise _SearchProviderError(f"Google CSE response is not valid JSON: {exc}") from exc

    items: list[dict] = data.get("items", [])
    if not isinstance(items, list):
        logger.warning("[GoogleCSE] Response contained no 'items' list — likely zero results.")
        return []

    parsed: list[SearchResult] = []
    for item in items:
        try:
            snippet_raw = item.get("snippet", item.get("htmlSnippet", "No excerpt available."))
            # Strip HTML tags from htmlSnippet if present
            snippet_clean = snippet_raw.replace("<b>", "").replace("</b>", "").replace("<br>", " ")
            parsed.append(
                SearchResult(
                    title=str(item.get("title", "No title")),
                    url=str(item.get("link", "https://example.com")),
                    snippet=snippet_clean,
                    provider=SearchProvider.GOOGLE_CSE,
                    score=None,
                )
            )
        except Exception as exc:
            logger.warning(f"[GoogleCSE] Skipping malformed result item: {exc} | item={item}")

    logger.debug(f"[GoogleCSE] Returned {len(parsed)} results for query: {query.query!r}")
    return parsed


# ── Main Discovery Agent ───────────────────────────────────────────────────


class DiscoveryAgent:
    """
    Production-grade web search orchestrator.

    Executes search queries via Tavily (primary) and falls back to Google
    Custom Search on 429 or transport failure. Includes retry logic,
    structured logging, and graceful degradation.

    Typical usage:
        agent = DiscoveryAgent()
        response = agent.search("firewall throughput 100Gbps datasheet")
        context_strings = response.to_context_list()
    """

    def __init__(
        self,
        tavily_api_key: Optional[str] = None,
        google_cse_api_key: Optional[str] = None,
        google_cse_engine_id: Optional[str] = None,
    ) -> None:
        """
        Initialise the discovery agent.

        API keys fall back to the centralised config/settings.py values.
        Pass overrides here only for testing or multi-tenant scenarios.

        Args:
            tavily_api_key:      Override TAVILY_API_KEY from env.
            google_cse_api_key:  Override GOOGLE_CSE_API_KEY from env.
            google_cse_engine_id: Override GOOGLE_CSE_ENGINE_ID from env.
        """
        import config.settings as _cfg

        # Allow runtime key injection while preserving settings defaults
        if tavily_api_key:
            _cfg.TAVILY_API_KEY = tavily_api_key
        if google_cse_api_key:
            _cfg.GOOGLE_CSE_API_KEY = google_cse_api_key
        if google_cse_engine_id:
            _cfg.GOOGLE_CSE_ENGINE_ID = google_cse_engine_id

        _tavily_configured = bool(TAVILY_API_KEY or tavily_api_key)
        _google_configured = bool(
            (GOOGLE_CSE_API_KEY or google_cse_api_key)
            and (GOOGLE_CSE_ENGINE_ID or google_cse_engine_id)
        )

        logger.info(
            f"DiscoveryAgent initialised. "
            f"Tavily: {'✓' if _tavily_configured else '✗ (key missing)'}  "
            f"GoogleCSE: {'✓' if _google_configured else '✗ (key/ID missing)'}"
        )

    # ── Public API ─────────────────────────────────────────────────────────

    def search(self, query_string: str, max_results: int = _MAX_RESULTS_PER_QUERY) -> SearchResponse:
        """
        Execute a web search for the given query string.

        Tries Tavily first; on 429 or failure, falls back to Google CSE.
        Returns a SearchResponse regardless of outcome — errors are surfaced
        inside the model rather than raised, so the audit pipeline is never
        blocked by a single search failure.

        Args:
            query_string: The search query string.
            max_results:  Maximum results to return per provider.

        Returns:
            SearchResponse with results and provenance metadata.
        """
        query = SearchQuery(query=query_string, max_results=max_results)
        start = time.perf_counter()

        # ── Attempt 1: Tavily ──────────────────────────────────────────────
        try:
            results = self._tavily_with_retry(query)
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                f"[DiscoveryAgent] Tavily: {len(results)} results "
                f"({latency_ms}ms) for: {query_string[:60]!r}"
            )
            return SearchResponse(
                query=query_string,
                results=results,
                provider_used=SearchProvider.TAVILY,
                fallback_triggered=False,
                latency_ms=latency_ms,
            )
        except _RateLimitError as exc:
            logger.warning(f"[DiscoveryAgent] Tavily rate-limited — triggering Google CSE fallback. {exc}")
        except _SearchProviderError as exc:
            logger.warning(f"[DiscoveryAgent] Tavily failed — triggering Google CSE fallback. {exc}")
        except RetryError as exc:
            logger.warning(f"[DiscoveryAgent] Tavily retries exhausted — triggering Google CSE fallback. {exc}")
        except Exception as exc:
            logger.error(f"[DiscoveryAgent] Unexpected Tavily error — triggering fallback. {exc}")

        # ── Attempt 2: Google Custom Search fallback ───────────────────────
        try:
            results = self._google_cse_with_retry(query)
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                f"[DiscoveryAgent] GoogleCSE fallback: {len(results)} results "
                f"({latency_ms}ms) for: {query_string[:60]!r}"
            )
            return SearchResponse(
                query=query_string,
                results=results,
                provider_used=SearchProvider.GOOGLE_CSE,
                fallback_triggered=True,
                latency_ms=latency_ms,
            )
        except _RateLimitError as exc:
            error_msg = f"Both providers rate-limited: {exc}"
            logger.error(f"[DiscoveryAgent] {error_msg}")
        except _SearchProviderError as exc:
            error_msg = f"Both providers failed: {exc}"
            logger.error(f"[DiscoveryAgent] {error_msg}")
        except RetryError as exc:
            error_msg = f"GoogleCSE retries exhausted: {exc}"
            logger.error(f"[DiscoveryAgent] {error_msg}")
        except Exception as exc:
            error_msg = f"Unexpected error in GoogleCSE fallback: {exc}"
            logger.error(f"[DiscoveryAgent] {error_msg}")
        else:
            error_msg = None  # type: ignore[assignment]

        # ── Both providers failed — return graceful empty response ─────────
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.warning(
            f"[DiscoveryAgent] All providers failed for query: {query_string[:60]!r}. "
            "Audit engine will proceed with empty context."
        )
        return SearchResponse(
            query=query_string,
            results=[],
            provider_used=SearchProvider.NONE,
            fallback_triggered=True,
            error_message=locals().get("error_msg"),
            latency_ms=latency_ms,
        )

    def batch_search(
        self,
        query_strings: list[str],
        max_results: int = _MAX_RESULTS_PER_QUERY,
        inter_request_delay_ms: int | None = None,
        max_workers: int | None = None,
    ) -> list[SearchResponse]:
        """
        Execute a batch of searches, one per query string.

        Searches run in parallel by default (bounded by MAX_SEARCH_WORKERS).
        Pass max_workers=1 for strictly sequential behaviour with optional delay.

        Args:
            query_strings:          List of query strings to search.
            max_results:            Maximum results per query.
            inter_request_delay_ms: Milliseconds to sleep between sequential requests.
                                    Defaults to SEARCH_INTER_REQUEST_DELAY_MS (0).
            max_workers:            Concurrent search workers. Defaults to MAX_SEARCH_WORKERS.

        Returns:
            List of SearchResponse objects in the same order as query_strings.
        """
        if not query_strings:
            return []

        delay_ms = (
            SEARCH_INTER_REQUEST_DELAY_MS
            if inter_request_delay_ms is None
            else inter_request_delay_ms
        )
        workers = max(1, min(max_workers or MAX_SEARCH_WORKERS, len(query_strings)))
        total = len(query_strings)
        responses: list[SearchResponse | None] = [None] * total

        logger.info(
            f"[DiscoveryAgent] Starting batch search: {total} queries "
            f"({workers} worker{'s' if workers != 1 else ''})."
        )

        if workers == 1:
            for i, query_str in enumerate(query_strings):
                logger.info(f"[DiscoveryAgent] Query [{i + 1}/{total}]: {query_str[:60]!r}")
                responses[i] = self.search(query_str, max_results=max_results)
                if i < total - 1 and delay_ms > 0:
                    time.sleep(delay_ms / 1000)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_idx = {
                    executor.submit(self.search, query_str, max_results): i
                    for i, query_str in enumerate(query_strings)
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    responses[idx] = future.result()
                    logger.debug(
                        f"[DiscoveryAgent] Completed query [{idx + 1}/{total}]: "
                        f"{query_strings[idx][:60]!r}"
                    )

        success_count = sum(1 for r in responses if r is not None and r.results)
        logger.info(
            f"[DiscoveryAgent] Batch complete. "
            f"{success_count}/{total} queries returned results."
        )
        return [r for r in responses if r is not None]

    def search_and_get_context(self, query_string: str) -> list[str]:
        """
        Convenience method for the backend bridge.
        Returns a flat list of context strings ready for injection into the
        audit engine, matching the `web_context_fn` signature expected by
        `core_ai.backend_bridge.run_audit_pipeline`.

        Args:
            query_string: The search query string.

        Returns:
            List of plain-text context strings from search results.
        """
        response = self.search(query_string)
        return response.to_context_list()

    # ── Retry-wrapped internal calls ───────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(_SearchProviderError),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _tavily_with_retry(self, query: SearchQuery) -> list[SearchResult]:
        """Tavily call with exponential backoff on transient errors only."""
        return _call_tavily(query)

    @retry(
        retry=retry_if_exception_type(_SearchProviderError),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _google_cse_with_retry(self, query: SearchQuery) -> list[SearchResult]:
        """Google CSE call with exponential backoff on transient errors only."""
        return _call_google_cse(query)