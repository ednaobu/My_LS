"""Finalize microservices: end-to-end tests against the real, multi-process stack.

Unlike the monolith, the interesting behaviour lives *between* services, so
these tests start every process and also kill some to prove the resilience
patterns (outbox, retries, idempotency) and show the failure modes.
"""
import time

import httpx
import pytest

from stack import Stack

MSA_ANSWERS = {"effective_date": "October 1, 2026", "term_months": 24, "deal_value": 250_000, "auto_renew": True,
               "processes_personal_data": True, "governing_law": "Delaware"}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(base_port=18000, analyze_port=19000, data_dir=tmp_path_factory.mktemp("data")).up()
    yield s
    s.down()


@pytest.fixture(scope="module")
def api(stack):
    with httpx.Client(base_url=f"{stack.url('gateway')}/api", headers={"X-API-Key": "dev-key"}, timeout=10) as c:
        yield c


def eventually(fn, timeout=15):
    deadline = time.time() + timeout
    while True:
        try:
            result = fn()
            if result:
                return result
        except (AssertionError, httpx.HTTPError, KeyError, IndexError, StopIteration):
            pass
        if time.time() > deadline:
            raise TimeoutError("condition not met")
        time.sleep(0.2)


def new_agreement(api, **answers) -> str:
    body = {"title": "Test MSA", "counterparty": "Globex Inc.", "requester": "sam", "template_id": "tpl_msa",
            "answers": {**MSA_ANSWERS, **answers}}
    intake = api.post("/intake", json=body)
    assert intake.status_code == 201, intake.text
    return intake.json()["agreement_id"]


def approve_all(api, agreement_id):
    workflow = eventually(lambda: api.get(f"/workflows?agreement_id={agreement_id}&status=in_progress").json()[0])
    while True:
        pending = [t for t in api.get("/tasks").json() if t["workflow_id"] == workflow["id"]]
        if not pending:
            wf = api.get(f"/workflows/{workflow['id']}").json()
            if wf["status"] == "completed":
                return wf
            time.sleep(0.1)
            continue
        for t in pending:
            api.post(f"/workflows/{workflow['id']}/tasks/{t['id']}/decision", json={"decision": "approve", "approver": "x"})


def status(api, agreement_id):
    return api.get(f"/agreements/{agreement_id}").json()["status"]


def test_gateway_requires_api_key(stack):
    assert httpx.get(f"{stack.url('gateway')}/api/templates").status_code == 401


def test_happy_path_lifecycle_reaches_analyze(api, stack):
    agreement_id = new_agreement(api)
    api.post(f"/agreements/{agreement_id}/submit", json={"submitted_by": "sam"}).raise_for_status()
    workflow = approve_all(api, agreement_id)
    assert [t["group"] for t in workflow["tasks"]] == ["legal", "security", "finance"]
    eventually(lambda: status(api, agreement_id) == "ready_for_signature")
    api.post(f"/agreements/{agreement_id}/sign", json={"signers": ["a", "b"]}).raise_for_status()
    contract = eventually(lambda: next(c for c in httpx.get(f"{stack.url('analyze')}/contracts").json()
                                       if c["source_ref"] == agreement_id))
    assert contract["source"] == "finalize"


def test_sign_before_approval_is_rejected(api):
    agreement_id = new_agreement(api)
    assert api.post(f"/agreements/{agreement_id}/sign", json={"signers": ["a"]}).status_code == 409


def test_sequential_steps_block_later_approvers(api):
    agreement_id = new_agreement(api)
    api.post(f"/agreements/{agreement_id}/submit", json={"submitted_by": "sam"})
    wf = eventually(lambda: api.get(f"/workflows?agreement_id={agreement_id}").json()[0])
    finance = next(t for t in wf["tasks"] if t["group"] == "finance")
    r = api.post(f"/workflows/{wf['id']}/tasks/{finance['id']}/decision", json={"decision": "approve", "approver": "f"})
    assert r.status_code == 409  # step 2 is not active until step 1 is done


