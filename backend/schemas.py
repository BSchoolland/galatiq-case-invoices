"""Extraction schema: the structured-output contract for invoice documents.
These Pydantic models are the only channel through which raw-document content
enters the system — every extraction validates against ExtractedInvoice."""

from typing import Literal

from pydantic import BaseModel, Field


class ExtractedLineItem(BaseModel):
    item_raw: str = Field(description="Item name exactly as written on the document, typos and all")
    quantity: float
    unit_price: float | None = Field(description="Per-unit price; null if not stated")
    line_total: float | None = Field(description="Line amount as stated on the document; null if not stated")
    note: str | None = Field(description="Per-line note as written (e.g. 'rush order'), else null")


class ExtractedCharge(BaseModel):
    label: str = Field(description="e.g. 'Shipping', 'Handling' — non-item, non-tax charges")
    amount: float


class ExtractedInvoice(BaseModel):
    invoice_number: str | None = Field(description="Normalized to 'INV-NNNN' form when clearly intended, else verbatim; null if absent")
    vendor_name: str | None = Field(description="Issuing vendor's name as written; null if absent")
    invoice_date: str | None = Field(description="ISO YYYY-MM-DD if parseable, else null")
    due_date: str | None = Field(description="ISO YYYY-MM-DD if parseable, else null")
    due_date_raw: str | None = Field(description="Verbatim due-date text when it could not be parsed to ISO (e.g. 'yesterday'), else null")
    currency: str = Field(description="ISO 4217 code; infer USD from '$' if unstated")
    line_items: list[ExtractedLineItem]
    other_charges: list[ExtractedCharge]
    stated_subtotal: float | None
    stated_tax: float | None
    stated_total: float | None = Field(description="Grand total exactly as stated on the document — never recomputed")
    payment_terms: str | None
    po_reference: str | None = Field(description="Purchase-order number referenced on the document, if any")
    revision: str | None = Field(description="Revision marker (e.g. 'R1') if the document presents itself as a revised/amended invoice")
    legibility: Literal["good", "degraded", "illegible"] = Field(
        description="Self-assessment of source quality: 'degraded' if artifacts forced interpretation, 'illegible' if fields were unreadable")
    issues_noticed: list[str] = Field(
        description="Every judgment call made: typos interpreted, OCR artifacts, ambiguities, missing fields")
