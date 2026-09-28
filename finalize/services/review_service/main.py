"""Review service - Finalize "AI-Assisted Review & Playbooks".

Bounded context: playbooks and the AI features that apply them.

* AI abstract & summarization: key data points plus an extractive summary.
* Natural-language querying: ask a question, get the best-matching clauses.
* Rule-based playbooks: non-negotiables and preferred language; violations
  come back as "Smart Edits" in track-changes form ([-deleted-]{+inserted+}).

The "AI" here is deterministic heuristics so the demo runs offline. Because
it sits behind its own API, swapping in an LLM changes only this service -
one of the practical benefits of the service boundary.

Talks to: agreement service (sync HTTP, to fetch agreement text).
Consumes: agreement.version_added (auto-reviews counterparty redlines).
Publishes: review.completed.
"""
import re
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, model_validator

from lscommon.events import Events
from lscommon.service import call, create_service
from lscommon.store import Store, db_path, new_id, now_iso

store = Store(db_path("review"))
events = Events("review", store)


@asynccontextmanager
async def lifespan(_app):
    seed_playbook()
    events.start()
    yield
    await events.stop()


app = create_service("review", lifespan=lifespan)


class PlaybookRule(BaseModel):
    id: str
    name: str
    kind: Literal["forbidden", "required"]
    pattern: str  # regex, case-insensitive
    severity: Literal["non_negotiable", "preferred"] = "preferred"
    suggestion: str = ""  # replacement (forbidden) or clause to insert (required)
    explanation: str = ""


class Playbook(BaseModel):
    name: str
    rules: list[PlaybookRule]


class Target(BaseModel):
    agreement_id: str | None = None
    version: int | None = None
    text: str | None = None

    @model_validator(mode="after")
    def one_source(self):
        if not (self.agreement_id or self.text):
            raise ValueError("provide agreement_id or text")
        return self


class ReviewRequest(Target):
    playbook_id: str = "default"


class Question(Target):
    question: str


# ---- playbooks -----------------------------------------------------------------
@app.post("/playbooks", status_code=201)
def create_playbook(playbook: Playbook):
    return store.put("playbooks", {"id": new_id("pbk"), **playbook.model_dump()})


@app.get("/playbooks")
def list_playbooks():
    return store.list("playbooks")


def seed_playbook() -> None:
    if store.get("playbooks", "default"):
        return
    rules = [
        PlaybookRule(id="no-unlimited-liability", name="No unlimited liability", kind="forbidden", severity="non_negotiable",
                     pattern=r"unlimited liability|liability shall be unlimited",
                     suggestion="aggregate liability shall not exceed the fees paid in the twelve (12) months preceding the claim",
                     explanation="Company never accepts uncapped liability."),
        PlaybookRule(id="no-perpetual-term", name="No perpetual obligations", kind="forbidden", severity="preferred",
                     pattern=r"\bin perpetuity\b|\bperpetual(ly)?\b",
                     suggestion="for a period of three (3) years", explanation="Obligations should be time-bound."),
        PlaybookRule(id="payment-net-30", name="Payment within 30 days", kind="forbidden", severity="preferred",
                     pattern=r"net\s*(?:[4-9]\d|\d{3,})\b", suggestion="Net 30",
                     explanation="Standard payment terms are Net 30."),
        PlaybookRule(id="governing-law", name="Governing law clause present", kind="required", severity="non_negotiable",
                     pattern=r"governed by the laws of",
                     suggestion="This Agreement shall be governed by the laws of the State of Delaware.",
                     explanation="Every agreement must state its governing law."),
        PlaybookRule(id="liability-cap", name="Limitation of liability present", kind="required", severity="non_negotiable",
                     pattern=r"liability shall not exceed|limitation of liability",
                     suggestion="Each party's aggregate liability shall not exceed the fees paid in the twelve (12) months preceding the claim.",
                     explanation="A liability cap is mandatory."),
    ]
    store.put("playbooks", {"id": "default", **Playbook(name="Standard commercial playbook", rules=rules).model_dump()})


# ---- helpers ---------------------------------------------------------------------
async def _load(target: Target) -> tuple[str, dict]:
    if target.text:
        return target.text, {}
    path = f"/agreements/{target.agreement_id}"
    if target.version:
        doc = await call("agreement", "GET", f"{path}/versions/{target.version}")
        return doc["content"], {"agreement_id": target.agreement_id, "version": target.version}
    doc = await call("agreement", "GET", path)
    return doc["content"], {"agreement_id": target.agreement_id, "version": doc["current_version"], "owner": doc["owner"]}


