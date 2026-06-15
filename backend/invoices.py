"""Invoice repository — the single door for writing invoice rows.

Status changes go through set_status() so assert_transition() is unavoidable and
every transition is recorded on the trace. Callers (the HTTP handler, the
pipeline, the CLI) never UPDATE invoices.status directly — that's the contract
that gives the state machine teeth.
"""

import json
import uuid
from enum import Enum

from . import payments, tracing
from .impossible import impossible
from .schemas import ExtractedInvoice
from .statuses import Outcome, PayDecision, Status, assert_transition
from .unit_of_work import UnitOfWork


def new_trace_id() -> str:
    return f"trc_{uuid.uuid4().hex[:12]}"


def create_invoice(
    uow: UnitOfWork, source_path: str, source_format: str, *, trace_id: str | None = None
) -> int:
    trace_id = trace_id or new_trace_id()
    invoice_id = uow.execute(
        "INSERT INTO invoices (trace_id, status, source_path, source_format) VALUES (?, ?, ?, ?)",
        (trace_id, Status.RECEIVED.value, source_path, source_format),
    ).lastrowid
    tracing.emit(
        uow, invoice_id, "lifecycle", "created",
        {"summary": f"received {source_format} from {source_path}", "status": Status.RECEIVED.value},
    )
    return invoice_id


def get_status(uow: UnitOfWork, invoice_id: int) -> Status:
    rows = uow.query("SELECT status FROM invoices WHERE id = ?", (invoice_id,))
    if not rows:
        impossible("status lookup on a missing invoice", {"invoice_id": invoice_id})
    return Status(rows[0]["status"])


def set_status(uow: UnitOfWork, invoice_id: int, new: Status) -> None:
    current = get_status(uow, invoice_id)
    assert_transition(current, new)
    uow.execute(
        "UPDATE invoices SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (new.value, invoice_id),
    )
    tracing.emit(
        uow, invoice_id, "lifecycle", "transition",
        {"from": current.value, "to": new.value, "summary": f"{current.value} -> {new.value}"},
    )


def save_extraction(uow: UnitOfWork, invoice_id: int, ex: ExtractedInvoice) -> None:
    """Persist the extracted fields onto the invoice row and its line items. The
    full extraction (other_charges, issues_noticed, legibility) stays in the
    trace; the columns hold what later stages match and decide against."""
    charges = sum(c.amount for c in ex.other_charges) if ex.other_charges else None
    uow.execute(
        "UPDATE invoices SET invoice_number=?, vendor_raw=?, currency=?, invoice_date=?,"
        " due_date=?, due_date_raw=?, po_reference=?, revision=?, payment_terms=?, stated_subtotal=?,"
        " stated_tax=?, stated_charges=?, stated_total=?, updated_at=datetime('now') WHERE id=?",
        (ex.invoice_number, ex.vendor_name, ex.currency, ex.invoice_date, ex.due_date,
         ex.due_date_raw, ex.po_reference, ex.revision, ex.payment_terms, ex.stated_subtotal,
         ex.stated_tax, charges, ex.stated_total, invoice_id),
    )
    for li in ex.line_items:
        uow.execute(
            "INSERT INTO invoice_line_items (invoice_id, item_raw, quantity, unit_price, line_total, note)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (invoice_id, li.item_raw, li.quantity, li.unit_price, li.line_total, li.note),
        )


def _enum_val(x: object) -> object:
    return x.value if isinstance(x, Enum) else x


def record_findings(uow: UnitOfWork, invoice_id: int, findings: list, source: str) -> None:
    """Append findings (deterministic Finding objects or the judge's concerns).
    Each carries .code, .severity, .message, .details — enums or plain strings."""
    for f in findings:
        uow.execute(
            "INSERT INTO findings (invoice_id, code, severity, message, details, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (invoice_id, _enum_val(f.code), _enum_val(f.severity), f.message,
             json.dumps(getattr(f, "details", {}) or {}), source),
        )


