"""Business Intelligence: dashboards and cross-functional reports.

Reports read the *governing* (currently amended) terms by calling the
governing module directly - no API call, no data copy, always consistent.
"""
import csv
import io
import json
import sqlite3
import uuid
from collections import Counter
from datetime import date, timedelta

from . import governing, repository, tracking

PRESETS = {
    "sales": {"name": "Sales: customer agreements", "audience": "Sales",
              "filters": {"contract_type": ["Customer Agreement", "NDA"]},
              "columns": ["title", "counterparty", "contract_type", "contract_value", "expiration_date", "auto_renewal"]},
    "finance": {"name": "Finance: spend and renewals", "audience": "Finance",
                "filters": {"min_value": 1},
                "columns": ["title", "counterparty", "department", "contract_value", "payment_terms_days",
                            "auto_renewal", "renewal_notice_days", "expiration_date"]},
    "procurement": {"name": "Procurement: vendor contracts expiring in 180 days", "audience": "Procurement",
                    "filters": {"department": "Procurement", "expiring_within_days": 180},
                    "columns": ["title", "counterparty", "contract_type", "contract_value", "expiration_date",
                                "renewal_notice_days", "governing_law"]},
}


def _rows(conn: sqlite3.Connection) -> list[dict]:
    """One flat row per base contract: metadata + governing terms."""
    rows = []
    for c in repository.list_contracts(conn, include_amendments=False):
        terms = governing.current_terms(conn, c["id"])["current_terms"]
        rows.append({**c, **{k: v["value"] for k, v in terms.items()}, "amendment_count": len(
            repository.get(conn, c["id"])["amendments"])})
    return rows


def _status(row: dict, as_of: date) -> str:
    expiry = row.get("expiration_date")
    return "expired" if expiry and expiry < as_of.isoformat() and not row.get("auto_renewal") else "active"


def dashboard(conn: sqlite3.Connection, as_of: date) -> dict:
    rows = _rows(conn)
    active = [r for r in rows if _status(r, as_of) == "active"]

    def expiring(days):
        return sum(1 for r in active if r.get("expiration_date")
                   and as_of.isoformat() <= r["expiration_date"] <= (as_of + timedelta(days=days)).isoformat())

    return {
        "as_of": as_of.isoformat(),
        "contracts": len(rows),
        "active": len(active),
        "total_active_value": sum(r.get("contract_value") or 0 for r in active),
        "by_type": Counter(r["contract_type"] for r in rows),
        "by_department": Counter(r["department"] for r in rows),
        "value_by_department": {d: sum(r.get("contract_value") or 0 for r in active if r["department"] == d)
                                for d in sorted({r["department"] for r in active})},
        "expiring": {f"{d}_days": expiring(d) for d in (30, 60, 90, 180)},
        "auto_renewing": sum(1 for r in active if r.get("auto_renewal")),
        "uncapped_liability": [r["title"] for r in active if r.get("liability_cap") == "UNLIMITED"],
        "upcoming_deadlines": tracking.upcoming(conn, as_of, 120)[:5],
    }


def save_report(conn: sqlite3.Connection, name: str, audience: str, filters: dict, columns: list[str]) -> dict:
    report_id = f"rpt_{uuid.uuid4().hex[:8]}"
    conn.execute("INSERT INTO reports (id, name, audience, filters, columns) VALUES (?, ?, ?, ?, ?)",
                 (report_id, name, audience, json.dumps(filters), json.dumps(columns)))
    return {"id": report_id, "name": name, "audience": audience, "filters": filters, "columns": columns}


def get_report(conn: sqlite3.Connection, report_id: str) -> dict | None:
    if report_id in PRESETS:
        return {"id": report_id, **PRESETS[report_id]}
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "name": row["name"], "audience": row["audience"],
            "filters": json.loads(row["filters"]), "columns": json.loads(row["columns"])}


def run_report(conn: sqlite3.Connection, report: dict, as_of: date) -> list[dict]:
    f = report["filters"]

    def keep(r):
        types = f.get("contract_type")
        if types and r["contract_type"] not in ([types] if isinstance(types, str) else types):
            return False
        if f.get("department") and r["department"] != f["department"]:
            return False
        if f.get("counterparty") and r["counterparty"] != f["counterparty"]:
            return False
        if f.get("min_value") and (r.get("contract_value") or 0) < f["min_value"]:
            return False
        if days := f.get("expiring_within_days"):
            limit = (as_of + timedelta(days=days)).isoformat()
            if not r.get("expiration_date") or not as_of.isoformat() <= r["expiration_date"] <= limit:
                return False
        return _status(r, as_of) == "active" or f.get("include_expired")

    return [{c: r.get(c) for c in report["columns"]} for r in _rows(conn) if keep(r)]


def to_csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
