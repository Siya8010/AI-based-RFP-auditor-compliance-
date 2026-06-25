"""
shared/schemas.py
─────────────────
IMMUTABLE DATA CONTRACT — co-authored by Developer A & Developer B.

This file is the single source of truth for all inter-module data shapes.
DO NOT modify field names without a sync call with both developers.
Both the AI auditing pipeline (Dev A) and the Excel exporter (Dev B) depend
on this schema being stable.

Last synced: [UPDATE THIS DATE ON EVERY SCHEMA CHANGE]
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, HttpUrl, field_validator


# ── Compliance Status Enum ─────────────────────────────────────────────────

class ComplianceStatus(str, Enum):
    FULL_MATCH = "Full Match"
    PARTIAL_MATCH = "Partial Match"
    NO_MATCH = "No Match"


# ── Primary Output Row (used by both Dev A output & Dev B Excel exporter) ──

class ComplianceRow(BaseModel):
    """
    One row of the RFP compliance audit output.
    Dev A produces a list[ComplianceRow] → Dev B consumes it in excel_exporter.py.
    """

    requirement: str = Field(
        ...,
        description="The original verbatim text snippet extracted from the RFP document.",
    )
    recommended_model: str = Field(
        ...,
        description="The vendor name and specific product model found to address this requirement.",
    )
    compliance_status: ComplianceStatus = Field(
        ...,
        description="Degree to which the recommended model satisfies the requirement.",
    )
    proof_justification: str = Field(
        ...,
        description="Evidence text cited from the web that justifies the compliance decision.",
    )
    source_url: str = Field(
        ...,
        description="The verified destination URL where proof_justification was sourced.",
    )

    @field_validator("requirement", "recommended_model", "proof_justification")
    @classmethod
    def must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field cannot be empty or whitespace-only.")
        return v.strip()

    @field_validator("source_url")
    @classmethod
    def basic_url_check(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"source_url must start with http:// or https://: got {v!r}")
        return v


# ── Ingestion Output (produced by Dev A Phase 2 parser) ───────────────────

class ParsedSegment(BaseModel):
    """
    A single extracted text block from the PDF, with its page context.
    Dev A's pdf_parser produces list[ParsedSegment] → fed into the audit pipeline.
    """

    page_number: int = Field(..., ge=1, description="1-based page number in the source PDF.")
    block_index: int = Field(..., ge=0, description="Zero-based index of the block on the page.")
    raw_text: str = Field(..., description="Raw extracted text from the bounding box.")
    cleaned_text: Optional[str] = Field(
        None,
        description="Text after ligature removal and whitespace normalisation (set by text_purifier).",
    )
    bbox: tuple[float, float, float, float] = Field(
        ...,
        description="Bounding box coordinates (x0, y0, x1, y1) from PyMuPDF.",
    )


# ── Audit Job Config (passed into the audit engine) ───────────────────────

class AuditJobConfig(BaseModel):
    """
    Configuration object that drives one full audit run.
    Allows the Gradio UI (Dev B) to pass structured settings to Dev A's backend.
    """

    pdf_path: str = Field(..., description="Absolute path to the RFP PDF file.")
    start_page: int = Field(1, ge=1, description="First page to parse (1-based, inclusive).")
    end_page: Optional[int] = Field(None, description="Last page to parse (1-based, inclusive). None = all pages.")
    output_path: str = Field("output/audit_results.xlsx", description="Destination path for the Excel report.")
