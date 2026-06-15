"""Invoice repository — the single door for writing invoice rows.

Status changes go through set_status() so assert_transition() is unavoidable and
every transition is recorded on the trace. Callers (the HTTP handler, the
pipeline, the CLI) never UPDATE invoices.status directly — that's the contract
that gives the state machine teeth.
"""

import uuid

from . import tracing
from .impossible import impossible
from .statuses import Status, assert_transition
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
