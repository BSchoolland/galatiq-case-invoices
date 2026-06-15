"""SQLite access. Init is lazy (not a lifespan hook): the CLI mounts the app via
ASGITransport, which doesn't run lifespan events. Schema and reference data live
in schema.sql / seed.sql; both are applied idempotently on first connect."""

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("GALATIQ_DB_PATH") or Path(__file__).resolve().parent.parent / "app.db")
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
SEED_PATH = Path(__file__).resolve().parent / "seed.sql"

# Keyed by path, not a bare flag, so a test can point DB_PATH at a fresh temp
# file and get a clean schema+seed applied to it without a stale guard blocking.
_initialized: set[Path] = set()


def _ensure_init() -> None:
    if DB_PATH in _initialized:
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_PATH.read_text())
        conn.executescript(SEED_PATH.read_text())
        conn.commit()
    finally:
        conn.close()
    _initialized.add(DB_PATH)


def connect() -> sqlite3.Connection:
    _ensure_init()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")  # wait out brief writer contention before erroring
    return conn
