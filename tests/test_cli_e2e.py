"""End-to-end: a user processes an invoice from the CLI and the system settles it.

One invoice that should sail through (clean, known vendor, exact PO match, under
the auto-pay ceiling) and one that should be caught (vendor not in the master).
The full path runs for real — upload, background pipeline, live LLM extract +
judge, deterministic validation, gate, payment — against a temp DB.

The human-review tests then take a held invoice and resolve it the way a reviewer
does from the console — approve (pays it) or reject (declines it). The hold itself
is anchored on a deterministic ERROR (unknown vendor), so it lands in review every
run regardless of the live judge.
"""

import asyncio

from backend.statuses import Status
from cli.client import approve, reject


def test_clean_invoice_pays_touchless(run_cli):
    """INV-1001: Widgets Inc., WidgetA×10 @ 250 + WidgetB×5 @ 500 = $5,000, all on
    an open PO at the authorized price. Nothing to flag → paid with no human."""
    result = run_cli("invoice_1001.txt")
    inv = result["invoice"]

    assert inv["status"] == "paid"
    assert inv["outcome"] == "paid"
    # Touchless payment means nothing blocked it.
    assert all(f["severity"] != "error" for f in result["findings"]), result["findings"]


def test_unknown_vendor_invoice_is_held(run_cli):
    """INV-1008: 'NoProd Industries' is not in the vendor master — a blocking
    ERROR. The gate holds it for a human regardless of what the judge recommends,
    so this outcome is stable even with live, nondeterministic LLM calls."""
    result = run_cli("invoice_1008.txt")
    inv = result["invoice"]

    assert inv["status"] == "needs_review"
    codes = {f["code"] for f in result["findings"]}
    assert "unknown_vendor" in codes, result["findings"]


def test_human_approves_held_invoice(seed_invoice):
    """A reviewer clears a held invoice from the CLI — the only forward move out of
    NEEDS_REVIEW. The system never auto-pays what it held, but a person can. The
    hold is seeded directly (no LLM); this test is about the human action."""
    invoice_id = seed_invoice(status=Status.NEEDS_REVIEW)

    result = asyncio.run(approve(invoice_id))
    inv = result["invoice"]
    assert inv["status"] == "paid"
    assert inv["outcome"] == "paid"


def test_human_rejects_held_invoice(seed_invoice):
    """A reviewer declines a held invoice from the CLI — the only path to REJECTED,
    with the reason recorded on the audit trail."""
    invoice_id = seed_invoice(status=Status.NEEDS_REVIEW)

    result = asyncio.run(reject(invoice_id, "vendor not in master; suspected fraud"))
    inv = result["invoice"]
    assert inv["status"] == "rejected"
    assert inv["outcome"] == "rejected"
    assert "human_reject" in [t["kind"] for t in result["trace"]], result["trace"]
