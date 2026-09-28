"""Analyze monolith: everything is testable in-process, no network, no other services."""
import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SAMPLES = Path(__file__).resolve().parents[1] / "analyze" / "samples"
AS_OF = "2026-10-01"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYZE_DB", str(tmp_path / "analyze.db"))
    monkeypatch.delenv("FINALIZE_EVENT_BUS_URL", raising=False)
    from analyze_app.main import app

    with TestClient(app) as c:
        assert c.post("/ingest/sync", json={"folder": str(SAMPLES)}).status_code == 200
        yield c


def by_title(client, title):
    return next(c for c in client.get("/contracts").json() if c["title"] == title)


def test_extraction_and_categorization(client):
    msa = client.get(f"/contracts/{by_title(client, 'Master Services Agreement')['id']}").json()
    values = {v["key"]: v["value"] for v in msa["smart_values"]}
    assert msa["contract_type"] == "MSA" and msa["counterparty"] == "Globex Inc."
    assert values["effective_date"] == "2025-01-15"
    assert values["expiration_date"] == "2027-01-15"
    assert values["auto_renewal"] is True and values["renewal_notice_days"] == 60
    assert values["contract_value"] == 250000 and values["payment_terms_days"] == 45
    assert values["governing_law"] == "Delaware"
    types = {c["title"]: (c["contract_type"], c["department"]) for c in client.get("/contracts").json()}
    assert types["Office Lease"] == ("Lease", "Finance")
    assert types["Mutual Non-Disclosure Agreement"] == ("NDA", "Legal")
    assert types["Customer Subscription Agreement"] == ("Customer Agreement", "Sales")


def test_amendment_rolls_up_into_governing_summary(client):
    msa = by_title(client, "Master Services Agreement")
    amendment = by_title(client, "First Amendment To Master Services Agreement")
    assert amendment["parent_id"] == msa["id"]
    gov = client.get(f"/contracts/{msa['id']}/governing").json()
    terms = {k: v["value"] for k, v in gov["current_terms"].items()}
    assert terms["contract_value"] == 300000
    assert terms["term_months"] == 36
    assert terms["expiration_date"] == "2028-01-15"  # recalculated from the amended term
    assert terms["governing_law"] == "New York"
    assert gov["amended_sections"][0]["section"] == "9"


def test_amendment_moves_tracked_deadlines(client):
    events = client.get(f"/events/upcoming?days=2000&as_of={AS_OF}").json()
    msa_events = {e["kind"]: e["due_date"] for e in events if e["title"] == "Master Services Agreement"}
    assert msa_events == {"auto_renewal": "2028-01-15", "renewal_notice_deadline": "2027-11-16"}
    alerts = client.post(f"/events/scan?as_of={AS_OF}").json()["alerts"]
    assert [a["title"] for a in alerts] == []  # nothing inside 30 days on Oct 1
    alerts = client.post("/events/scan?as_of=2026-10-15").json()["alerts"]
    assert [a["title"] for a in alerts] == ["Customer Subscription Agreement"]


def test_dashboard_reports_and_search(client):
    d = client.get(f"/dashboard?as_of={AS_OF}").json()
    assert d["contracts"] == 5  # amendments roll up into their parent
    assert d["total_active_value"] == 300000 + 480000 + 1200000 + 75000
    assert d["uncapped_liability"] == ["Customer Subscription Agreement"]
    rows = client.get(f"/reports/procurement/run?as_of={AS_OF}").json()["rows"]
    assert [r["title"] for r in rows] == ["Supply Agreement"]
    custom = client.post("/reports", json={"name": "NY law", "columns": ["title", "governing_law"], "filters": {}}).json()
    csv = client.get(f"/reports/{custom['id']}/run?format=csv&as_of={AS_OF}").text
    assert "Master Services Agreement,New York" in csv
    assert [r["title"] for r in client.get("/search", params={"q": "premises"}).json()] == ["Office Lease"]
    assert "Contract portfolio" in client.get(f"/?as_of={AS_OF}").text


def test_ingest_is_idempotent_and_rejects_unknown_files(client):
    text = (SAMPLES / "03_initech_nda.txt").read_text()
    again = client.post("/ingest", json={"text": text}).json()
    assert again["created"] is False
    bad = client.post("/ingest", json={"filename": "x.exe", "content_base64": base64.b64encode(b"MZ").decode()})
    assert bad.status_code == 415


def test_finalize_event_is_ingested_once(client):
    event = {"id": "evt_1", "topic": "agreement.executed", "payload": {
        "agreement_id": "agr_1", "title": "Signed NDA", "counterparty": "Wayne Enterprises",
        "metadata": {"department": "Sales"},
        "content": "MUTUAL NON-DISCLOSURE AGREEMENT between Acme Corp (A) and Wayne Enterprises (B), "
                   "effective as of 2026-09-01. Confidential Information. Term of 12 months."}}
    first = client.post("/integrations/finalize/events", json=event).json()
    assert first["status"] == "ingested"
    assert client.post("/integrations/finalize/events", json=event).json()["status"] == "duplicate-ignored"
    contract = client.get(f"/contracts/{first['contract_id']}").json()
    assert (contract["source"], contract["department"], contract["contract_type"]) == ("finalize", "Sales", "NDA")
