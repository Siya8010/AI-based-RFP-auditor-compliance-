"""
config/settings.py
──────────────────
Centralised configuration management.
Reads from .env (local dev) or actual environment variables (CI/production).
All modules should import from here — never call os.environ directly.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (works regardless of where script is invoked)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


# ── Gemini / Google AI Studio ──────────────────────────────────────────────

GOOGLE_API_KEY: str = os.environ.get("GOOGLE_API_KEY", "")
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
MAX_TOKENS: int = int(os.environ.get("MAX_TOKENS", "8192"))

# ── Search APIs (used by Developer B; exported here for shared access) ─────

TAVILY_API_KEY: str = os.environ.get("TAVILY_API_KEY", "")
GOOGLE_CSE_API_KEY: str = os.environ.get("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_ENGINE_ID: str = os.environ.get("GOOGLE_CSE_ENGINE_ID", "")

# ── Application ────────────────────────────────────────────────────────────

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")


def validate_env() -> None:
    """
    Call at application startup to fail fast on missing critical keys.
    Reads os.environ live (not the module-level snapshot) so that
    test patches with patch.dict("os.environ", ...) work correctly.
    Raises EnvironmentError with a descriptive message.
    """
    missing = []
    if not os.environ.get("GOOGLE_API_KEY", ""):
        missing.append("GOOGLE_API_KEY")
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example → .env and fill in the values."
        )


def get_logger(name: str) -> logging.Logger:
    """
    Returns a consistently configured logger.
    Uses rich for pretty output when available.
    """
    try:
        from rich.logging import RichHandler
        logging.basicConfig(
            level=LOG_LEVEL,
            format="%(message)s",
            handlers=[RichHandler(rich_tracebacks=True)],
        )
    except ImportError:
        logging.basicConfig(
            level=LOG_LEVEL,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
    return logging.getLogger(name)
