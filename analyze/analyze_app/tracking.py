"""Automated Event Tracking: renewal deadlines and expirations, from the governing terms."""
import sqlite3
from datetime import date, timedelta

from . import governing

ALERT_WINDOW_DAYS = 30


def reschedule(conn: sqlite3.Connection, contract_id: str) -> list[dict]:
    """Recompute the tracked dates of a contract (called whenever it or an amendment is ingested)."""
    terms = {k: v["value"] for k, v in governing.current_terms(conn, contract_id)["current_terms"].items()}
    conn.execute("DELETE FROM tracked_events WHERE contract_id = ? AND status = 'upcoming'", (contract_id,))
    planned = []
    if expiry := terms.get("expiration_date"):
        expiry_date = date.fromisoformat(expiry)
        if terms.get("auto_renewal"):
            planned.append(("auto_renewal", expiry_date, "Renews automatically unless notice is given"))
            if days := terms.get("renewal_notice_days"):
                planned.append(("renewal_notice_deadline", expiry_date - timedelta(days=int(days)),
                                f"Last day to send {days}-day non-renewal notice"))
        else:
            planned.append(("expiration", expiry_date, "Agreement expires"))
    for kind, due, note in planned:
        conn.execute("INSERT INTO tracked_events (contract_id, kind, due_date, note) VALUES (?, ?, ?, ?)",
                     (contract_id, kind, due.isoformat(), note))
    return [{"kind": k, "due_date": d.isoformat(), "note": n} for k, d, n in planned]


def upcoming(conn: sqlite3.Connection, as_of: date, within_days: int) -> list[dict]:
    rows = conn.execute(
        "SELECT e.*, c.title, c.counterparty, c.department FROM tracked_events e JOIN contracts c ON c.id = e.contract_id "
        "WHERE e.due_date BETWEEN ? AND ? ORDER BY e.due_date",
        (as_of.isoformat(), (as_of + timedelta(days=within_days)).isoformat()),
    )
    return [dict(r) for r in rows]


def scan(conn: sqlite3.Connection, as_of: date) -> list[dict]:
    """Raise alerts for deadlines entering the alert window (run daily by a scheduler)."""
    due = [e for e in upcoming(conn, as_of, ALERT_WINDOW_DAYS) if e["status"] == "upcoming"]
    conn.executemany("UPDATE tracked_events SET status = 'alerted' WHERE id = ?", [(e["id"],) for e in due])
    return due