def save_validation(uow: UnitOfWork, invoice_id: int, result) -> None:
    """Persist the deterministic match: resolved vendor, fingerprint, the catalog
    identity matched onto each line, and the findings."""
    uow.execute(
        "UPDATE invoices SET vendor_id=?, fingerprint=?, updated_at=datetime('now') WHERE id=?",
        (result.vendor_id, result.fingerprint, invoice_id))
    for m in result.line_matches:
        if m.matched_item is not None:
            uow.execute("UPDATE invoice_line_items SET matched_item=?, matched_po_line_id=? WHERE id=?",
                        (m.matched_item, m.po_line_id, m.line_id))
    record_findings(uow, invoice_id, result.findings, source="deterministic")


def save_verdict(uow: UnitOfWork, invoice_id: int, verdict) -> None:
    """Persist the judge's advisory: pay/hold, the alarm level, the summary, the set
    of categories (each rated 1-10; the highest denormalized onto the row as the
    primary), and the qualitative concerns as findings."""
    cats = sorted(verdict.categories, key=lambda c: c.importance, reverse=True)
    primary = cats[0].category if cats else None
    uow.execute(
        "UPDATE invoices SET recommendation=?, review_category=?, review_level=?, review_summary=?,"
        " updated_at=datetime('now') WHERE id=?",
        (_enum_val(PayDecision.PAY if verdict.pay else PayDecision.HOLD),
         _enum_val(primary), _enum_val(verdict.level), verdict.summary, invoice_id))
    record_categories(uow, invoice_id, cats)
    record_findings(uow, invoice_id, verdict.concerns, source="llm")


def record_categories(uow: UnitOfWork, invoice_id: int, cats: list) -> None:
    """Replace the invoice's category set — the source of truth; the row's
    review_category column holds the denormalized primary (highest importance)."""
    uow.execute("DELETE FROM review_categories WHERE invoice_id=?", (invoice_id,))
    for c in cats:
        uow.execute(
            "INSERT INTO review_categories (invoice_id, category, importance, reason) VALUES (?, ?, ?, ?)",
            (invoice_id, _enum_val(c.category), c.importance, c.reason))


def ensure_held_categories(uow: UnitOfWork, invoice_id: int, suggested: list) -> None:
    """Fallback so a held invoice is never uncategorized: if the judge named no
    categories, seed them from the deterministic suggestions (importance by order)."""
    if uow.query("SELECT 1 FROM review_categories WHERE invoice_id=? LIMIT 1", (invoice_id,)):
        return
    primary = None
    for i, cat in enumerate(suggested):
        uow.execute(
            "INSERT INTO review_categories (invoice_id, category, importance, reason) VALUES (?, ?, ?, ?)",
            (invoice_id, _enum_val(cat), max(1, 6 - i), "raised by the deterministic checks"))
        primary = primary or cat
    if primary is not None:
        uow.execute("UPDATE invoices SET review_category=? WHERE id=?", (_enum_val(primary), invoice_id))


def set_outcome(uow: UnitOfWork, invoice_id: int, outcome: Outcome, *, superseded_by: int | None = None) -> None:
    uow.execute(
        "UPDATE invoices SET outcome=?, superseded_by=?, updated_at=datetime('now') WHERE id=?",
        (_enum_val(outcome), superseded_by, invoice_id))


