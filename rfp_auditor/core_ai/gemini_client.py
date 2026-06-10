"""
core_ai/gemini_client.py
─────────────────────────
Phase 1 — Gemini API Client & Connection Baseline (Developer A Lead)

Wraps the LangChain + Google Generative AI integration into a single,
reusable client. All other modules that need Gemini should use this
client rather than constructing their own.

Features:
- Validates API key on init (fail-fast).
- Exposes a base LLM instance and a structured-output-bound version.
- Includes a `health_check()` method to test latency and token stability.
- Uses tenacity for automatic retry on transient API errors.

Usage:
    from core_ai.gemini_client import GeminiClient
    client = GeminiClient()
    client.health_check()
"""

import time
from typing import Any, Type

from langchain_google_genai import ChatGoogleGenerativeAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config.settings import GOOGLE_API_KEY, GEMINI_MODEL, MAX_TOKENS, validate_env, get_logger

logger = get_logger(__name__)


class GeminiClient:
    """
    Singleton-style wrapper around the LangChain Gemini integration.
    Instantiate once and share across modules.
    """

    def __init__(self) -> None:
        validate_env()  # Fail fast if keys are missing

        self._llm = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL,
            google_api_key=GOOGLE_API_KEY,
            max_output_tokens=MAX_TOKENS,
            temperature=0.1,      # Low temp for deterministic compliance scoring
        )
        logger.info(f"GeminiClient initialised with model: {GEMINI_MODEL}")

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def llm(self) -> ChatGoogleGenerativeAI:
        """Raw LangChain LLM instance for prompt chains."""
        return self._llm

    def with_structured_output(self, schema: Type[Any]) -> Any:
        """
        Returns an LLM bound to a Pydantic schema for structured JSON output.
        Used in the audit engine to enforce ComplianceRow output format.

        Args:
            schema: A Pydantic BaseModel class (e.g., ComplianceRow).

        Returns:
            A runnable that outputs validated Pydantic objects.
        """
        return self._llm.with_structured_output(schema)

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def invoke(self, prompt: str) -> str:
        """
        Send a plain text prompt and return the response string.
        Includes automatic exponential-backoff retry for transient errors.

        Args:
            prompt: The prompt string to send to Gemini.

        Returns:
            The model's text response.
        """
        response = self._llm.invoke(prompt)
        return response.content

    def health_check(self) -> dict[str, Any]:
        """
        Phase 1 connection test: measures latency and verifies the API is reachable.
        Call this from main.py or a test to confirm the environment is correctly set up.

        Returns:
            Dict with keys: status, model, latency_ms, response_preview.
        """
        test_prompt = (
            "Respond with exactly one sentence confirming you are operational "
            "and state the current model name."
        )
        logger.info("Running Gemini health check...")
        start = time.perf_counter()
        try:
            response = self.invoke(test_prompt)
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            result = {
                "status": "OK",
                "model": GEMINI_MODEL,
                "latency_ms": elapsed_ms,
                "response_preview": response[:120],
            }
            logger.info(f"Health check passed — latency: {elapsed_ms}ms")
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            result = {
                "status": "FAILED",
                "model": GEMINI_MODEL,
                "latency_ms": elapsed_ms,
                "error": str(exc),
            }
            logger.error(f"Health check failed: {exc}")
        return result
