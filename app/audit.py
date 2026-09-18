import datetime as dt
import sqlite3

from app.db import new_id


def utcnow() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def record_change(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    actor: str,
    entity_type: str,
    entity_id: str,
    stage: str | None = None,
    reason_code: str | None = None,
    field: str | None = None,
    before=None,
    after=None,
    confidence: float | None = None,
    rule_id: str | None = None,
    model: str | None = None,
    prompt_hash: str | None = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    note: str | None = None,
) -> None:
    """The one path any mutation takes to leave a trace. Takes the caller's
    connection rather than opening its own, so the audit row commits in the same
    transaction as the mutation it documents -- one can't succeed without the other."""
    conn.execute(
        """INSERT INTO audit_events
           (id, run_id, ts, actor, stage, reason_code, entity_type, entity_id, field,
            before, after, confidence, rule_id, model, prompt_hash, latency_ms, cost_usd, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            new_id(), run_id, utcnow(), actor, stage, reason_code, entity_type, entity_id, field,
            str(before) if before is not None else None,
            str(after) if after is not None else None,
            confidence, rule_id, model, prompt_hash, latency_ms, cost_usd, note,
        ),
    )
