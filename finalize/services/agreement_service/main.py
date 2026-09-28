"""Agreement service - Finalize "Version Control & Auditability" + state management.

Bounded context: the agreement record itself - its lifecycle state machine,
every version of its text, the negotiation hub and the audit trail.

Publishes: agreement.created, agreement.version_added, agreement.status_changed,
           agreement.submitted_for_approval, agreement.executed
Consumes:  approval.workflow_completed -> "ready_for_signature"
           approval.rejected           -> "changes_requested"
           review.completed            -> audit entry
"""
import difflib
from collections import Counter
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from lscommon.events import Events
from lscommon.service import create_service
from lscommon.store import Store, db_path, new_id, now_iso

store = Store(db_path("agreement"))
events = Events("agreement", store)


@asynccontextmanager
async def lifespan(_app):
    events.start()
    yield
    await events.stop()


app = create_service("agreement", lifespan=lifespan)

# Which status may follow which. Anything else is rejected with 409.
TRANSITIONS = {
    "draft": {"in_negotiation", "pending_approval"},
    "in_negotiation": {"pending_approval"},
    "pending_approval": {"ready_for_signature", "changes_requested"},
    "changes_requested": {"in_negotiation", "pending_approval"},
    "ready_for_signature": {"executed", "in_negotiation"},
    "executed": set(),
}
EDITABLE = {"draft", "in_negotiation", "changes_requested", "ready_for_signature"}


class NewAgreement(BaseModel):
    title: str
    counterparty: str
    owner: str
    content: str
    project: str = "General"
    requester: str | None = None
    template_id: str | None = None
    intake_id: str | None = None
    metadata: dict = Field(default_factory=dict)


class NewVersion(BaseModel):
    content: str
    author: str
    source: Literal["internal", "external"] = "internal"  # external = counterparty redline
    note: str = ""


class Submission(BaseModel):
    submitted_by: str


class Signature(BaseModel):
    signers: list[str]
    provider: Literal["ls_sign", "docusign", "adobe_sign"] = "ls_sign"


# ---- helpers -----------------------------------------------------------------
def _get(agreement_id: str) -> dict:
    agreement = store.get("agreements", agreement_id)
    if not agreement:
        raise HTTPException(404, "agreement not found")
    return agreement


def _audit(agreement_id: str, actor: str, action: str, **details) -> None:
    store.put(
        "audit",
        {"id": new_id("aud"), "agreement_id": agreement_id, "at": now_iso(), "actor": actor, "action": action, "details": details},
    )


def _transition(agreement: dict, status: str, actor: str, reason: str = "") -> None:
    if status == agreement["status"]:
        return
    if status not in TRANSITIONS[agreement["status"]]:
        raise HTTPException(409, f"cannot move from {agreement['status']} to {status}")
    previous, agreement["status"] = agreement["status"], status
    agreement["updated_at"] = now_iso()
    store.put("agreements", agreement)
    _audit(agreement["id"], actor, "status_changed", **{"from": previous, "to": status, "reason": reason})
    events.publish(
        "agreement.status_changed",
        {"agreement_id": agreement["id"], "from": previous, "to": status, "owner": agreement["owner"]},
    )


def _add_version(agreement: dict, body: NewVersion) -> dict:
    previous = store.get("versions", f"{agreement['id']}:{agreement['current_version']}") if agreement["current_version"] else None
    number = agreement["current_version"] + 1
    diff = list(difflib.unified_diff((previous or {}).get("content", "").splitlines(), body.content.splitlines(), lineterm=""))
    version = {
        "id": f"{agreement['id']}:{number}",
        "agreement_id": agreement["id"],
        "number": number,
        "label": f"v{number} ({'external redline' if body.source == 'external' else 'internal'})",
        **body.model_dump(),
        "created_at": now_iso(),
        "changes": {
            "lines_added": sum(1 for d in diff if d.startswith("+") and not d.startswith("+++")),
            "lines_removed": sum(1 for d in diff if d.startswith("-") and not d.startswith("---")),
        },
    }
    store.put("versions", version)
    agreement.update(current_version=number, updated_at=now_iso())
    store.put("agreements", agreement)
    _audit(agreement["id"], body.author, "version_added", version=number, source=body.source, note=body.note)
    events.publish(
        "agreement.version_added",
        {"agreement_id": agreement["id"], "version": number, "source": body.source, "author": body.author, "owner": agreement["owner"]},
    )
    return version


