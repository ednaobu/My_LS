"""The one shared database. Every module reads and writes these tables directly."""
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    counterparty    TEXT,
    contract_type   TEXT,
    department      TEXT,
    type_confidence REAL,
    parent_id       TEXT REFERENCES contracts(id),   -- amendments point at their base contract
    source          TEXT NOT NULL,                   -- upload | folder_sync | finalize
    source_ref      TEXT,
    filename        TEXT,
    content_hash    TEXT UNIQUE,
    ocr_confidence  REAL,
    text            TEXT NOT NULL,
    ingested_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS smart_values (
    contract_id TEXT NOT NULL REFERENCES contracts(id),
    key         TEXT NOT NULL,
    value       TEXT,             -- JSON encoded
    confidence  REAL,
    snippet     TEXT,
    PRIMARY KEY (contract_id, key)
);
CREATE TABLE IF NOT EXISTS tracked_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id TEXT NOT NULL REFERENCES contracts(id),
    kind        TEXT NOT NULL,    -- expiration | renewal_notice_deadline | auto_renewal
    due_date    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'upcoming',  -- upcoming | alerted
    note        TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    audience   TEXT,
    filters    TEXT NOT NULL,     -- JSON
    columns    TEXT NOT NULL      -- JSON
);
CREATE TABLE IF NOT EXISTS processed_events (
    id          TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS contracts_fts USING fts5(contract_id UNINDEXED, title, counterparty, text);
"""


def db_file() -> str:
    return os.environ.get("ANALYZE_DB", ".data/analyze.db")


def connect() -> sqlite3.Connection:
    path = db_file()
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def transaction():
    """One unit of work across all modules: commit everything or nothing."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
