"""Smart Values: turn unstructured contract text into structured, queryable data.

Pure functions (text in, values out). A production system would put an ML/LLM
extractor behind the same `extract()` signature.
"""
import re
from datetime import date, datetime

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE = rf"(?:(?:{MONTHS})\s+\d{{1,2}},\s*\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/\d{{4}}|\d{{1,2}}\s+(?:{MONTHS})\s+\d{{4}})"
NUM = r"(?:[a-z\-]+\s+)?\(?(\d+)\)?"  # "sixty (60)" or "60"


def parse_date(raw: str) -> date | None:
    raw = re.sub(r"\s+", " ", raw.strip())
    for fmt in ("%B %d, %Y", "%B %d,%Y", "%Y-%m-%d", "%m/%d/%Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError(d)


def _snippet(text: str, m: re.Match) -> str:
    return text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ").strip()


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.;])\s+", text.replace("\n", " "))


def extract(text: str) -> dict[str, dict]:
    """Returns {key: {"value", "confidence", "snippet"}}."""
    out: dict[str, dict] = {}

    def put(key, value, confidence, m=None):
        out[key] = {"value": value, "confidence": confidence, "snippet": _snippet(text, m) if m else None}

    if m := re.search(r"between\s+(.+?)\s+\([^)]*\)\s+and\s+(.+?)\s+\(", text, re.I | re.S):
        put("parties", [m.group(1).strip(" ,"), m.group(2).strip(" ,")], 0.9, m)
    elif m := re.search(r"between\s+([A-Z][\w&.\- ]+?)\s+and\s+([A-Z][\w&.\- ]+?)[,.]", text):
        put("parties", [m.group(1).strip(), m.group(2).strip()], 0.7, m)

    if m := re.search(rf"effective\s+(?:as\s+of\s+|date\s+(?:is\s+|of\s+)?|on\s+)?({DATE})", text, re.I):
        if d := parse_date(m.group(1)):
            put("effective_date", d.isoformat(), 0.9, m)

    if m := re.search(rf"term[^.]{{0,40}}?(?:of|extended to|is)\s+{NUM}\s+(month|year)s?", text, re.I):
        months = int(m.group(1)) * (12 if m.group(2).lower() == "year" else 1)
        put("term_months", months, 0.85, m)

    if m := re.search(rf"(?:expire|terminate)s?\s+on\s+({DATE})", text, re.I):
        if d := parse_date(m.group(1)):
            put("expiration_date", d.isoformat(), 0.9, m)
    elif "effective_date" in out and "term_months" in out:
        start = date.fromisoformat(out["effective_date"]["value"])
        put("expiration_date", add_months(start, out["term_months"]["value"]).isoformat(), 0.6)

    if m := re.search(r"automatically\s+renew", text, re.I):
        put("auto_renewal", True, 0.9, m)
        for sentence in _sentences(text):
            if re.search(r"renew", sentence, re.I) and (n := re.search(rf"(?:at least|no less than|not less than)\s+{NUM}\s+days", sentence, re.I)):
                put("renewal_notice_days", int(n.group(1)), 0.85, n)
                break

    if m := re.search(r"governed by(?:\s+and\s+construed\s+in\s+accordance\s+with)?\s+the\s+laws\s+of\s+(?:the\s+State\s+of\s+)?([A-Z][A-Za-z ]+?)(?=[,.]|\s+without)", text):
        put("governing_law", m.group(1).strip(), 0.9, m)

    if m := re.search(r"(?:fees|contract value|consideration|price)[^$.]{0,60}\$\s?([\d,]+(?:\.\d{2})?)", text, re.I):
        put("contract_value", float(m.group(1).replace(",", "")), 0.85, m)
    elif m := re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", text):
        put("contract_value", float(m.group(1).replace(",", "")), 0.5, m)

    if m := re.search(r"\bnet\s*(\d+)\b", text, re.I) or re.search(rf"within\s+{NUM}\s+days\s+of\s+(?:receipt|the\s+date)\s+of\s+(?:the\s+)?invoice", text, re.I):
        put("payment_terms_days", int(m.group(1)), 0.8, m)

    if m := re.search(r"liability[^.]{0,120}?(?:shall not exceed|capped at|limited to)\s+([^.]+)\.", text, re.I):
        put("liability_cap", m.group(1).strip(), 0.75, m)
    elif m := re.search(r"unlimited liability", text, re.I):
        put("liability_cap", "UNLIMITED", 0.8, m)

    if m := re.search(r"terminat\w+[^.]{0,80}for\s+convenience", text, re.I):
        put("termination_for_convenience", True, 0.8, m)

    return out


def counterparty(parties: list[str] | None, our_company: str) -> str | None:
    if not parties:
        return None
    others = [p for p in parties if our_company.lower() not in p.lower()]
    return (others or parties)[0]
