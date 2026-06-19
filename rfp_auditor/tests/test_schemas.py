"""
tests/test_schemas.py
──────────────────────
Unit tests for the shared Pydantic schema.
These ensure the data contract stays valid as both developers make changes.
"""

import pytest
from pydantic import ValidationError
from shared.schemas import ComplianceRow, ComplianceStatus, ParsedSegment, AuditJobConfig


class TestComplianceRow:
    def _valid_row(self, **overrides) -> dict:
        base = {
            "requirement": "System shall support VLAN tagging per IEEE 802.1Q",
            "recommended_model": "Cisco Catalyst 9300",
            "compliance_status": ComplianceStatus.FULL_MATCH,
            "proof_justification": "The Catalyst 9300 datasheet confirms 802.1Q VLAN support on all ports.",
            "source_url": "https://cisco.com/datasheet/9300",
        }
        base.update(overrides)
        return base

    def test_valid_row_passes(self):
        row = ComplianceRow(**self._valid_row())
        assert row.compliance_status == ComplianceStatus.FULL_MATCH

    def test_empty_requirement_raises(self):
        with pytest.raises(ValidationError):
            ComplianceRow(**self._valid_row(requirement="   "))

    def test_invalid_url_raises(self):
        with pytest.raises(ValidationError):
            ComplianceRow(**self._valid_row(source_url="not-a-url"))

    def test_all_compliance_statuses_valid(self):
        for status in ComplianceStatus:
            row = ComplianceRow(**self._valid_row(compliance_status=status))
            assert row.compliance_status == status

    def test_invalid_compliance_status_raises(self):
        with pytest.raises(ValidationError):
            ComplianceRow(**self._valid_row(compliance_status="Unknown Status"))


class TestParsedSegment:
    def test_valid_segment(self):
        seg = ParsedSegment(
            page_number=3,
            block_index=1,
            raw_text="Shall support dual PSU with hot-swap capability.",
            bbox=(50.0, 100.0, 540.0, 120.0),
        )
        assert seg.cleaned_text is None  # Optional, not set yet
        assert seg.page_number == 3

    def test_page_number_must_be_positive(self):
        with pytest.raises(ValidationError):
            ParsedSegment(
                page_number=0,
                block_index=0,
                raw_text="some text",
                bbox=(0, 0, 100, 20),
            )


class TestAuditJobConfig:
    def test_defaults(self):
        config = AuditJobConfig(pdf_path="/some/rfp.pdf")
        assert config.start_page == 1
        assert config.end_page is None

    def test_custom_range(self):
        config = AuditJobConfig(pdf_path="/rfp.pdf", start_page=5, end_page=20)
        assert config.start_page == 5
        assert config.end_page == 20
