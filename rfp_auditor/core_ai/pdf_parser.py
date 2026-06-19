"""
core_ai/pdf_parser.py
──────────────────────
Phase 2 — Bounded Layout Ingestion & Extraction (Developer A Lead)

Extracts structured text blocks from a PDF using PyMuPDF's layout bounding box
mode (`get_text("blocks")`). This preserves table structures and column alignment
that simple text extraction would flatten.

Key design decisions:
- Uses `get_text("blocks")` to get position-aware text blocks (x0, y0, x1, y1).
- Automatically filters headers, footers, and page numbers using a margin heuristic.
- Returns list[ParsedSegment] — the shared contract with Dev B's Excel exporter.

Usage:
    from core_ai.pdf_parser import PDFParser
    parser = PDFParser("path/to/rfp.pdf")
    segments = parser.extract(start_page=1, end_page=10)
"""

import re
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

from config.settings import get_logger
from shared.schemas import ParsedSegment

logger = get_logger(__name__)

# ── Heuristic constants for header/footer detection ───────────────────────
# Blocks within these fractional margins of the page height are likely
# headers/footers. Tune these if your RFP layout is unusual.
_TOP_MARGIN_FRACTION = 0.08     # top 8% of page → likely header
_BOTTOM_MARGIN_FRACTION = 0.92  # bottom 8% of page → likely footer

# Regex to detect standalone page number blocks (e.g., "1", "Page 2 of 10")
_PAGE_NUMBER_RE = re.compile(
    r"^\s*(page\s*)?\d+(\s*of\s*\d+)?\s*$",
    re.IGNORECASE,
)


class PDFParser:
    """
    Parses a PDF file page-by-page, extracting text blocks with their
    bounding box coordinates. Filters decorative/structural elements.
    """

    def __init__(self, pdf_path: str | Path) -> None:
        self.pdf_path = Path(pdf_path)
        if not self.pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {self.pdf_path}")
        self._doc: Optional[fitz.Document] = None

    # ── Context manager support ────────────────────────────────────────────

    def __enter__(self) -> "PDFParser":
        self._doc = fitz.open(str(self.pdf_path))
        logger.info(f"Opened PDF: {self.pdf_path.name} ({self._doc.page_count} pages)")
        return self

    def __exit__(self, *_) -> None:
        if self._doc:
            self._doc.close()

    # ── Public API ─────────────────────────────────────────────────────────

    def extract(
        self,
        start_page: int = 1,
        end_page: Optional[int] = None,
    ) -> list[ParsedSegment]:
        """
        Extract text segments from the specified page range.

        Args:
            start_page: 1-based first page to parse (inclusive).
            end_page:   1-based last page to parse (inclusive). Defaults to last page.

        Returns:
            Ordered list of ParsedSegment objects.
        """
        if self._doc is None:
            raise RuntimeError("Use PDFParser as a context manager: `with PDFParser(...) as p:`")

        total_pages = self._doc.page_count
        # Normalise to 0-based indexes for PyMuPDF
        first = max(0, start_page - 1)
        last = min(total_pages - 1, (end_page - 1) if end_page else total_pages - 1)

        logger.info(f"Extracting pages {first + 1}–{last + 1} of {total_pages}")

        segments: list[ParsedSegment] = []
        for page_idx in range(first, last + 1):
            page = self._doc[page_idx]
            page_segments = self._extract_page(page, page_number=page_idx + 1)
            segments.extend(page_segments)
            logger.debug(f"  Page {page_idx + 1}: {len(page_segments)} blocks extracted")

        logger.info(f"Total segments extracted: {len(segments)}")
        return segments

    # ── Private helpers ────────────────────────────────────────────────────

    def _extract_page(self, page: fitz.Page, page_number: int) -> list[ParsedSegment]:
        """
        Extract all valid text blocks from a single page.
        Each block from get_text("blocks") is a tuple:
          (x0, y0, x1, y1, text, block_no, block_type)
        block_type == 0 → text block; 1 → image block (skip images).
        """
        page_height = page.rect.height
        top_threshold = page_height * _TOP_MARGIN_FRACTION
        bottom_threshold = page_height * _BOTTOM_MARGIN_FRACTION

        raw_blocks = page.get_text("blocks")
        segments: list[ParsedSegment] = []

        for block in raw_blocks:
            x0, y0, x1, y1, text, block_no, block_type = block

            # Skip image blocks
            if block_type != 0:
                continue

            # Skip header/footer zone blocks
            if y0 < top_threshold or y1 > bottom_threshold:
                logger.debug(f"  Skipping margin block at y={y0:.1f}–{y1:.1f}")
                continue

            # Skip lone page number strings
            if _PAGE_NUMBER_RE.match(text):
                logger.debug(f"  Skipping page number block: {text!r}")
                continue

            # Skip empty or whitespace-only blocks
            stripped = text.strip()
            if not stripped:
                continue

            segments.append(
                ParsedSegment(
                    page_number=page_number,
                    block_index=block_no,
                    raw_text=stripped,
                    cleaned_text=None,  # Filled later by text_purifier (Dev B)
                    bbox=(x0, y0, x1, y1),
                )
            )

        return segments

    @property
    def page_count(self) -> int:
        """Total pages in the document. Requires open context."""
        if self._doc is None:
            raise RuntimeError("Open document first.")
        return self._doc.page_count