# ---- agreements & negotiation hub -------------------------------------------
@app.post("/agreements", status_code=201)
def create_agreement(body: NewAgreement):
    agreement = {
        "id": new_id("agr"),
        **body.model_dump(exclude={"content"}),
        "status": "draft",
        "current_version": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    store.put("agreements", agreement)
    _audit(agreement["id"], body.owner, "created", intake_id=body.intake_id, template_id=body.template_id)
    events.publish("agreement.created", {"agreement_id": agreement["id"], "title": body.title, "owner": body.owner})
    _add_version(agreement, NewVersion(content=body.content, author=body.owner, note="Initial draft"))
    return agreement


@app.get("/agreements")
def list_agreements(counterparty: str | None = None, project: str | None = None, status: str | None = None):
    return store.list("agreements", counterparty=counterparty, project=project, status=status)


@app.get("/hub")
def negotiation_hub(counterparty: str | None = None, project: str | None = None):
    """Dashboard of all active negotiations, filterable by counterparty or project."""
    agreements = store.list("agreements", counterparty=counterparty, project=project)
    active = [a for a in agreements if a["status"] != "executed"]
    return {
        "active_count": len(active),
        "by_status": Counter(a["status"] for a in agreements),
        "by_counterparty": Counter(a["counterparty"] for a in active),
        "by_project": Counter(a["project"] for a in active),
        "active": sorted(
            (
                {k: a[k] for k in ("id", "title", "counterparty", "project", "status", "current_version", "owner", "updated_at")}
                for a in active
            ),
            key=lambda a: a["updated_at"],
            reverse=True,
        ),
    }


@app.get("/agreements/{agreement_id}")
def get_agreement(agreement_id: str):
    agreement = _get(agreement_id)
    current = store.get("versions", f"{agreement_id}:{agreement['current_version']}")
    return {**agreement, "content": current["content"] if current else ""}


# ---- versions ------------------------------------------------------------------
@app.post("/agreements/{agreement_id}/versions", status_code=201)
def add_version(agreement_id: str, body: NewVersion):
    agreement = _get(agreement_id)
    if agreement["status"] not in EDITABLE:
        raise HTTPException(409, f"agreement is {agreement['status']}; it cannot be edited")
    if body.source == "external" or agreement["status"] in {"changes_requested", "ready_for_signature"}:
        # A redline (or any edit after approval) re-opens negotiation and voids approvals.
        _transition(agreement, "in_negotiation", body.author, f"new {body.source} version")
    return _add_version(agreement, body)


@app.get("/agreements/{agreement_id}/versions")
def list_versions(agreement_id: str):
    _get(agreement_id)
    return [{k: v for k, v in ver.items() if k != "content"} for ver in store.list("versions", agreement_id=agreement_id)]


@app.get("/agreements/{agreement_id}/versions/{number}")
def get_version(agreement_id: str, number: int):
    version = store.get("versions", f"{agreement_id}:{number}")
    if not version:
        raise HTTPException(404, "version not found")
    return version


@app.get("/agreements/{agreement_id}/diff")
def diff(agreement_id: str, from_version: int, to_version: int):
    a, b = get_version(agreement_id, from_version), get_version(agreement_id, to_version)
    return {
        "from": a["label"],
        "to": b["label"],
        "diff": list(difflib.unified_diff(a["content"].splitlines(), b["content"].splitlines(), a["label"], b["label"], lineterm="")),
    }


# ---- workflow state ------------------------------------------------------------
@app.post("/agreements/{agreement_id}/submit")
def submit_for_approval(agreement_id: str, body: Submission):
    agreement = _get(agreement_id)
    _transition(agreement, "pending_approval", body.submitted_by, "submitted for approval")
    # Event-carried state: the approval service gets everything it needs to
    # evaluate its matrix without calling back into this service.
    events.publish(
        "agreement.submitted_for_approval",
        {
            "agreement_id": agreement_id,
            "title": agreement["title"],
            "owner": agreement["owner"],
            "counterparty": agreement["counterparty"],
            "version": agreement["current_version"],
            "metadata": agreement["metadata"],
        },
    )
    return agreement


@app.post("/agreements/{agreement_id}/sign")
def sign(agreement_id: str, body: Signature):
    """Simulated LS Sign / DocuSign / Adobe Sign execution."""
    agreement = _get(agreement_id)
    if agreement["status"] != "ready_for_signature":
        raise HTTPException(409, f"agreement is {agreement['status']}; all approvals must clear first")
    agreement.update(signers=body.signers, signature_provider=body.provider, executed_at=now_iso())
    _transition(agreement, "executed", ", ".join(body.signers), f"signed via {body.provider}")
    final = store.get("versions", f"{agreement_id}:{agreement['current_version']}")
    events.publish(
        "agreement.executed",
        {
            "agreement_id": agreement_id,
            "title": agreement["title"],
            "counterparty": agreement["counterparty"],
            "owner": agreement["owner"],
            "metadata": agreement["metadata"],
            "version": agreement["current_version"],
            "content": final["content"],
            "signers": body.signers,
            "provider": body.provider,
            "executed_at": agreement["executed_at"],
        },
    )
    return agreement


@app.get("/agreements/{agreement_id}/audit")
def audit_trail(agreement_id: str):
    _get(agreement_id)
    return store.list("audit", agreement_id=agreement_id)


# ---- event handlers (choreography) ------------------------------------------
@events.on("approval.workflow_completed")
async def on_approved(event: dict) -> None:
    agreement = _get(event["payload"]["agreement_id"])
    if agreement["status"] == "pending_approval":  # ignore stale workflows
        _transition(agreement, "ready_for_signature", "approval-service", "all approvals cleared")


@events.on("approval.rejected")
async def on_rejected(event: dict) -> None:
    p = event["payload"]
    agreement = _get(p["agreement_id"])
    if agreement["status"] == "pending_approval":
        _transition(agreement, "changes_requested", p["approver"], f"{p['group']} rejected: {p.get('comment', '')}")


@events.on("review.completed")
async def on_review(event: dict) -> None:
    p = event["payload"]
    _audit(p["agreement_id"], "review-service", "ai_review_completed", version=p["version"], issues=p["issue_count"], non_negotiable=p["non_negotiable_violations"])


events.mount(app)
