"""Centralized Secure Repository: storage and search of executed agreements."""
import json
import sqlite3
import uuid

from .database import connect

CONTRACT_COLUMNS = ("id", "title", "counterparty", "contract_type", "department", "type_confidence", "parent_id",
                    "source", "source_ref", "filename", "ocr_confidence", "ingested_at")


def insert_contract(conn: sqlite3.Connection, **fields) -> str:
    fields.setdefault("id", f"ctr_{uuid.uuid4().hex[:10]}")
    cols = ", ".join(fields)
    conn.execute(f"INSERT INTO contracts ({cols}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values()))
    conn.execute("INSERT INTO contracts_fts (contract_id, title, counterparty, text) VALUES (?, ?, ?, ?)",
                 (fields["id"], fields["title"], fields.get("counterparty") or "", fields["text"]))
    return fields["id"]


def save_values(conn: sqlite3.Connection, contract_id: str, values: dict[str, dict]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO smart_values (contract_id, key, value, confidence, snippet) VALUES (?, ?, ?, ?, ?)",
        [(contract_id, k, json.dumps(v["value"]), v["confidence"], v["snippet"]) for k, v in values.items()],
    )


def values_of(conn: sqlite3.Connection, contract_id: str) -> dict:
    rows = conn.execute("SELECT key, value FROM smart_values WHERE contract_id = ?", (contract_id,))
    return {r["key"]: json.loads(r["value"]) for r in rows}


def find_by_hash(conn: sqlite3.Connection, content_hash: str) -> str | None:
    row = conn.execute("SELECT id FROM contracts WHERE content_hash = ?", (content_hash,)).fetchone()
    return row["id"] if row else None


def get(conn: sqlite3.Connection, contract_id: str, with_text: bool = False) -> dict | None:
    row = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
    if not row:
        return None
    contract = {k: row[k] for k in CONTRACT_COLUMNS + (("text",) if with_text else ())}
    contract["smart_values"] = [
        {"key": r["key"], "value": json.loads(r["value"]), "confidence": r["confidence"], "snippet": r["snippet"]}
        for r in conn.execute("SELECT * FROM smart_values WHERE contract_id = ? ORDER BY key", (contract_id,))
    ]
    contract["amendments"] = [r["id"] for r in conn.execute(
        "SELECT id FROM contracts WHERE parent_id = ? ORDER BY ingested_at", (contract_id,))]
    return contract


def list_contracts(conn: sqlite3.Connection, contract_type=None, department=None, counterparty=None,
                   include_amendments=True) -> list[dict]:
    sql, args = "SELECT * FROM contracts WHERE 1=1", []
    for col, val in (("contract_type", contract_type), ("department", department), ("counterparty", counterparty)):
        if val:
            sql += f" AND {col} = ?"
            args.append(val)
    if not include_amendments:
        sql += " AND parent_id IS NULL"
    return [{k: r[k] for k in CONTRACT_COLUMNS} for r in conn.execute(sql + " ORDER BY ingested_at", args)]


def search(q: str, limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT contract_id, title, counterparty, snippet(contracts_fts, 3, '[', ']', ' … ', 12) AS hit "
            "FROM contracts_fts WHERE contracts_fts MATCH ? ORDER BY rank LIMIT ?",
            (_fts_query(q), limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _fts_query(q: str) -> str:
    """Treat user input as plain words (all must match), not FTS syntax."""
    words = [w.replace('"', "") for w in q.split() if w.strip('"')]
    return " ".join(f'"{w}"' for w in words) or '""'
