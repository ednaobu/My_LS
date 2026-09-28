"""Collaboration service - Finalize "Centralized Communication".

Bounded context: discussion threads tied to an agreement record, and the
notifications (Slack-style) that tell people when they need to act.

It is a pure *consumer* of other services' events: it never calls them, so
if it is down nothing else breaks - notifications just arrive late (the bus
retries delivery).

Consumes: intake.routed_to_legal, approval.*, review.completed, agreement.executed
"""
import os
import re
from contextlib import asynccontextmanager

from fastapi import HTTPException
from pydantic import BaseModel

from lscommon.events import Events
from lscommon.service import create_service, get_logger, http_client
from lscommon.store import Store, db_path, new_id, now_iso

store = Store(db_path("collaboration"))
events = Events("collaboration", store)
log = get_logger("collaboration")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")


@asynccontextmanager
async def lifespan(_app):
    events.start()
    yield
    await events.stop()


app = create_service("collaboration", lifespan=lifespan)


class Comment(BaseModel):
    author: str
    body: str
    parent_id: str | None = None
    internal: bool = True  # internal notes are never shared with the counterparty


# ---- comments ----------------------------------------------------------------
@app.post("/agreements/{agreement_id}/comments", status_code=201)
async def add_comment(agreement_id: str, comment: Comment):
    if comment.parent_id and not store.get("comments", comment.parent_id):
        raise HTTPException(404, "parent comment not found")
    doc = {"id": new_id("cmt"), "agreement_id": agreement_id, **comment.model_dump(),
           "mentions": re.findall(r"@([\w.-]+)", comment.body), "at": now_iso()}
    store.put("comments", doc)
    for person in doc["mentions"]:
        await notify(person, f"{comment.author} mentioned you: {comment.body}", agreement_id)
    return doc


@app.get("/agreements/{agreement_id}/comments")
def thread(agreement_id: str):
    comments = store.list("comments", agreement_id=agreement_id)
    replies = {}
    for c in comments:
        replies.setdefault(c["parent_id"], []).append(c)

    def nest(c):
        return {**c, "replies": [nest(r) for r in replies.get(c["id"], [])]}

    return [nest(c) for c in replies.get(None, [])]


# ---- notifications -----------------------------------------------------------
async def notify(recipient: str, text: str, agreement_id: str | None = None) -> dict:
    doc = {"id": new_id("ntf"), "recipient": recipient, "text": text, "agreement_id": agreement_id,
           "channel": "slack" if SLACK_WEBHOOK_URL else "slack (simulated)", "at": now_iso()}
    store.put("notifications", doc)
    if SLACK_WEBHOOK_URL:
        await http_client().post(SLACK_WEBHOOK_URL, json={"text": f"<@{recipient}> {text}"}, timeout=3.0)
    log.info("notify %s: %s", recipient, text)
    return doc


@app.get("/notifications")
def list_notifications(recipient: str | None = None, agreement_id: str | None = None):
    return store.list("notifications", recipient=recipient, agreement_id=agreement_id)


@events.on("intake.routed_to_legal")
async def on_funneled(event: dict) -> None:
    p = event["payload"]
    await notify("legal", f"New request '{p['title']}' from {p['requester']} needs Legal: {'; '.join(p['legal_review_reasons'])}")


@events.on("approval.requested")
async def on_approval_requested(event: dict) -> None:
    p = event["payload"]
    await notify(p["group"], f"Approval needed ({p['rule']}) for '{p['title']}'", p["agreement_id"])


@events.on("approval.workflow_completed")
async def on_approved(event: dict) -> None:
    p = event["payload"]
    await notify(p["owner"], f"'{p['title']}' is fully approved and Ready for Signature", p["agreement_id"])


@events.on("approval.rejected")
async def on_rejected(event: dict) -> None:
    p = event["payload"]
    await notify(p["owner"], f"{p['group']} rejected '{p['title']}': {p['comment']}", p["agreement_id"])


@events.on("review.completed")
async def on_review(event: dict) -> None:
    p = event["payload"]
    if p["issue_count"]:
        await notify(p["owner"], f"AI review of redline v{p['version']}: {p['issue_count']} playbook issue(s), "
                                 f"{p['non_negotiable_violations']} non-negotiable", p["agreement_id"])


@events.on("agreement.executed")
async def on_executed(event: dict) -> None:
    p = event["payload"]
    await notify(p["owner"], f"'{p['title']}' was executed via {p['provider']}", p["agreement_id"])


events.mount(app)
