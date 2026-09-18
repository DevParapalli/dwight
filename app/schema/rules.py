from dataclasses import dataclass, field

from dateutil.relativedelta import relativedelta

from app.schema.loader import CrossFieldRule


@dataclass
class RuleContext:
    """Cross-record data a rule may need. Populated by the caller during a real run;
    empty by default so single-record tests don't need it."""

    known_employee_ids: set[str] = field(default_factory=set)


def _r1(record: dict, ctx: RuleContext) -> bool:
    hire, term = record.get("hire_date"), record.get("termination_date")
    if hire is None or term is None:
        return True
    return term >= hire


def _r2(record: dict, ctx: RuleContext) -> bool:
    if record.get("employment_status") != "terminated":
        return True
    return record.get("termination_date") is not None


def _r3(record: dict, ctx: RuleContext) -> bool:
    if record.get("employment_status") == "terminated":
        return True
    return record.get("termination_date") is None


def _r4(record: dict, ctx: RuleContext) -> bool:
    dob, hire = record.get("date_of_birth"), record.get("hire_date")
    if dob is None or hire is None:
        return True
    return dob <= hire - relativedelta(years=16)


def _r5(record: dict, ctx: RuleContext) -> bool:
    mgr, emp = record.get("manager_employee_id"), record.get("employee_id")
    if mgr is None:
        return True
    return mgr != emp


def _r6(record: dict, ctx: RuleContext) -> bool:
    mgr = record.get("manager_employee_id")
    if mgr is None:
        return True
    return mgr in ctx.known_employee_ids


# One small function per rule id. Adding a rule means one YAML entry (for display
# on /policy and in audit rows) plus one entry here -- no expression language to maintain.
RULE_CHECKS = {
    "R1": _r1,
    "R2": _r2,
    "R3": _r3,
    "R4": _r4,
    "R5": _r5,
    "R6": _r6,
}


def evaluate_rules(
    record: dict, rules: list[CrossFieldRule], ctx: RuleContext | None = None
) -> list[str]:
    """Return the ids of the rules that fail for this record."""
    ctx = ctx or RuleContext()
    failed = []
    for rule in rules:
        check = RULE_CHECKS.get(rule.id)
        if check is None:
            raise ValueError(f"no check registered for rule {rule.id!r}")
        if not check(record, ctx):
            failed.append(rule.id)
    return failed
