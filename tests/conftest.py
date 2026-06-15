"""E2E harness: drive the CLI exactly as a user does — in-process over the same
HTTP API the frontend calls — against a throwaway database, so a test run never
touches the real app.db.

These tests make LIVE xAI calls (the extract + judge stages), so they need
XAI_API_KEY set and network access, and they're slow. They assert only on stable
outcomes, never on the judge's prose: the clean invoice pays touchless; the
unknown-vendor invoice is held no matter what the judge decides, because a
deterministic ERROR finding blocks the gate on its own.
"""

import asyncio
from pathlib import Path

import pytest

from backend import db, invoices
from backend.review import ReviewCategory
from backend.schemas import ExtractedInvoice
from backend.statuses import Outcome, Status
from backend.unit_of_work import unit_of_work
from cli.client import process

REPO_ROOT = Path(__file__).resolve().parent.parent
INVOICES = REPO_ROOT / "data" / "invoices"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point the whole app at a fresh, seeded temp DB for one test. db.connect()
    reads db.DB_PATH on every call (the background job thread included), so
    patching the module attribute isolates the test end to end."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")


@pytest.fixture
def run_cli():
    """Process an invoice by name through the real CLI entrypoint and return the
    settled invoice (row + line items + findings + trace)."""
    def _run(invoice_name: str) -> dict:
        return asyncio.run(process(str(INVOICES / invoice_name)))
    return _run


@pytest.fixture
def seed_invoice():
    """Create an invoice directly at a chosen resting status, without the pipeline.

    Lets a test that exercises a *downstream* action (human approve/reject) skip
    the live LLM extract+judge it doesn't care about. It still goes through the
    real repository writes and the real state-machine transitions
    (RECEIVED -> PROCESSING -> ...), so the seeded row is indistinguishable from a
    pipeline-produced one — only the LLM is skipped."""
    def _seed(*, status: Status = Status.NEEDS_REVIEW, vendor: str = "NoProd Industries",
              total: float = 9900.0, currency: str = "USD",
              review_category: ReviewCategory | None = ReviewCategory.UNKNOWN_VENDOR) -> int:
        extracted = ExtractedInvoice(
            invoice_number="INV-SEED", vendor_name=vendor, invoice_date=None, due_date=None,
            due_date_raw=None, currency=currency, line_items=[], other_charges=[],
            stated_subtotal=total, stated_tax=0.0, stated_total=total, payment_terms=None,
            po_reference=None, revision=None, legibility="good", issues_noticed=[],
        )
        with unit_of_work() as uow:
            invoice_id = invoices.create_invoice(uow, "(seeded)", "seed")
            invoices.save_extraction(uow, invoice_id, extracted)
            invoices.set_status(uow, invoice_id, Status.PROCESSING)
            if status == Status.PROCESSING:
                return invoice_id
            if status == Status.NEEDS_REVIEW:
                invoices.set_status(uow, invoice_id, Status.NEEDS_REVIEW)
                invoices.set_outcome(uow, invoice_id, Outcome.NEEDS_REVIEW)
                if review_category is not None:
                    invoices.set_review_category(uow, invoice_id, review_category)
                return invoice_id
            raise ValueError(f"seed_invoice does not support landing at {status}")
    return _seed
