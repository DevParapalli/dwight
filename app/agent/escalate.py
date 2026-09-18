import json

from app.agent.decisions import lookup_decision
from app.agent.policy import (
    CLASS_SCOPED_REASON_CODES,
    LEARNABLE_REASON_CODES,
    REASON_CODE_SCOPE,
    PolicyDecision,
    signature,
)
from app.audit import record_change, utcnow
from app.db import new_id


def create_or_merge_escalation(conn, run_id: str, decision: PolicyDecision, entity_id: str | None) -> str | None:
    """Returns the escalation id, or None when nothing was asked.

    A signature a human has already answered in any earlier run is never asked
    again -- but only for reason codes in policy.LEARNABLE_REASON_CODES, whose
    signature identifies a genuinely reusable question. The stored decision is
    applied and only an audit row is written.

    Otherwise, class-scoped reason codes (see policy.CLASS_SCOPED_REASON_CODES)
    merge into one open escalation per signature, incrementing affected_count --
    the "escalate classes, not instances" principle. Record/pair-scoped codes
    always get their own row, even when their signature coincides with another's.

    entity_id is informational for class-scoped codes (an example instance, not
    updated on later merges) and authoritative for record/pair-scoped ones."""
    sig = signature(decision.reason_code, decision.scope_key)

    prior = (lookup_decision(conn, decision.reason_code, sig)
             if decision.reason_code in LEARNABLE_REASON_CODES else None)
    if prior is not None:
        record_change(
            conn, run_id=run_id, actor="agent", reason_code=decision.reason_code,
            entity_type="escalation", entity_id=entity_id, field=decision.scope_key,
            after=prior["resolution"].get("value"),
            note=f"applied prior decision ({prior['resolution'].get('action')}) "
                 f"from {prior['resolved_by']} -- not re-escalated",
        )
        return None

    if decision.reason_code in CLASS_SCOPED_REASON_CODES:
        existing = conn.execute(
            "SELECT id FROM escalations WHERE run_id = ? AND reason_code = ? AND signature = ? AND status = 'open'",
            (run_id, decision.reason_code, sig),
        ).fetchone()
        if existing:
            conn.execute("UPDATE escalations SET affected_count = affected_count + 1 WHERE id = ?", (existing["id"],))
            return existing["id"]

    escalation_id = new_id()
    conn.execute(
        """INSERT INTO escalations
           (id, run_id, reason_code, scope, signature, entity_id, question, evidence,
            affected_count, suggested_action, suggested_value, options, context, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'open', ?)""",
        (escalation_id, run_id, decision.reason_code, REASON_CODE_SCOPE[decision.reason_code], sig, entity_id,
         decision.question, decision.evidence, decision.suggested_action,
         decision.suggested_value, json.dumps(decision.options) if decision.options else None,
         json.dumps(decision.context) if decision.context else None,
         utcnow()),
    )
    return escalation_id
