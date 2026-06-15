"""Smoke test for the extractor: run a spread of real invoices through
document -> ExtractedInvoice and print the structured JSON. Hits the live xAI
API. Each run creates a throwaway invoice row so the exchange is traced.

    python scripts/try_extract.py [file ...]
"""

import json
import sys
from pathlib import Path

from backend import agents, invoices
from backend.ingestion import load_document
from backend.llm import RunContext
from backend.statuses import Status
from backend.unit_of_work import unit_of_work

SAMPLES = [
    "data/invoices/invoice_1001.txt",   # clean
    "data/invoices/invoice_1002.txt",   # messy: "INVOCE", "Vndr", abbreviations
    "data/invoices/invoice_1006.csv",
    "data/invoices/invoice_1004.json",
    "data/invoices/invoice_1014.xml",   # EUR
]


def main() -> None:
    files = sys.argv[1:] or SAMPLES
    for f in files:
        path = Path(f)
        doc = load_document(path)
        # One unit of work per invoice: it owns the connection and commits at the
        # boundary; extraction + its trace rows ride the same transaction.
        with unit_of_work() as uow:
            inv_id = invoices.create_invoice(uow, str(path), path.suffix.lstrip("."))
            invoices.set_status(uow, inv_id, Status.PROCESSING)
            ex = agents.extract(RunContext(uow=uow, invoice_id=inv_id), doc)
        print(f"\n===== {path.name}  (kind={doc.kind}, invoice_id={inv_id}) =====")
        print(json.dumps(ex.model_dump(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
