"""The invoice state machine.

Status is the invoice's durable *disposition* — where it rests — not which
pipeline stage is running (that's recorded per step in invoice_trace). Keeping
status coarse means the pipeline can grow stages without touching this machine.

  RECEIVED ──► PROCESSING ──► APPROVED ──► PAID          (touchless)
                   │              ▲
                   ├──► NEEDS_REVIEW ──┤   human: approve & pay
                   │              └────► REJECTED         human: decline a held invoice
                   ├──► SUPERSEDED                        exact-duplicate auto-dedup
                   └──► FAILED                            processing error (defensive)

The system never declines on its own: it routes anything it can't clear to
NEEDS_REVIEW with a category explaining why (backend/review.py). From there a
human resolves it — approve & pay, or reject to decline. REJECTED is only ever a
human move; no automated path reaches it.

Transitions are asserted: an illegal move is an impossible() state, never a
silent write. The LLM never drives a transition — only deterministic code does;
the judge's verdict is an input the gate reads, not a write it makes.
"""

from enum import Enum

from .impossible import impossible


class Status(str, Enum):
    RECEIVED = "received"          # ingested, not yet processed
    PROCESSING = "processing"      # pipeline running (extract … finalize)
    NEEDS_REVIEW = "needs_review"  # durable: waiting on a human
    APPROVED = "approved"          # cleared to pay (by the gate, or a human) — payment in flight
    PAID = "paid"                  # terminal: payment succeeded
    REJECTED = "rejected"          # terminal: a human declined to pay a held invoice
    SUPERSEDED = "superseded"      # terminal: exact duplicate of a processed invoice
    FAILED = "failed"              # terminal: unrecoverable processing error


TERMINAL: frozenset[Status] = frozenset(
    {Status.PAID, Status.REJECTED, Status.SUPERSEDED, Status.FAILED}
)

# Every active (non-terminal) state may also fail, so FAILED is folded in below.
_ALLOWED: dict[Status, set[Status]] = {
    Status.RECEIVED:     {Status.PROCESSING},
    Status.PROCESSING:   {Status.APPROVED, Status.NEEDS_REVIEW, Status.SUPERSEDED},
    Status.NEEDS_REVIEW: {Status.APPROVED, Status.REJECTED},  # human: approve & pay, or decline
    Status.APPROVED:     {Status.PAID},
    Status.PAID:         set(),
    Status.REJECTED:     set(),
    Status.SUPERSEDED:   set(),
    Status.FAILED:       set(),
}
for _s, _targets in _ALLOWED.items():
    if _s not in TERMINAL:
        _targets.add(Status.FAILED)


def can_transition(current: Status, new: Status) -> bool:
    return new in _ALLOWED[current]


def assert_transition(current: Status, new: Status) -> None:
    """Permit a legal transition; an illegal one is an impossible() state."""
    if not can_transition(current, new):
        impossible(
            f"illegal invoice status transition: {current.value} -> {new.value}",
            {
                "from": current.value,
                "to": new.value,
                "allowed": sorted(s.value for s in _ALLOWED[current]),
            },
        )


class Outcome(str, Enum):
    """The business-facing resting label, set explicitly when an invoice settles.
    It tracks alongside Status, but is its own column so reporting reads one field
    regardless of how the invoice got there. APPROVED is transient: the gate sets
    it, then payment immediately lands it on PAID."""
    PAID = "paid"
    APPROVED = "approved"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class PayDecision(str, Enum):
    """Pay-or-hold: the vocabulary shared by the judge's recommendation and the
    gate's routing. Unlike Status it is advice/routing, never a transition — the
    gate reads it, code makes the move."""
    PAY = "pay"
    HOLD = "hold"
