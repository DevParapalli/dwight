import hashlib
import json
import random
import time
import urllib.error
import urllib.request

from pydantic import ValidationError

from app.agent.escalate import create_or_merge_escalation
from app.agent.policy import decide_push, should_retry_push
from app.audit import record_change, utcnow
from app.db import connect, new_id
from app.progress import Ticker, emit
from app.schema.loader import Schema
from app.schema.pydantic_builder import build_model
from app.settings import settings


def _request(method: str, path: str, payload: dict | None, idempotency_key: str | None) -> tuple[int, dict]:
    url = f"{settings.target_api_url}{path}"
    data = json.dumps(payload, default=str).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            body = {}
        return e.code, body
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"target API at {settings.target_api_url} is unreachable: {e}. "
            "Start it with: uv run tools/mock_target_api.py"
        ) from e


def _payload_hash(data: dict) -> str:
    """Stable fingerprint of an employee's values, so a re-import can tell
    unchanged records from changed ones without diffing field by field."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _backoff_seconds(attempt: int, policy: dict) -> float:
    cfg = policy["push"]
    delay = (cfg["backoff_base_ms"] / 1000) * (2 ** attempt)
    if cfg.get("jitter"):
        delay *= 0.5 + random.random()
    return delay


def push_run(run_id: str, schema: Schema, policy: dict) -> dict:
    """Pushes every reconciled employee to the target, retrying transient
    failures per policy and escalating deterministic rejections. Returns a
    summary dict. Records that still fail full-schema validation after
    reconciliation are marked incomplete and never pushed -- this is the
    'staged' gate, and the only place the strict (all-required) model is used."""
    cfg = policy["push"]
    strict_model = build_model(schema, require_all=True)

    with connect() as conn:
        run = conn.execute("SELECT stage FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run and run["stage"] in ("pushing", "done"):
            return {"skipped": "already pushed"}
        conn.execute("UPDATE runs SET stage = 'pushing', updated_at = ? WHERE id = ?", (utcnow(), run_id))
        candidates = conn.execute(
            "SELECT id, natural_key, data FROM records "
            "WHERE run_id = ? AND status IN ('merged_survivor', 'clean')",
            (run_id,),
        ).fetchall()
        already_pushed = {
            r["employee_id"]: r["payload_hash"]
            for r in conn.execute("SELECT employee_id, payload_hash FROM pushed_state")
        }

    emit(run_id, "stage", f"Pushing {len(candidates):,} reconciled employee(s)",
         stage="pushing", total=len(candidates))
    ticker = Ticker(run_id, "pushing", "Pushed", total=len(candidates), every=250)

    pushed = rejected = incomplete = unchanged = changed = created = 0
    attempts_rows: list[tuple] = []
    rejections: list[tuple[str, str, int, int, str]] = []
    incomplete_ids: list[str] = []
    pushed_state_rows: list[tuple] = []

    for record in candidates:
        data = json.loads(record["data"])
        try:
            strict_model(**data)
        except ValidationError:
            incomplete += 1
            incomplete_ids.append(record["id"])
            continue

        employee_id = data.get("employee_id") or record["natural_key"]
        digest = _payload_hash(data)

        # Delta re-import: an employee whose values are byte-identical to what
        # was last pushed is skipped entirely -- no request, no attempt row.
        previous = already_pushed.get(employee_id)
        if previous == digest:
            unchanged += 1
            continue
        if previous is None:
            created += 1
        else:
            changed += 1

        payload = {**data, "run_id": run_id}
        idempotency_key = f"{run_id}:{employee_id}"

        status, body = 0, {}
        for attempt in range(cfg["max_retries"] + 1):
            status, body = _request("POST", "/employees", payload, idempotency_key)
            attempts_rows.append(
                (new_id(), run_id, record["id"], attempt + 1, status, idempotency_key,
                 "success" if status == 200 else "failure", utcnow())
            )
            if status == 200:
                break
            if should_retry_push(status, policy) and attempt < cfg["max_retries"]:
                time.sleep(_backoff_seconds(attempt, policy))
                continue
            break

        ticker.tick()
        if status == 200:
            pushed += 1
            pushed_state_rows.append((employee_id, digest, run_id, utcnow()))
        else:
            rejected += 1
            rejections.append((record["id"], employee_id, status, cfg["max_retries"] + 1,
                               str(body.get("error", ""))))

    with connect() as conn:
        conn.executemany(
            """INSERT INTO push_attempts
               (id, run_id, record_id, attempt_number, status_code, idempotency_key, outcome, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            attempts_rows,
        )
        conn.executemany(
            """INSERT INTO pushed_state (employee_id, payload_hash, run_id, pushed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (employee_id) DO UPDATE SET
                   payload_hash = excluded.payload_hash, run_id = excluded.run_id,
                   pushed_at = excluded.pushed_at""",
            pushed_state_rows,
        )
        for record_id in incomplete_ids:
            conn.execute(
                "UPDATE records SET status = 'incomplete', updated_at = ? WHERE id = ?",
                (utcnow(), record_id),
            )
        for record_id, employee_id, status, attempts, target_error in rejections:
            decision = decide_push(employee_id, status, attempts, policy, target_error)
            if decision.reason_code:
                create_or_merge_escalation(conn, run_id, decision, entity_id=record_id)
        record_change(
            conn, run_id=run_id, actor="agent", stage="pushed", entity_type="run", entity_id=run_id,
            after="done",
            note=(f"pushed {pushed} ({created} new, {changed} changed), "
                  f"skipped {unchanged} unchanged, rejected {rejected}, incomplete {incomplete}"),
        )
        conn.execute("UPDATE runs SET stage = 'done', updated_at = ? WHERE id = ?", (utcnow(), run_id))

    total = pushed + rejected
    failure_rate = (rejected / total) if total else 0.0
    emit(run_id, "finished",
         (f"Pushed {pushed:,} ({created:,} new, {changed:,} changed), "
          f"skipped {unchanged:,} unchanged, {rejected:,} refused"),
         stage="done", pushed=pushed, rejected=rejected, unchanged=unchanged,
         incomplete=incomplete, failure_rate=round(failure_rate, 4))
    return {
        "pushed": pushed,
        "created": created,
        "changed": changed,
        "unchanged_skipped": unchanged,
        "rejected": rejected,
        "incomplete": incomplete,
        "attempts": len(attempts_rows),
        "failure_rate": round(failure_rate, 4),
        "offer_rollback": failure_rate > cfg["offer_rollback_above_failure_rate"],
    }


