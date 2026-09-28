"""Smart Administration: automatically classify and file incoming contracts."""
import re

SIGNALS = {
    "Amendment": (r"\bamendment\b", r"hereby amended", r"\bamends\b", r"deleted and replaced"),
    "NDA": (r"non-disclosure", r"confidential information", r"\bNDA\b"),
    "MSA": (r"master services agreement", r"statements? of work"),
    "SOW": (r"statement of work", r"deliverables", r"milestones?"),
    "Customer Agreement": (r"\bcustomer\b", r"subscription", r"order form"),
    "Vendor Agreement": (r"\bsupplier\b", r"\bvendor\b", r"purchase order"),
    "Lease": (r"\blease\b", r"\bpremises\b", r"\blandlord\b", r"\btenant\b"),
    "Employment": (r"\bemployee\b", r"\bemployment\b", r"\bsalary\b"),
}
DEPARTMENT = {
    "NDA": "Legal",
    "MSA": "Procurement",
    "SOW": "Procurement",
    "Vendor Agreement": "Procurement",
    "Customer Agreement": "Sales",
    "Lease": "Finance",
    "Employment": "HR",
}


def categorize(text: str, title: str = "") -> dict:
    head = text[:400]  # the title and preamble name the document type: weighted 3x
    scores = {}
    for kind, patterns in SIGNALS.items():
        score = sum(len(re.findall(p, text, re.I)) for p in patterns)
        score += 3 * sum(bool(re.search(p, f"{title} {head}", re.I)) for p in patterns)
        if score:
            scores[kind] = score
    if not scores:
        return {"contract_type": "Other", "department": "Legal", "confidence": 0.0}
    kind = max(scores, key=scores.get)
    return {
        "contract_type": kind,
        "department": DEPARTMENT.get(kind),  # None for amendments: inherited from the parent
        "confidence": round(scores[kind] / sum(scores.values()), 2),
    }
