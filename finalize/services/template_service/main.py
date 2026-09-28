"""Template service - Finalize "Standardized Intake & Generation".

Bounded context: templates, the conditional Q&A that drives drafting, and the
intake queue that funnels complex requests to Legal.

* Centralized repository of company templates.
* Dynamic Q&A drafting: answers fill placeholders and switch clauses on/off.
* Request funneling: rules decide whether a request can self-serve or must be
  routed to Legal (third-party paper always goes to Legal).

Talks to: agreement service (sync HTTP, to create the agreement record).
Publishes: intake.submitted, intake.routed_to_legal.
"""
import re
from contextlib import asynccontextmanager

from fastapi import HTTPException
from pydantic import BaseModel, Field

from lscommon.events import Events
from lscommon.rules import describe, evaluate
from lscommon.service import call, create_service
from lscommon.store import Store, db_path, new_id, now_iso

store = Store(db_path("template"))
events = Events("template", store)


@asynccontextmanager
async def lifespan(_app):
    seed_templates()
    events.start()
    yield
    await events.stop()


app = create_service("template", lifespan=lifespan)


class Question(BaseModel):
    id: str
    prompt: str
    type: str = "text"  # text | number | boolean | choice | date
    options: list[str] | None = None
    required: bool = True


class Clause(BaseModel):
    id: str
    title: str
    text: str  # may contain {{placeholders}}
    condition: dict | None = None  # clause is included only when this holds


class FunnelRule(BaseModel):
    reason: str
    condition: dict


class Template(BaseModel):
    name: str
    contract_type: str
    description: str = ""
    questions: list[Question]
    clauses: list[Clause]
    legal_review_rules: list[FunnelRule] = Field(default_factory=list)


class IntakeRequest(BaseModel):
    title: str
    counterparty: str
    requester: str
    department: str = "Sales"
    project: str = "General"
    template_id: str | None = None  # None -> third-party paper
    answers: dict = Field(default_factory=dict)
    third_party_document: str | None = None


class LegalAcceptance(BaseModel):
    legal_owner: str


# ---- templates --------------------------------------------------------------
@app.post("/templates", status_code=201)
def create_template(template: Template):
    return store.put("templates", {"id": new_id("tpl"), **template.model_dump(), "created_at": now_iso()})


@app.get("/templates")
def list_templates():
    return store.list("templates")


@app.get("/templates/{template_id}")
def get_template(template_id: str):
    template = store.get("templates", template_id)
    if not template:
        raise HTTPException(404, "template not found")
    return template


@app.post("/templates/{template_id}/preview")
def preview(template_id: str, answers: dict):
    template = get_template(template_id)
    return {"content": render(template, answers), "legal_review_reasons": funnel(template, answers)}


def validate_answers(template: dict, answers: dict) -> None:
    missing = [q["id"] for q in template["questions"] if q["required"] and answers.get(q["id"]) in (None, "")]
    if missing:
        raise HTTPException(422, f"missing answers: {', '.join(missing)}")
    for q in template["questions"]:
        value = answers.get(q["id"])
        if q["type"] == "choice" and value is not None and value not in q["options"]:
            raise HTTPException(422, f"{q['id']} must be one of {q['options']}")


def render(template: dict, answers: dict) -> str:
    def fill(text: str) -> str:
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: _fmt(answers.get(m.group(1), f"[{m.group(1)}]")), text)

    included = [c for c in template["clauses"] if evaluate(c.get("condition"), answers)]
    body = [fill(f"{template['name'].upper()}")]
    body += [f"{n}. {c['title']}. {fill(c['text'])}" for n, c in enumerate(included, start=1)]
    return "\n\n".join(body)


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


def funnel(template: dict | None, answers: dict) -> list[str]:
    if template is None:
        return ["Third-party paper must be reviewed by Legal"]
    return [
        f"{r['reason']} ({describe(r['condition'])})"
        for r in template.get("legal_review_rules", [])
        if evaluate(r["condition"], answers)
    ]


# ---- intake -----------------------------------------------------------------
@app.post("/intake", status_code=201)
async def submit_intake(request: IntakeRequest):
    template = get_template(request.template_id) if request.template_id else None
    if template:
        validate_answers(template, request.answers)
        content = render(template, {**request.answers, "counterparty": request.counterparty})
    elif request.third_party_document:
        content = request.third_party_document
    else:
        raise HTTPException(422, "provide template_id or third_party_document")

    intake = {
        "id": new_id("int"),
        **request.model_dump(exclude={"third_party_document"}),
        "contract_type": template["contract_type"] if template else "Third-Party Paper",
        "content": content,
        "legal_review_reasons": funnel(template, request.answers),
        "status": "submitted",
        "agreement_id": None,
        "created_at": now_iso(),
    }
    store.put("intake", intake)
    events.publish("intake.submitted", _summary(intake))

    if intake["legal_review_reasons"]:
        intake["status"] = "routed_to_legal"
        store.put("intake", intake)
        events.publish("intake.routed_to_legal", _summary(intake))
        return intake
    return await _create_agreement(intake, owner=request.requester)