def rollback_run(run_id: str) -> dict:
    """Deletes everything this run pushed. The target is a separate process with
    its own store, so the effect is externally observable, not just a flag."""
    with connect() as conn:
        pushed = conn.execute(
            """SELECT DISTINCT r.natural_key, r.data FROM push_attempts p
               JOIN records r ON r.id = p.record_id
               WHERE p.run_id = ? AND p.outcome = 'success'""",
            (run_id,),
        ).fetchall()

    removed = 0
    removed_ids: list[str] = []
    for row in pushed:
        employee_id = row["natural_key"] or json.loads(row["data"]).get("employee_id")
        if not employee_id:
            continue
        status, _ = _request("DELETE", f"/employees/{employee_id}", None, None)
        if status == 200:
            removed += 1
            removed_ids.append(employee_id)

    with connect() as conn:
        # Drop the delta fingerprints too: these employees are no longer in the
        # target, so a later run has to push them again rather than skip them
        # as unchanged.
        conn.executemany("DELETE FROM pushed_state WHERE employee_id = ?",
                         [(eid,) for eid in removed_ids])
        record_change(
            conn, run_id=run_id, actor="human", stage="rolled_back", entity_type="run",
            entity_id=run_id, after="rolled_back", note=f"rolled back {removed} pushed employee(s)",
        )
        conn.execute("UPDATE runs SET stage = 'rolled_back', updated_at = ? WHERE id = ?",
                     (utcnow(), run_id))
    emit(run_id, "finished", f"Rolled back {removed:,} employee(s) from the target",
         stage="rolled_back", removed=removed)
    return {"removed": removed}
