import json

from app.agent.decisions import record_decision
from app.agent.escalate import create_or_merge_escalation
from app.agent.policy import load_policy
from app.agent.validate import rule_issues
from app.audit import record_change, utcnow
from app.schema.loader import load_schema
from app.settings import settings

VALID_ACTIONS = {"approve", "edit", "reject"}


def _apply_mapping(conn, escalation, action: str, value: str | None) -> None:
    """MAP_* escalations point at a mappings row, so the answer can be applied
    to the current run immediately rather than waiting for a re-run."""
    mapping_id = escalation["entity_id"]
    if not mapping_id:
        return
    if action == "reject":
        conn.execute(
            "UPDATE mappings SET target_field = NULL, status = 'accepted' WHERE id = ?", (mapping_id,)
        )
    elif value:
        # Approve and edit both apply whatever value the card carried. The
        # control is pre-filled with the proposal, so approving applies the
        # proposal and editing applies the change, without a separate path.
        conn.execute(
            "UPDATE mappings SET target_field = ?, status = 'accepted' WHERE id = ?", (value, mapping_id)
        )
    else:
        conn.execute("UPDATE mappings SET status = 'accepted' WHERE id = ?", (mapping_id,))


def _apply_push_repair(conn, escalation, action: str, value: str | None) -> None:
    """Writes an approved correction back onto the staged record.

    Nothing reaches the target here. The record is corrected in place and the
    next push picks it up, which is what makes the second push meaningful: only
    records whose payload actually changed are sent again.

    Rejecting leaves the record exactly as it was and marks it excluded, so it is
    skipped by the push rather than silently retried and refused again.
    """
    record_id = escalation["entity_id"]
    if not record_id:
        return

    row = conn.execute("SELECT data, status FROM records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return

    context = json.loads(escalation["context"]) if escalation["context"] else {}
    field = context.get("repair_field")

    if action == "reject" or not value:
        conn.execute(
            "UPDATE records SET status = 'excluded', blocked_on = 'PUSH_REJECTED', updated_at = ? "
            "WHERE id = ?",
            (utcnow(), record_id),
        )
        record_change(
            conn, run_id=escalation["run_id"], actor="human", stage="pushed",
            reason_code="PUSH_REJECTED", entity_type="record", entity_id=record_id,
            before=row["status"], after="excluded",
            note="left uncorrected and excluded from the push; the target's rule still refuses it",
        )
        return

    if not field:
        return

    data = json.loads(row["data"])
    before = data.get(field)
    data[field] = value
    # The record keeps whatever status it had before it was refused -- a merge
    # survivor is still a merge survivor after one of its fields is corrected.
    restored = "clean" if row["status"] == "excluded" else row["status"]
    conn.execute(
        "UPDATE records SET data = ?, status = ?, blocked_on = NULL, updated_at = ? WHERE id = ?",
        (json.dumps(data, default=str), restored, utcnow(), record_id),
    )
    record_change(
        conn, run_id=escalation["run_id"], actor="human", stage="pushed",
        reason_code="PUSH_REJECTED", entity_type="record", entity_id=record_id,
        field=field, before=str(before or ""), after=value,
        note="corrected after the target refused it; will be sent again on the next push",
    )

    # One field corrected can contradict another that was already there -- a
    # termination date moved past a hire date, a manager set to the employee's
    # own id. The fill page checks for exactly this before it saves; this path
    # did not, so an answer given here could put the record back in front of the
    # target in a state the target refuses again, with nothing in between saying
    # why. Raised as a question rather than refused: the answer came from a
    # person who cannot see this screen's rejection list, and dropping their
    # correction on the floor is the worse failure.
    schema, policy = load_schema(settings.schema_path), load_policy()
    asked = {
        row["rule_id"]
        for row in conn.execute(
            """SELECT json_extract(context, '$.rule_id') AS rule_id FROM escalations
               WHERE entity_id = ? AND status = 'open' AND reason_code = 'LOGIC_CONTRADICTION'""",
            (record_id,),
        )
    }
    for issue in rule_issues(data, schema, policy):
        rule_id = (issue.decision.context or {}).get("rule_id")
        if rule_id in asked:
            continue
        create_or_merge_escalation(conn, escalation["run_id"], issue.decision, entity_id=record_id)
        record_change(
            conn, run_id=escalation["run_id"], actor="agent", stage="resolved",
            reason_code=issue.decision.reason_code, entity_type="record", entity_id=record_id,
            field=issue.field, rule_id=rule_id,
            note=f"escalated after a correction: {issue.decision.scope_key}",
        )


# Only reason codes whose answer can be applied to the already-processed run
# in place. Everything else is recorded as a decision and takes effect on the
# next pass, which is what PLAN.md's Learning section describes.
_APPLIERS = {
    "MAP_AMBIGUOUS": _apply_mapping,
    "MAP_UNMAPPED": _apply_mapping,
    "PUSH_REJECTED": _apply_push_repair,
}


def resolve_escalation(
    conn, run_id: str, escalation_id: str, action: str, value: str | None = None,
    resolved_by: str = "consultant", apply_to_all: bool = False,
) -> int:
    """Records the answer as a reusable decision, applies it where it can be
    applied in place, and closes the escalation. With apply_to_all, every other
    open escalation in this run sharing the signature is closed the same way --
    that is the "apply to all N similar" action. Returns how many were closed."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown resolution action: {action!r}")

    escalation = conn.execute("SELECT * FROM escalations WHERE id = ? AND run_id = ?",
                              (escalation_id, run_id)).fetchone()
    if escalation is None:
        raise ValueError(f"no open escalation {escalation_id!r} in run {run_id!r}")

    # Approving without touching the control means "do what you proposed", so
    # fall back to the stored suggestion rather than recording an empty answer.
    effective = value if value else (escalation["suggested_value"] if action != "reject" else None)
    resolution = {"action": action, "value": effective}
    record_decision(conn, escalation["reason_code"], escalation["signature"], resolution, resolved_by)

    if apply_to_all:
        targets = conn.execute(
            "SELECT * FROM escalations WHERE run_id = ? AND reason_code = ? AND signature = ? AND status = 'open'",
            (run_id, escalation["reason_code"], escalation["signature"]),
        ).fetchall()
    else:
        targets = [escalation]

    applier = _APPLIERS.get(escalation["reason_code"])
    closed = 0
    for target in targets:
        if applier:
            # Approving a class means "do what you proposed for each of these",
            # so each escalation contributes its own suggestion. Editing means
            # "use my value for all of them", so the typed value wins.
            #
            # The difference is not cosmetic. One refusal reason can cover many
            # different offending values -- 102 misspelled job titles are one
            # question and 102 different corrections -- and pushing the first
            # card's value onto all of them would quietly rewrite the other 101.
            per_target = effective
            if action == "approve" and not value:
                if not target["suggested_value"]:
                    # Nothing was proposed for this one, so there is nothing to
                    # approve. Falling through would hand it the value proposed
                    # for a different record -- the same mis-correction the
                    # per-record suggestion exists to prevent. It stays open.
                    continue
                per_target = target["suggested_value"]
            applier(conn, target, action, per_target)
        conn.execute(
            "UPDATE escalations SET status = 'resolved', resolved_at = ? WHERE id = ?",
            (utcnow(), target["id"]),
        )
        record_change(
            conn, run_id=run_id, actor="human", stage="resolved",
            reason_code=target["reason_code"], entity_type="escalation", entity_id=target["id"],
            field=target["signature"], before="open", after=action, note=(
                f"{resolved_by} resolved: {action}"
                + (f" -> {effective}" if effective else "")
                + (f" (applied to {len(targets)} similar)" if apply_to_all and len(targets) > 1 else "")
            ),
        )
        closed += 1

    # What was actually closed, not what was selected: anything left open for
    # want of its own proposal is still a question.
    return closed
