"""Per-invoice audit trail: append each LLM exchange / tool call / decision to
invoice_trace. The wide-event idea at the domain grain — one ordered record
stream per invoice, queryable across requests. Token/timing metrics ride in the
payload so the column set stays the same across kinds.

Pure append: writes go through the UnitOfWork (so they're billed to the bound
event and share its transaction). Commit cadence is the caller's — the HTTP
handler's unit_of_work boundary, or the pipeline per stage."""

import json

from .unit_of_work import UnitOfWork


def emit(
    uow: UnitOfWork,
    invoice_id: int,
    node: str,
    kind: str,
    payload: dict,
    *,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    duration_ms: int | None = None,
) -> None:
    rows = uow.query("SELECT trace_id FROM invoices WHERE id = ?", (invoice_id,))
    trace_id = rows[0]["trace_id"] if rows else "unknown"
    seq = uow.query(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM invoice_trace WHERE invoice_id = ?", (invoice_id,)
    )[0]["n"]
    body = dict(payload)
    if tokens_in is not None or tokens_out is not None or duration_ms is not None:
        body["_meta"] = {"tokens_in": tokens_in, "tokens_out": tokens_out, "duration_ms": duration_ms}
    uow.execute(
        "INSERT INTO invoice_trace (invoice_id, trace_id, seq, stage, kind, payload)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (invoice_id, trace_id, seq, node, kind, json.dumps(body, default=str)),
    )