def run_playbook(text: str, playbook: dict) -> dict:
    issues, redlined = [], text
    for rule in playbook["rules"]:
        matches = list(re.finditer(rule["pattern"], text, re.IGNORECASE))
        if rule["kind"] == "forbidden":
            for m in matches:
                issues.append({"rule": rule["id"], "name": rule["name"], "severity": rule["severity"],
                               "explanation": rule["explanation"],
                               "smart_edit": {"action": "replace", "original": m.group(0), "suggestion": rule["suggestion"]}})
            redlined = re.sub(rule["pattern"], lambda m: f"[-{m.group(0)}-]{{+{rule['suggestion']}+}}", redlined, flags=re.IGNORECASE)
        elif not matches:
            issues.append({"rule": rule["id"], "name": rule["name"], "severity": rule["severity"],
                           "explanation": rule["explanation"],
                           "smart_edit": {"action": "insert", "suggestion": rule["suggestion"]}})
            redlined += f"\n\n{{+{rule['suggestion']}+}}"
    return {
        "issues": issues,
        "non_negotiable_violations": sum(i["severity"] == "non_negotiable" for i in issues),
        "redlined_text": redlined,
    }


DATE = r"(?:[A-Z][a-z]+ \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2})"
KEY_POINTS = {
    "parties": r"between (.+?) \(.*?\) and (.+?) \(",
    "effective_date": rf"effective (?:as of )?({DATE})",
    "term": r"term of ([\w\s()]+?(?:months|years))",
    "contract_value": r"(\$\d{1,3}(?:,\d{3})*(?:\.\d\d)?)",
    "payment_terms": r"\b(net\s*\d+)\b",
    "governing_law": r"governed by the laws of (?:the State of )?([A-Z][a-zA-Z ]+?)[.,]",
    "auto_renewal": r"(automatically renew[^.]*)",
}
IMPORTANT = ("term", "fee", "liability", "terminat", "renew", "govern", "confidential", "payment", "data")
STOPWORDS = set("a an the is are of to in on for and or what which who when does do this that with by be it how any".split())


def summarize(text: str) -> dict:
    points = {}
    for key, pattern in KEY_POINTS.items():
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            points[key] = " / ".join(g.strip() for g in m.groups()) if len(m.groups()) > 1 else m.group(1).strip()
    paragraphs = [re.sub(r"^\d+\.\s+[A-Z][\w ]*\.\s+", "", p.strip()) for p in re.split(r"\n\s*\n", text)]
    sentences = [s for p in paragraphs for s in re.split(r"(?<=[.!?])\s+", p) if len(s.split()) > 3]
    ranked = sorted(sentences, key=lambda s: -sum(k in s.lower() for k in IMPORTANT))[:3]
    return {"key_data_points": points, "summary": " ".join(s for s in sentences if s in ranked)}


def _tokens(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", text.lower()) if w not in STOPWORDS}


def answer(text: str, question: str) -> dict:
    clauses = [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
    wanted = _tokens(question)
    scored = sorted(((len(wanted & _tokens(c)) / (len(wanted) or 1), c) for c in clauses), reverse=True)
    hits = [{"clause": c, "score": round(s, 2)} for s, c in scored[:3] if s > 0]
    return {"question": question, "answer": hits[0]["clause"] if hits else "No matching clause found.", "citations": hits}


# ---- endpoints -------------------------------------------------------------------
@app.post("/review")
async def review(request: ReviewRequest):
    playbook = store.get("playbooks", request.playbook_id)
    if not playbook:
        raise HTTPException(404, "playbook not found")
    text, ref = await _load(request)
    result = {"id": new_id("rev"), **ref, "playbook_id": playbook["id"], "at": now_iso(), **run_playbook(text, playbook)}
    store.put("reviews", result)
    return result


@app.get("/reviews")
def list_reviews(agreement_id: str | None = None):
    return store.list("reviews", agreement_id=agreement_id)


@app.post("/summarize")
async def summarize_endpoint(target: Target):
    text, ref = await _load(target)
    return {**ref, **summarize(text)}


@app.post("/ask")
async def ask(q: Question):
    text, ref = await _load(q)
    return {**ref, **answer(text, q.question)}


@events.on("agreement.version_added")
async def on_version(event: dict) -> None:
    p = event["payload"]
    if p["source"] != "external":
        return
    result = await review(ReviewRequest(agreement_id=p["agreement_id"], version=p["version"]))
    events.publish(
        "review.completed",
        {"agreement_id": p["agreement_id"], "version": p["version"], "owner": p["owner"], "review_id": result["id"],
         "issue_count": len(result["issues"]), "non_negotiable_violations": result["non_negotiable_violations"]},
    )


events.mount(app)
