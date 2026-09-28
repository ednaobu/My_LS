"""Approval service - Finalize "Workflow Automation & Governance".

Bounded context: approval matrices and the approval workflows built from them.

* Conditional approval matrices: rules such as "deal_value > 100k -> Finance".
* Sequential and parallel sequences: rules carry a step number; tasks in the
  same step run in parallel, steps run in order. A "parallel" matrix flattens
  everything into one step.

Consumes: agreement.submitted_for_approval -> builds a workflow
Publishes: approval.requested, approval.workflow_completed, approval.rejected
The agreement service reacts to the last two - neither service calls the other.
"""
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel

from lscommon.events import Events
from lscommon.rules import describe, evaluate
from lscommon.service import create_service
from lscommon.store import Store, db_path, new_id, now_iso

store = Store(db_path("approval"))
events = Events("approval", store)


@asynccontextmanager
async def lifespan(_app):
    seed_matrix()
    events.start()
    yield
    await events.stop()


app = create_service("approval", lifespan=lifespan)


class MatrixRule(BaseModel):
    name: str
    approver_group: str
    condition: dict | None = None
    step: int = 1


class Matrix(BaseModel):
    name: str
    mode: Literal["sequential", "parallel"] = "sequential"
    rules: list[MatrixRule]


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    approver: str
    comment: str = ""


# ---- matrices ----------------------------------------------------------------
@app.post("/matrices", status_code=201)
def create_matrix(matrix: Matrix):
    return store.put("matrices", {"id": new_id("mtx"), **matrix.model_dump()})


@app.get("/matrices")
def list_matrices():
    return store.list("matrices")


@app.put("/matrices/{matrix_id}")
def replace_matrix(matrix_id: str, matrix: Matrix):
    if not store.get("matrices", matrix_id):
        raise HTTPException(404, "matrix not found")
    return store.put("matrices", {"id": matrix_id, **matrix.model_dump()})


def seed_matrix() -> None:
    if store.get("matrices", "default"):
        return
    store.put(
        "matrices",
        {
            "id": "default",
            **Matrix(
                name="Default commercial approvals",
                mode="sequential",
                rules=[
                    MatrixRule(name="Legal sign-off", approver_group="legal", step=1),
                    MatrixRule(name="Security review for personal data", approver_group="security", step=1,
                               condition={"field": "processes_personal_data", "op": "truthy"}),
                    MatrixRule(name="Finance review over $100k", approver_group="finance", step=2,
                               condition={"field": "deal_value", "op": "gt", "value": 100_000}),
                    MatrixRule(name="CFO over $1M", approver_group="cfo", step=3,
                               condition={"field": "deal_value", "op": "gt", "value": 1_000_000}),
                ],
            ).model_dump(),
        },
    )


# ---- workflows ---------------------------------------------------------------
@events.on("agreement.submitted_for_approval")
async def on_submitted(event: dict) -> None:
    p = event["payload"]
    facts = p["metadata"]
    matrix = store.get("matrices", facts.get("approval_matrix", "default")) or store.get("matrices", "default")

    for old in store.list("workflows", agreement_id=p["agreement_id"], status="in_progress"):
        old["status"] = "superseded"  # a resubmission replaces the previous workflow
        store.put("workflows", old)

    tasks = [
        {
            "id": new_id("tsk"),
            "rule": rule["name"],
            "condition": describe(rule["condition"]),
            "group": rule["approver_group"],
            "step": 1 if matrix["mode"] == "parallel" else rule["step"],
            "status": "waiting",
        }
        for rule in matrix["rules"]
        if evaluate(rule["condition"], facts)
    ]
    workflow = {
        "id": new_id("wf"),
        "agreement_id": p["agreement_id"],
        "title": p["title"],
        "owner": p["owner"],
        "version": p["version"],
        "matrix_id": matrix["id"],
        "mode": matrix["mode"],
        "status": "in_progress",
        "tasks": tasks,
        "created_at": now_iso(),
    }
    _advance(workflow)


def _advance(workflow: dict) -> None:
    """Activate the next step, or finish the workflow."""
    remaining = [t for t in workflow["tasks"] if t["status"] != "approved"]
    if not remaining:
        workflow.update(status="completed", completed_at=now_iso())
        store.put("workflows", workflow)
        events.publish(
            "approval.workflow_completed",
            {"agreement_id": workflow["agreement_id"], "workflow_id": workflow["id"], "owner": workflow["owner"], "title": workflow["title"]},
        )
        return
    step = min(t["step"] for t in remaining)
    for task in remaining:
        if task["step"] == step and task["status"] == "waiting":
            task["status"] = "pending"
            events.publish(
                "approval.requested",
                {"agreement_id": workflow["agreement_id"], "workflow_id": workflow["id"], "task_id": task["id"],
                 "group": task["group"], "rule": task["rule"], "title": workflow["title"]},
            )
    store.put("workflows", workflow)


@app.get("/workflows")
def list_workflows(agreement_id: str | None = None, status: str | None = None):
    return store.list("workflows", agreement_id=agreement_id, status=status)


@app.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str):
    workflow = store.get("workflows", workflow_id)
    if not workflow:
        raise HTTPException(404, "workflow not found")
    return workflow


@app.get("/tasks")
def inbox(group: str | None = None, status: str = "pending"):
    """Approval inbox for a group, across all workflows."""
    return [
        {**t, "workflow_id": w["id"], "agreement_id": w["agreement_id"], "title": w["title"]}
        for w in store.list("workflows", status="in_progress")
        for t in w["tasks"]
        if t["status"] == status and (group is None or t["group"] == group)
    ]


@app.post("/workflows/{workflow_id}/tasks/{task_id}/decision")
def decide(workflow_id: str, task_id: str, body: Decision):
    workflow = get_workflow(workflow_id)
    if workflow["status"] != "in_progress":
        raise HTTPException(409, f"workflow is {workflow['status']}")
    task = next((t for t in workflow["tasks"] if t["id"] == task_id), None)
    if not task:
        raise HTTPException(404, "task not found")
    if task["status"] != "pending":
        raise HTTPException(409, f"task is {task['status']} (sequential step not reached yet?)")
    task.update(status="approved" if body.decision == "approve" else "rejected",
                approver=body.approver, comment=body.comment, decided_at=now_iso())
    if body.decision == "reject":
        workflow.update(status="rejected", completed_at=now_iso())
        store.put("workflows", workflow)
        events.publish(
            "approval.rejected",
            {"agreement_id": workflow["agreement_id"], "workflow_id": workflow_id, "group": task["group"],
             "approver": body.approver, "comment": body.comment, "owner": workflow["owner"], "title": workflow["title"]},
        )
    else:
        _advance(workflow)
    return workflow


events.mount(app)
