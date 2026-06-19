"""
tests/test_pdf_parser.py
─────────────────────────
Unit tests for the PDF parser module.
Uses pytest-mock to avoid needing a real PDF in CI.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path

from core_ai.pdf_parser import PDFParser, _PAGE_NUMBER_RE


class TestPageNumberRegex:
    def test_bare_number(self):
        assert _PAGE_NUMBER_RE.match("1")
        assert _PAGE_NUMBER_RE.match("  42  ")

    def test_page_n_of_m(self):
        assert _PAGE_NUMBER_RE.match("Page 2 of 10")
        assert _PAGE_NUMBER_RE.match("page 1 of 5")

    def test_normal_text_not_matched(self):
        assert not _PAGE_NUMBER_RE.match("The system shall support 10GbE")
        assert not _PAGE_NUMBER_RE.match("Section 3: Requirements")


class TestPDFParser:
    @patch("core_ai.pdf_parser.fitz.open")
    def test_file_not_found_raises(self, mock_open):
        with pytest.raises(FileNotFoundError):
            PDFParser("/nonexistent/path.pdf")

    @patch("pathlib.Path.exists", return_value=True)
    @patch("core_ai.pdf_parser.fitz.open")
    def test_extract_requires_context_manager(self, mock_fitz, mock_exists):
        parser = PDFParser("/fake/path.pdf")
        with pytest.raises(RuntimeError, match="context manager"):
            parser.extract()

    @patch("pathlib.Path.exists", return_value=True)
    @patch("core_ai.pdf_parser.fitz.open")
    def test_extract_filters_page_numbers(self, mock_fitz, mock_exists):
        """Blocks matching the page-number pattern should be excluded."""
        mock_doc = MagicMock()
        mock_doc.page_count = 1
        mock_fitz.return_value = mock_doc

        mock_page = MagicMock()
        mock_page.rect.height = 800
        # One real block, one page-number block
        mock_page.get_text.return_value = [
            (50, 100, 500, 130, "The system shall provide 10GbE uplink ports.", 0, 0),
            (250, 780, 300, 800, "Page 1 of 10", 1, 0),
        ]
        mock_doc.__getitem__.return_value = mock_page

        parser = PDFParser("/fake/path.pdf")
        parser._doc = mock_doc
        segments = parser._extract_page(mock_page, page_number=1)

        assert len(segments) == 1
        assert "10GbE" in segments[0].raw_text

    @patch("pathlib.Path.exists", return_value=True)
    @patch("core_ai.pdf_parser.fitz.open")
    def test_extract_skips_image_blocks(self, mock_fitz, mock_exists):
        """Blocks with block_type=1 (images) should be skipped."""
        mock_doc = MagicMock()
        mock_page = MagicMock()
        mock_page.rect.height = 800
        mock_page.get_text.return_value = [
            (50, 100, 500, 130, "Some text requirement.", 0, 0),   # text block
            (50, 200, 500, 400, "", 1, 1),                          # image block
        ]
        mock_doc.__getitem__.return_value = mock_page

        parser = PDFParser("/fake/path.pdf")
        parser._doc = mock_doc
        segments = parser._extract_page(mock_page, page_number=1)
        assert len(segments) == 1