@app.get("/intake")
def list_intake(status: str | None = None):
    return store.list("intake", status=status)


@app.get("/intake/{intake_id}")
def get_intake(intake_id: str):
    intake = store.get("intake", intake_id)
    if not intake:
        raise HTTPException(404, "intake request not found")
    return intake


@app.post("/intake/{intake_id}/accept")
async def legal_accepts(intake_id: str, body: LegalAcceptance):
    """Legal takes ownership of a funneled request and initiates the agreement."""
    intake = get_intake(intake_id)
    if intake["status"] != "routed_to_legal":
        raise HTTPException(409, f"intake is {intake['status']}, not routed_to_legal")
    intake["legal_owner"] = body.legal_owner
    return await _create_agreement(intake, owner=body.legal_owner)


async def _create_agreement(intake: dict, owner: str) -> dict:
    # Synchronous call: the requester wants the agreement id back right now.
    # If the agreement service is down, this request fails with a 503.
    agreement = await call(
        "agreement",
        "POST",
        "/agreements",
        json={
            "title": intake["title"],
            "counterparty": intake["counterparty"],
            "project": intake["project"],
            "owner": owner,
            "requester": intake["requester"],
            "template_id": intake["template_id"],
            "intake_id": intake["id"],
            "content": intake["content"],
            "metadata": {
                **intake["answers"],
                "department": intake["department"],
                "contract_type": intake["contract_type"],
            },
        },
    )
    intake.update(status="agreement_created", agreement_id=agreement["id"])
    store.put("intake", intake)
    return intake


def _summary(intake: dict) -> dict:
    keys = ("id", "title", "counterparty", "requester", "department", "contract_type", "legal_review_reasons")
    return {k: intake[k] for k in keys}


# ---- seed data --------------------------------------------------------------
def seed_templates() -> None:
    if store.list("templates"):
        return
    store.put("templates", {"id": "tpl_msa", **SEED_MSA, "created_at": now_iso()})
    store.put("templates", {"id": "tpl_nda", **SEED_NDA, "created_at": now_iso()})


SEED_MSA = Template(
    name="Master Services Agreement",
    contract_type="MSA",
    description="Company paper for purchasing or selling services.",
    questions=[
        Question(id="effective_date", prompt="Effective date?", type="date"),
        Question(id="term_months", prompt="Initial term (months)?", type="number"),
        Question(id="deal_value", prompt="Total contract value (USD)?", type="number"),
        Question(id="auto_renew", prompt="Auto-renew?", type="boolean"),
        Question(id="processes_personal_data", prompt="Will personal data be processed?", type="boolean"),
        Question(id="governing_law", prompt="Governing law", type="choice", options=["Delaware", "New York", "California"]),
        Question(id="non_standard_terms", prompt="Any non-standard terms requested?", type="boolean", required=False),
    ],
    clauses=[
        Clause(id="parties", title="Parties", text='This Master Services Agreement is entered into by and between Acme Corp ("Company") and {{counterparty}} ("Provider"), effective as of {{effective_date}}.'),
        Clause(id="term", title="Term", text="This Agreement has an initial term of {{term_months}} months."),
        Clause(id="renewal", title="Renewal", text="This Agreement shall automatically renew for successive twelve (12) month periods unless either party gives at least sixty (60) days prior written notice of non-renewal.", condition={"field": "auto_renew", "op": "truthy"}),
        Clause(id="fees", title="Fees", text="The total fees payable under this Agreement are ${{deal_value}}, payable Net 30."),
        Clause(id="dpa", title="Data Protection", text="Provider shall process personal data only in accordance with the Data Processing Addendum attached as Exhibit B.", condition={"field": "processes_personal_data", "op": "truthy"}),
        Clause(id="liability", title="Limitation of Liability", text="Each party's aggregate liability shall not exceed the fees paid in the twelve (12) months preceding the claim."),
        Clause(id="law", title="Governing Law", text="This Agreement shall be governed by the laws of the State of {{governing_law}}."),
    ],
    legal_review_rules=[
        FunnelRule(reason="Non-standard terms requested", condition={"field": "non_standard_terms", "op": "truthy"}),
        FunnelRule(reason="Strategic deal above $1M", condition={"field": "deal_value", "op": "gt", "value": 1_000_000}),
    ],
).model_dump()

SEED_NDA = Template(
    name="Mutual Non-Disclosure Agreement",
    contract_type="NDA",
    questions=[
        Question(id="effective_date", prompt="Effective date?", type="date"),
        Question(id="purpose", prompt="Purpose of the disclosure?"),
        Question(id="term_months", prompt="Confidentiality period (months)?", type="number"),
    ],
    clauses=[
        Clause(id="parties", title="Parties", text="This Mutual Non-Disclosure Agreement is entered into by and between Acme Corp and {{counterparty}}, effective as of {{effective_date}}."),
        Clause(id="purpose", title="Purpose", text="Confidential Information is disclosed solely for {{purpose}}."),
        Clause(id="term", title="Term", text="Obligations survive for a term of {{term_months}} months."),
    ],
).model_dump()


events.mount(app)