def apply_po_drawdown(uow: UnitOfWork, invoice_id: int) -> dict:
    """Consume the matched PO lines on payment: add each paid line's quantity to
    its PO line's qty_invoiced, then close any PO now fully drawn down. This is
    what makes over-billing *across* invoices catchable, and retires a fulfilled
    PO so later invoices against it surface as unauthorized. Returns a summary
    for the trace."""
    lines = uow.query(
        "SELECT matched_po_line_id, quantity FROM invoice_line_items"
        " WHERE invoice_id=? AND matched_po_line_id IS NOT NULL AND quantity IS NOT NULL",
        (invoice_id,))
    touched_pos: set[int] = set()
    for ln in lines:
        uow.execute("UPDATE po_lines SET qty_invoiced = qty_invoiced + ? WHERE id=?",
                    (ln["quantity"], ln["matched_po_line_id"]))
        touched_pos.add(uow.query("SELECT po_id FROM po_lines WHERE id=?",
                                  (ln["matched_po_line_id"],))[0]["po_id"])

    closed: list[str] = []
    for po_id in touched_pos:
        open_lines = uow.query(
            "SELECT COUNT(*) AS n FROM po_lines WHERE po_id=? AND qty_invoiced < qty_ordered",
            (po_id,))[0]["n"]
        if open_lines == 0:
            uow.execute("UPDATE purchase_orders SET status='closed' WHERE id=?", (po_id,))
            closed.append(uow.query("SELECT po_number FROM purchase_orders WHERE id=?",
                                    (po_id,))[0]["po_number"])
    return {"lines_drawn": len(lines), "pos_closed": closed}


def pay(uow: UnitOfWork, invoice_id: int, *, stage: str, trigger: str = "") -> dict:
    """Move money for an APPROVED invoice and land it on PAID: call the payment
    rail, draw down the matched PO lines, set PAID + the paid outcome, and trace
    it. The single payment path — both the touchless gate and a human review
    approval call this, so the pay -> drawdown -> PAID -> outcome -> trace
    sequence exists in exactly one place and the two can't drift.

    `stage`/`trigger` only colour the trace (what set the payment off); the
    money-moving sequence is identical either way. Returns the payment receipt."""
    inv = uow.query(
        "SELECT vendor_raw, stated_total, currency FROM invoices WHERE id=?", (invoice_id,))[0]
    receipt = payments.pay(inv["vendor_raw"] or "(unknown)", inv["stated_total"] or 0.0, inv["currency"])
    drawdown = apply_po_drawdown(uow, invoice_id)
    set_status(uow, invoice_id, Status.PAID)
    set_outcome(uow, invoice_id, Outcome.PAID)
    tracing.emit(uow, invoice_id, stage, "payment",
                 {"summary": f"{trigger}paid {receipt['amount']} {receipt['currency']} to {receipt['vendor']}",
                  "reference": receipt["reference"], "drawdown": drawdown})
    return receipt


def set_review_category(uow: UnitOfWork, invoice_id: int, category) -> None:
    """Gate fallback: stamp a deterministic category when the judge held an
    invoice without naming one (it should, but the gate must never leave a held
    invoice uncategorized)."""
    uow.execute("UPDATE invoices SET review_category=?, updated_at=datetime('now') WHERE id=?",
                (_enum_val(category), invoice_id))


def reject(uow: UnitOfWork, invoice_id: int, reason: str, *, stage: str = "review") -> None:
    """Record a human's decision to decline a held invoice: NEEDS_REVIEW -> REJECTED,
    with the reviewer's reason on the trace. The only path to REJECTED — no automated
    route reaches it (see statuses.py)."""
    set_status(uow, invoice_id, Status.REJECTED)
    set_outcome(uow, invoice_id, Outcome.REJECTED)
    tracing.emit(uow, invoice_id, stage, "human_reject",
                 {"summary": f"reviewer rejected: {reason}", "reason": reason})


def add_review_note(uow: UnitOfWork, invoice_id: int, note: str, *, stage: str = "review") -> None:
    """Append a reviewer note to the trace without moving the invoice — e.g. a
    request for more information on a held invoice. Keeps the audit trail complete."""
    tracing.emit(uow, invoice_id, stage, "note", {"summary": f"reviewer note: {note}", "note": note})


_CORRECTABLE = ("vendor_raw", "invoice_number", "currency", "due_date", "payment_terms", "stated_total")