def test_rejection_requests_changes(api):
    agreement_id = new_agreement(api, deal_value=50_000, processes_personal_data=False)
    api.post(f"/agreements/{agreement_id}/submit", json={"submitted_by": "sam"})
    wf = eventually(lambda: api.get(f"/workflows?agreement_id={agreement_id}").json()[0])
    assert [t["group"] for t in wf["tasks"]] == ["legal"]  # matrix: small deal, no personal data
    api.post(f"/workflows/{wf['id']}/tasks/{wf['tasks'][0]['id']}/decision",
             json={"decision": "reject", "approver": "lee", "comment": "fix indemnity"})
    eventually(lambda: status(api, agreement_id) == "changes_requested")


def test_redline_triggers_ai_review(api):
    agreement_id = new_agreement(api)
    api.post(f"/agreements/{agreement_id}/versions", json={
        "content": "Supplier accepts unlimited liability. Payment Net 90.", "author": "them", "source": "external"})
    review = eventually(lambda: api.get(f"/reviews?agreement_id={agreement_id}").json()[0])
    assert {i["rule"] for i in review["issues"]} == {"no-unlimited-liability", "payment-net-30", "governing-law", "liability-cap"}
    assert status(api, agreement_id) == "in_negotiation"


def test_funneled_request_waits_for_legal(api):
    body = {"title": "Big deal", "counterparty": "Initech", "requester": "sam", "template_id": "tpl_msa",
            "answers": {**MSA_ANSWERS, "deal_value": 5_000_000}}
    intake = api.post("/intake", json=body).json()
    assert intake["status"] == "routed_to_legal" and intake["agreement_id"] is None
    accepted = api.post(f"/intake/{intake['id']}/accept", json={"legal_owner": "lee"}).json()
    assert accepted["status"] == "agreement_created"
    eventually(lambda: any("Big deal" in n["text"] for n in api.get("/notifications?recipient=legal").json()))


def test_duplicate_event_delivery_is_ignored(stack):
    event = {"id": "evt_dup", "topic": "review.completed", "source": "test", "occurred_at": "", "payload": {
        "agreement_id": "agr_x", "version": 1, "owner": "x", "issue_count": 0, "non_negotiable_violations": 0}}
    url = f"{stack.url('collaboration')}/events"
    assert httpx.post(url, json=event).json()["status"] == "processed"
    assert httpx.post(url, json=event).json()["status"] == "duplicate-ignored"


# ---- failure modes: what distribution costs, and how the patterns pay it back -------
def test_sync_dependency_down_fails_the_request(api, stack):
    """template -> agreement is a synchronous call: no agreement service, no intake."""
    stack.stop_service("agreement")
    try:
        body = {"title": "t", "counterparty": "c", "requester": "r", "template_id": "tpl_msa", "answers": MSA_ANSWERS}
        r = api.post("/intake", json=body)
        assert r.status_code == 503 and "agreement service unavailable" in r.json()["detail"]
        assert api.get("/templates").status_code == 200  # everything else keeps working
    finally:
        stack.start_service("agreement")
        stack.wait_healthy(["agreement"])


def test_event_bus_down_loses_nothing(api, stack):
    """Outbox pattern: writes succeed while the bus is down; events flow once it returns."""
    agreement_id = new_agreement(api)
    stack.stop_service("event_bus")
    try:
        r = api.post(f"/agreements/{agreement_id}/versions",
                     json={"content": "unlimited liability", "author": "them", "source": "external"})
        assert r.status_code == 201
        time.sleep(1)
        assert api.get(f"/reviews?agreement_id={agreement_id}").json() == []  # not reviewed yet
    finally:
        stack.start_service("event_bus")
        stack.wait_healthy(["event_bus"])
    eventually(lambda: api.get(f"/reviews?agreement_id={agreement_id}").json())


def test_consumer_down_gets_events_after_restart(api, stack):
    """At-least-once delivery: the bus retries a subscriber that is briefly down."""
    stack.stop_service("collaboration")
    try:
        agreement_id = new_agreement(api, deal_value=10_000, processes_personal_data=False)
        api.post(f"/agreements/{agreement_id}/submit", json={"submitted_by": "sam"})
        approve_all(api, agreement_id)
        eventually(lambda: status(api, agreement_id) == "ready_for_signature")  # approvals unaffected
    finally:
        stack.start_service("collaboration")
        stack.wait_healthy(["collaboration"])
    eventually(lambda: api.get(f"/notifications?agreement_id={agreement_id}").json())
