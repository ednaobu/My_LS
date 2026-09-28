"""Evaluation of declarative conditions, e.g. {"field": "deal_value", "op": "gt", "value": 100000}.

Used by the template service (conditional clauses, request funneling) and the
approval service (approval matrices). It is a pure utility, safe to share.
"""

OPS = {
    "eq": lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "truthy": lambda a, _: bool(a),
    "falsy": lambda a, _: not a,
}


def evaluate(condition: dict | None, facts: dict) -> bool:
    if not condition:
        return True
    if "all" in condition:
        return all(evaluate(c, facts) for c in condition["all"])
    if "any" in condition:
        return any(evaluate(c, facts) for c in condition["any"])
    value = facts.get(condition["field"])
    try:
        return bool(OPS[condition.get("op", "eq")](value, condition.get("value")))
    except TypeError:  # e.g. None > 100000
        return False


def describe(condition: dict | None) -> str:
    if not condition:
        return "always"
    if "all" in condition:
        return " and ".join(describe(c) for c in condition["all"])
    if "any" in condition:
        return " or ".join(describe(c) for c in condition["any"])
    return f"{condition['field']} {condition.get('op', 'eq')} {condition.get('value', '')}".strip()
