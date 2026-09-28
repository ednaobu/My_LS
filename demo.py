"""Walk through the full contract lifecycle across both architectures.

    python demo.py            # starts a fresh stack, runs the story, stops it
    python demo.py --attach   # use a stack already started with `python stack.py`

Finalize (microservices): intake -> draft -> counterparty redline -> AI review
-> approvals -> signature. Analyze (monolith): the executed agreement arrives
by event, then portfolio ingestion, amendments, deadlines and reporting.
"""
import argparse
import json
import time
from pathlib import Path

import httpx

from stack import Stack

AS_OF = "2026-10-01"


def title(text: str) -> None:
    print(f"\n\033[1m== {text}\033[0m")


def show(label: str, data) -> None:
    body = json.dumps(data, indent=2, default=str)
    lines = body.splitlines()
    print(f"-- {label}\n" + "\n".join(lines[:25]) + ("\n   ..." if len(lines) > 25 else ""))


def wait_for(fn, what: str, timeout: float = 15):
    """Asynchronous flows are eventually consistent: poll until the effect shows up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for {what}")


def run(stack: Stack) -> None:
    api = httpx.Client(base_url=f"{stack.url('gateway')}/api", headers={"X-API-Key": "dev-key"}, timeout=10)
    analyze = httpx.Client(base_url=stack.url("analyze"), timeout=10)

    def ok(response: httpx.Response):
        if response.is_error:
            raise RuntimeError(f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text}")
        return response.json()

    title("FINALIZE (microservices) - every call below goes through the API gateway")
    print("health:", ok(httpx.get(f"{stack.url('gateway')}/health/all")))

    title("1. Standardized intake: business user drafts an MSA via conditional Q&A (template service)")
    intake = ok(api.post("/intake", json={
        "title": "Globex analytics MSA", "counterparty": "Globex Inc.", "requester": "sam.sales",
        "department": "Sales", "project": "Analytics rollout", "template_id": "tpl_msa",
        "answers": {"effective_date": "October 1, 2026", "term_months": 24, "deal_value": 250_000,
                    "auto_renew": True, "processes_personal_data": True, "governing_law": "Delaware"}}))
    agreement_id = intake["agreement_id"]
    print(f"intake {intake['id']} -> status {intake['status']}, agreement {agreement_id}")

    risky = ok(api.post("/intake", json={
        "title": "Initech custom deal", "counterparty": "Initech LLC", "requester": "sam.sales", "template_id": "tpl_msa",
        "answers": {"effective_date": "October 1, 2026", "term_months": 36, "deal_value": 2_500_000, "auto_renew": False,
                    "processes_personal_data": False, "governing_law": "New York", "non_standard_terms": True}}))
    print(f"request funneling: second request -> {risky['status']}: {risky['legal_review_reasons']}")

    title("2. Version control: counterparty sends a redline (agreement service)")
    draft = ok(api.get(f"/agreements/{agreement_id}"))["content"]
    redline = (draft.replace("shall not exceed the fees paid in the twelve (12) months preceding the claim",
                             "shall be unlimited liability for all claims")
               .replace("payable Net 30", "payable Net 60"))
    ok(api.post(f"/agreements/{agreement_id}/versions",
                json={"content": redline, "author": "counsel@globex.com", "source": "external", "note": "Globex redline"}))
    print("versions:", [v["label"] for v in ok(api.get(f"/agreements/{agreement_id}/versions"))])
    print("diff v1 -> v2:", "\n  ".join(ok(api.get(f"/agreements/{agreement_id}/diff?from_version=1&to_version=2"))["diff"][2:]))

    title("3. AI review ran automatically on the redline (review service reacted to agreement.version_added)")
    review = wait_for(lambda: ok(api.get(f"/reviews?agreement_id={agreement_id}")), "AI review")[0]
    for issue in review["issues"]:
        print(f"  [{issue['severity']}] {issue['name']}: {issue['smart_edit']}")
    show("AI summary", ok(api.post("/summarize", json={"agreement_id": agreement_id})))
    print("Ask: 'what law governs this agreement?' ->",
          ok(api.post("/ask", json={"agreement_id": agreement_id, "question": "what law governs this agreement?"}))["answer"])

    title("4. Collaboration: internal thread on the agreement record (collaboration service)")
    ok(api.post(f"/agreements/{agreement_id}/comments",
                json={"author": "lee.legal", "body": "@sam.sales Globex removed our liability cap - pushing back."}))
    print("thread:", [c["body"] for c in ok(api.get(f"/agreements/{agreement_id}/comments"))])

    title("5. We accept the smart edits, then submit for approval")
    fixed = (redline.replace("shall be unlimited liability for all claims",
                             "shall not exceed the fees paid in the twelve (12) months preceding the claim")
             .replace("Net 60", "Net 30"))
    ok(api.post(f"/agreements/{agreement_id}/versions", json={"content": fixed, "author": "lee.legal", "note": "Accepted playbook smart edits"}))
    ok(api.post(f"/agreements/{agreement_id}/submit", json={"submitted_by": "lee.legal"}))
    workflow = wait_for(lambda: ok(api.get(f"/workflows?agreement_id={agreement_id}")), "approval workflow")[0]
    print("approval matrix produced:", [(t["step"], t["group"], t["condition"]) for t in workflow["tasks"]])

    title("6. Approvals: step 1 in parallel (legal + security), then finance (approval service)")
    for step in sorted({t["step"] for t in workflow["tasks"]}):
        pending = wait_for(lambda: [t for t in ok(api.get("/tasks")) if t["workflow_id"] == workflow["id"]], f"step {step} tasks")
        for task in pending:
            ok(api.post(f"/workflows/{workflow['id']}/tasks/{task['id']}/decision",
                        json={"decision": "approve", "approver": f"{task['group']}.lead"}))
            print(f"  step {task['step']}: {task['group']} approved")
    status = wait_for(lambda: (a := ok(api.get(f"/agreements/{agreement_id}")))["status"] == "ready_for_signature" and a["status"],
                      "ready_for_signature")
    print("agreement status (set by agreement service reacting to approval.workflow_completed):", status)

    title("7. Execution via LS Sign; Analyze picks it up from the event bus")
    ok(api.post(f"/agreements/{agreement_id}/sign", json={"signers": ["ceo@acme.com", "cfo@globex.com"], "provider": "ls_sign"}))
    print("hub:", {k: v for k, v in ok(api.get("/hub")).items() if k != "active"})
    show("audit trail", [f"{e['at'][11:19]} {e['actor']}: {e['action']} {e['details']}" for e in ok(api.get(f"/agreements/{agreement_id}/audit"))])
    show("notifications (Slack)", [f"@{n['recipient']}: {n['text']}" for n in ok(api.get("/notifications"))])
    # One HTTP request (the redline upload) fanned out across services; the correlation id ties it together.
    cid = ok(api.get("/events?topic=review.completed"))[0]["correlation_id"]
    show(f"trace of the redline request (correlation id {cid})",
         [f"{e['source']:>9} -> {e['topic']}" for e in ok(api.get(f"/events?correlation_id={cid}"))])

    title("ANALYZE (monolith) - one process, one database")
    executed = wait_for(lambda: [c for c in ok(analyze.get("/contracts")) if c["source"] == "finalize"], "Analyze ingestion")[0]
    print(f"executed agreement arrived in Analyze as {executed['id']} ({executed['contract_type']}, {executed['department']})")

    title("8. Portfolio ingestion from a synced folder (OCR -> extraction -> categorization -> tracking, one transaction)")
    for row in ok(analyze.post("/ingest/sync", json={"folder": str(Path(__file__).parent / "analyze" / "samples"), "provider": "s3"})):
        print(f"  {row['file']:<28} -> {row['contract_type']}")

    title("9. Governing summary: the MSA as amended")
    msa = next(c for c in ok(analyze.get("/contracts?counterparty=Globex Inc.&contract_type=MSA")) if c["source"] != "finalize")
    gov = ok(analyze.get(f"/contracts/{msa['id']}/governing"))
    for key in ("contract_value", "term_months", "expiration_date", "governing_law"):
        print(f"  {key:<16} {gov['current_terms'][key]['value']!s:<12} (from {gov['current_terms'][key]['source_title']})")

    title("10. Event tracking, dashboard, cross-functional reports")
    show("upcoming deadlines (120 days)", [f"{e['due_date']} {e['kind']}: {e['title']}" for e in ok(analyze.get(f"/events/upcoming?days=120&as_of={AS_OF}"))])
    show("dashboard", {k: v for k, v in ok(analyze.get(f"/dashboard?as_of={AS_OF}")).items() if k != "upcoming_deadlines"})
    print("-- finance report (CSV)\n" + analyze.get(f"/reports/finance/run?format=csv&as_of={AS_OF}").text)
    print("search 'unlimited liability':", [r["title"] for r in ok(analyze.get("/search", params={"q": "unlimited liability"}))])
    print(f"\nOpen {stack.url('analyze')}/?as_of={AS_OF} for the HTML dashboard.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--attach", action="store_true", help="use an already-running stack")
    args = parser.parse_args()
    if args.attach:
        run(Stack())
    else:
        stack = Stack().up(fresh=True)
        try:
            run(stack)
        finally:
            stack.down()
