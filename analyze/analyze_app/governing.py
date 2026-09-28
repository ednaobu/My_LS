"""Governing Summaries: roll every amendment up into the currently-effective terms."""
import re
import sqlite3
from datetime import date

from . import extraction, repository

# Terms an amendment may change. Anything it restates elsewhere is ignored.
AMENDABLE = ("contract_value", "term_months", "governing_law", "payment_terms_days", "auto_renewal",
             "renewal_notice_days", "liability_cap", "expiration_date")


def find_parent(conn: sqlite3.Connection, text: str, counterparty: str | None) -> str | None:
    """Link an amendment to the agreement it amends: same counterparty, matching 'dated ...' if given."""
    if not counterparty:
        return None
    candidates = conn.execute(
        "SELECT id FROM contracts WHERE counterparty = ? AND parent_id IS NULL AND contract_type != 'Amendment' "
        "ORDER BY ingested_at DESC", (counterparty,)).fetchall()
    if m := re.search(rf"dated\s+({extraction.DATE})", text):
        dated = extraction.parse_date(m.group(1))
        for c in candidates:
            if repository.values_of(conn, c["id"]).get("effective_date") == (dated and dated.isoformat()):
                return c["id"]
    return candidates[0]["id"] if candidates else None


def current_terms(conn: sqlite3.Connection, contract_id: str) -> dict:
    base = repository.get(conn, contract_id, with_text=True)
    if base is None:
        raise KeyError(contract_id)
    terms = {v["key"]: {"value": v["value"], "source": contract_id, "source_title": base["title"]}
             for v in base["smart_values"]}
    history, sections = [], []

    amendments = [repository.get(conn, a, with_text=True) for a in base["amendments"]]
    amendments.sort(key=lambda a: repository.values_of(conn, a["id"]).get("effective_date") or a["ingested_at"])
    for amendment in amendments:
        values = repository.values_of(conn, amendment["id"])
        changed = {}
        for key in AMENDABLE:
            explicit = key != "expiration_date" or re.search(r"(?:expire|terminate)s?\s+on", amendment["text"], re.I)
            if key in values and explicit and values[key] != terms.get(key, {}).get("value"):
                changed[key] = {"from": terms.get(key, {}).get("value"), "to": values[key]}
                terms[key] = {"value": values[key], "source": amendment["id"], "source_title": amendment["title"]}
        for number, heading, new_text in re.findall(
                r"Section\s+(\d+)\s*(?:\(([^)]*)\))?[^.:]*?(?:deleted and replaced|amended and restated)[^:]*:\s*\"?([^\"\n]+)\"?",
                amendment["text"], re.I):
            sections.append({"section": number, "heading": heading, "current_text": new_text.strip(), "source": amendment["id"]})
        history.append({"id": amendment["id"], "title": amendment["title"],
                        "effective_date": values.get("effective_date"), "changes": changed})

    # A changed term moves the expiration date unless an amendment set it explicitly.
    effective, term, expiry = terms.get("effective_date"), terms.get("term_months"), terms.get("expiration_date")
    if effective and term and term["source"] != contract_id and (not expiry or expiry["source"] == contract_id):
        recalculated = extraction.add_months(date.fromisoformat(effective["value"]), int(term["value"]))
        terms["expiration_date"] = {"value": recalculated.isoformat(), "source": term["source"],
                                    "source_title": f"{term['source_title']} (recalculated)"}

    return {
        "contract": {k: base[k] for k in ("id", "title", "counterparty", "contract_type", "department")},
        "current_terms": {k: v for k, v in sorted(terms.items())},
        "amendments": history,
        "amended_sections": sections,
    }
