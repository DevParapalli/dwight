from app.agent.decisions import record_decision
from app.audit import record_change, utcnow

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


# Only reason codes whose answer can be applied to the already-processed run
# in place. Everything else is recorded as a decision and takes effect on the
# next pass, which is what PLAN.md's Learning section describes.
_APPLIERS = {
    "MAP_AMBIGUOUS": _apply_mapping,
    "MAP_UNMAPPED": _apply_mapping,
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
    for target in targets:
        if applier:
            applier(conn, target, action, effective)
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

    return len(targets)