def apply_corrections(uow: UnitOfWork, invoice_id: int, fields: dict, line_edits: list) -> int:
    """Apply a reviewer's corrections to misread fields, recording each change
    (old -> new) on the trace. Returns the number of values changed. Fixing a due
    date also clears the unparseable due_date_raw so the corrected value is shown."""
    row = uow.query("SELECT * FROM invoices WHERE id=?", (invoice_id,))[0]
    changed = 0
    for field, new in fields.items():
        if field not in _CORRECTABLE or row[field] == new:
            continue
        uow.execute(f"UPDATE invoices SET {field}=?, updated_at=datetime('now') WHERE id=?", (new, invoice_id))
        tracing.emit(uow, invoice_id, "review", "human_edit",
                     {"summary": f"corrected {field}: {row[field]} -> {new}", "field": field, "from": row[field], "to": new})
        changed += 1
        if field == "due_date" and new is not None and row["due_date_raw"] is not None:
            uow.execute("UPDATE invoices SET due_date_raw=NULL WHERE id=?", (invoice_id,))

    for edit in line_edits:
        rows = uow.query("SELECT * FROM invoice_line_items WHERE id=? AND invoice_id=?", (edit.get("id"), invoice_id))
        if not rows:
            continue
        li = rows[0]
        for field in ("item_raw", "quantity", "unit_price"):
            if field in edit and edit[field] != li[field]:
                uow.execute(f"UPDATE invoice_line_items SET {field}=? WHERE id=?", (edit[field], li["id"]))
                tracing.emit(uow, invoice_id, "review", "human_edit",
                             {"summary": f"corrected line {li['id']} {field}: {li[field]} -> {edit[field]}",
                              "field": f"line.{field}", "line_id": li["id"], "from": li[field], "to": edit[field]})
                changed += 1
    return changed


def revalidate(uow: UnitOfWork, invoice_id: int) -> None:
    """Re-run ONLY the deterministic checks after a correction: drop the prior
    deterministic findings + line matches, re-match against vendors/POs, and record
    the refreshed result. The judge's verdict (recommendation/category/level/summary)
    is deliberately left untouched — it predates the edit, and the UI flags it stale."""
    from . import validation

    uow.execute("DELETE FROM findings WHERE invoice_id=? AND source='deterministic'", (invoice_id,))
    uow.execute("UPDATE invoice_line_items SET matched_item=NULL, matched_po_line_id=NULL WHERE invoice_id=?", (invoice_id,))
    result = validation.validate(uow, invoice_id)
    save_validation(uow, invoice_id, result)
    tracing.emit(uow, invoice_id, "validate", "recheck",
                 {"summary": f"re-checked after correction: {len(result.findings)} finding(s), "
                             f"{'blocking' if result.blocking else 'clean'}",
                  "findings": [f.code.value for f in result.findings], "blocking": result.blocking})


def load_invoice(uow: UnitOfWork, invoice_id: int) -> dict:
    """The full invoice for an API/CLI response: row + line items + findings + trace."""
    rows = uow.query("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    if not rows:
        impossible("load of a missing invoice", {"invoice_id": invoice_id})
    return {
        "invoice": dict(rows[0]),
        "line_items": [dict(r) for r in uow.query(
            "SELECT item_raw, matched_item, quantity, unit_price, line_total, note FROM invoice_line_items"
            " WHERE invoice_id=? ORDER BY id", (invoice_id,))],
        "findings": [dict(r) for r in uow.query(
            "SELECT code, severity, message, source FROM findings WHERE invoice_id=? ORDER BY id",
            (invoice_id,))],
        "categories": [dict(r) for r in uow.query(
            "SELECT category, importance, reason FROM review_categories WHERE invoice_id=?"
            " ORDER BY importance DESC, id", (invoice_id,))],
        "trace": [{**dict(r), "payload": json.loads(r["payload"])} for r in uow.query(
            "SELECT seq, stage, kind, payload FROM invoice_trace WHERE invoice_id=? ORDER BY seq",
            (invoice_id,))],
    }
