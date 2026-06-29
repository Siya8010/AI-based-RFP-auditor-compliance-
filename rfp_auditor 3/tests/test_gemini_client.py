"""
tests/test_gemini_client.py
────────────────────────────
Unit tests for the Gemini client.
Mocks the API call so tests can run without a real GOOGLE_API_KEY.
"""

import pytest
from unittest.mock import patch, MagicMock
from core_ai.gemini_client import GeminiClient


class TestGeminiClientHealthCheck:
    @patch.dict("os.environ", {"GOOGLE_API_KEY": "fake-test-key"})
    @patch("langchain_google_genai.ChatGoogleGenerativeAI")
    def test_health_check_ok(self, mock_chat_class):
        """Health check should return status OK when API responds."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "I am operational. Model: gemini-1.5-flash."
        mock_chat_class.return_value = mock_llm

        client = GeminiClient()
        result = client.health_check()

        assert result["status"] == "OK"
        assert "latency_ms" in result
        assert "response_preview" in result

    @patch.dict("os.environ", {"GOOGLE_API_KEY": "fake-test-key"})
    @patch("langchain_google_genai.ChatGoogleGenerativeAI")
    def test_health_check_failure(self, mock_chat_class):
        """Health check should return FAILED status on exception."""
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = Exception("API rate limit exceeded")
        mock_chat_class.return_value = mock_llm

        client = GeminiClient()
        result = client.health_check()

        assert result["status"] == "FAILED"
        assert "error" in result

    @patch("langchain_google_genai.ChatGoogleGenerativeAI")
    def test_missing_api_key_raises(self, mock_chat_class):
        """
        Client init should raise EnvironmentError if GOOGLE_API_KEY is absent.
        """
        with patch("config.settings.os.environ") as mock_env:
            mock_env.get = lambda key, default="": "" if key == "GOOGLE_API_KEY" else default
            with pytest.raises(EnvironmentError, match="GOOGLE_API_KEY"):
                GeminiClient()