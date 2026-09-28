"""Database-per-service: every service gets its own private SQLite file.

No service ever opens another service's database. If it needs that data it
asks the owning service over HTTP or keeps its own copy built from events.
"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def db_path(service: str) -> str:
    data_dir = os.environ.get("DATA_DIR", ".data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"{service}.db")


class Store:
    """A tiny JSON document store: collections of documents keyed by id."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS docs ("
                " collection TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,"
                " PRIMARY KEY (collection, id))"
            )
            self._conn.commit()

    def put(self, collection: str, doc: dict) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT INTO docs (collection, id, body) VALUES (?, ?, ?) "
                "ON CONFLICT(collection, id) DO UPDATE SET body = excluded.body",
                (collection, doc["id"], json.dumps(doc)),
            )
            self._conn.commit()
        return doc

    def get(self, collection: str, doc_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM docs WHERE collection = ? AND id = ?", (collection, doc_id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, collection: str, **filters) -> list[dict]:
        """All documents in insertion order, optionally filtered on top-level fields."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT body FROM docs WHERE collection = ? ORDER BY rowid", (collection,)
            ).fetchall()
        docs = [json.loads(r[0]) for r in rows]
        wanted = {k: v for k, v in filters.items() if v is not None}
        return [d for d in docs if all(d.get(k) == v for k, v in wanted.items())]

    def delete(self, collection: str, doc_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM docs WHERE collection = ? AND id = ?", (collection, doc_id))
            self._conn.commit()
