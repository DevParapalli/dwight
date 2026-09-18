import hashlib
import json
import random
import time
import urllib.error
import urllib.request

from pydantic import ValidationError

from app.agent.escalate import create_or_merge_escalation
from app.agent.policy import decide_push, should_retry_push, signature
from app.agent.repair import mentioned_fields, propose_repair
from app.audit import record_change, utcnow
from app.db import connect, new_id
from app.progress import RunInterrupted, Ticker, emit
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


def _propose_repairs(run_id: str, schema, examples: dict[tuple[str, str], dict]) -> None:
    """Works out what to change for each distinct refusal, in two steps.

    The first step is deterministic and always runs: `mentioned_fields()` reads
    which field the target named out of its own message. That is what the UI
    needs to offer "fill these in yourself", so it must not depend on a model
    being reachable -- an earlier version set it only as a side effect of a
    successful model call, and a provider outage mid-push therefore turned every
    remaining question into a dead end with no proposal and no way to answer it.

    The second step asks a model for the value, and is allowed to fail. Each
    refusal is isolated: one failure costs that one proposal, not the rest of
    them. The proposal is only ever written to the escalation, never to the
    record.
    """
    emit(run_id, "progress",
         f"Looking for a fix for {len(examples)} refused value(s)",
         stage="pushing", what="repair", kinds=len(examples))
    # One model call per distinct refused value, so this is the slowest step in
    # the run and the one most likely to look like a hang. every=1 because each
    # tick is seconds, not milliseconds.
    ticker = Ticker(run_id, "pushing", "Working out corrections",
                    total=len(examples), every=1)

    failures = 0
    for (sig, target_error), payload in examples.items():
        ticker.tick()
        named = mentioned_fields(target_error, schema)
        field = next(iter(named)) if len(named) == 1 else None
        if field:
            with connect() as conn:
                conn.execute(
                    """UPDATE escalations
                       SET context = json_set(COALESCE(context, '{}'), '$.repair_field', ?)
                       WHERE run_id = ? AND signature = ? AND status = 'open'
                         AND json_extract(context, '$.target_error') = ?""",
                    (field, run_id, sig, target_error),
                )

        try:
            proposal, why_not = propose_repair(target_error, payload, schema, run_id=run_id)
        except RunInterrupted:
            # A shutdown is not a proposal failure and must not be absorbed by
            # the catch below, or Ctrl-C would be swallowed once per refusal.
            raise
        except Exception as exc:  # noqa: BLE001
            # Deliberately broad. A provider can fail in a dozen ways -- rate
            # limits, transport errors, malformed JSON, a changed SDK exception
            # -- and every one of them should cost this one proposal rather than
            # the rest of the queue. Naming them would couple this module to the
            # provider SDK and still miss one.
            #
            # Reported, not swallowed, and not fatal: the question still reaches
            # the consultant with the field it is about, which is the part that
            # matters. propose_repair has already emitted its own failure frame.
            failures += 1
            with connect() as conn:
                record_change(
                    conn, run_id=run_id, actor="llm", stage="pushed",
                    reason_code="PUSH_REJECTED", entity_type="escalation", entity_id=sig,
                    field=field,
                    note=f"no repair proposed, the model call failed: {type(exc).__name__}: {exc}"[:400],
                )
            continue

        with connect() as conn:
            if proposal:
                # json_set rather than a plain overwrite: every escalation row
                # carries its own refused payload in context, and only the field
                # to change is shared across the signature.
                conn.execute(
                    """UPDATE escalations
                       SET suggested_value = ?, suggested_action = ?, evidence = evidence || ?,
                           context = json_set(COALESCE(context, '{}'), '$.repair_field', ?)
                       WHERE run_id = ? AND signature = ? AND status = 'open'
                         AND json_extract(context, '$.target_error') = ?""",
                    (proposal["proposed_value"],
                     f"change {proposal['field']} to {proposal['proposed_value']}",
                     f". The AI looked at what the target refused and suggests: {proposal['rationale']}",
                     proposal["field"], run_id, sig, target_error),
                )
            else:
                conn.execute(
                    """UPDATE escalations SET evidence = evidence || ?
                       WHERE run_id = ? AND signature = ? AND status = 'open'
                         AND json_extract(context, '$.target_error') = ?""",
                    (f". The AI could not suggest a safe fix: {why_not}", run_id, sig, target_error),
                )
            record_change(
                conn, run_id=run_id, actor="llm", stage="pushed",
                reason_code="PUSH_REJECTED", entity_type="escalation", entity_id=sig,
                field=(proposal or {}).get("field") or field,
                before=str((proposal or {}).get("current_value") or ""),
                after=str((proposal or {}).get("proposed_value") or ""),
                confidence=(proposal or {}).get("confidence"),
                note=(f"proposed for review, not applied: {proposal['rationale']}" if proposal
                      else f"no repair proposed: {why_not}"),
            )

    if failures:
        emit(run_id, "llm_failed",
             f"{failures} of {len(examples)} refusals got no suggested fix because the model "
             "could not be reached. They are still in the queue with the field named.",
             stage="pushing", what="repair", failures=failures)


def push_run(run_id: str, schema: Schema, policy: dict) -> dict:
    """Pushes every reconciled employee to the target, retrying transient
    failures per policy and escalating deterministic rejections. Returns a
    summary dict. Records that still fail full-schema validation after
    reconciliation are marked incomplete and never pushed -- this is the
    'staged' gate, and the only place the strict (all-required) model is used."""
    try:
        return _push_run(run_id, schema, policy)
    except BaseException as exc:
        # 'pushing' is what stops a second push starting. If a push dies partway
        # the flag outlives it and the run can never be pushed again -- which
        # looks exactly like a button that does nothing.
        #
        # BaseException, not Exception, because Ctrl-C is the most likely way
        # this happens and KeyboardInterrupt is not an Exception. Nothing is
        # swallowed: the state is repaired and the interrupt is re-raised
        # immediately.
        with connect() as conn:
            conn.execute(
                "UPDATE runs SET stage = 'reconciled', updated_at = ? WHERE id = ? AND stage = 'pushing'",
                (utcnow(), run_id),
            )
        reason = ("the server was shut down" if isinstance(exc, RunInterrupted)
                  else f"of an unexpected error ({type(exc).__name__})")
        emit(run_id, "failed",
             f"The push stopped partway because {reason}. What was already sent is "
             "recorded and will be skipped next time. You can start it again.",
             stage="reconciled", error=type(exc).__name__)
        raise


def _push_run(run_id: str, schema: Schema, policy: dict) -> dict:
    cfg = policy["push"]
    strict_model = build_model(schema, require_all=True)

    with connect() as conn:
        run = conn.execute("SELECT stage FROM runs WHERE id = ?", (run_id,)).fetchone()
        # Only a push that is actually running blocks another one. A finished
        # run may be pushed again on purpose: that is how a corrected record
        # reaches the target after a rejection is resolved. It is safe because
        # pushed_state skips byte-identical records without a request and the
        # idempotency key is derived from the payload, so re-sending an
        # unchanged record cannot duplicate it.
        if run and run["stage"] == "pushing":
            return {"skipped": "a push is already running"}
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
        # What the target already refused, taken from the open questions
        # themselves: each one stores the payload it was raised for. No extra
        # bookkeeping -- the question *is* the record of the refusal.
        already_refused = {
            json.loads(r["context"])["payload"].get("employee_id"):
                _payload_hash(json.loads(r["context"])["payload"])
            for r in conn.execute(
                """SELECT context FROM escalations
                   WHERE run_id = ? AND reason_code = 'PUSH_REJECTED' AND status = 'open'
                     AND json_extract(context, '$.payload') IS NOT NULL""",
                (run_id,),
            )
        }

    emit(run_id, "stage", f"Pushing {len(candidates):,} reconciled employee(s)",
         stage="pushing", total=len(candidates))
    ticker = Ticker(run_id, "pushing", "Pushed", total=len(candidates), every=250)

    pushed = rejected = incomplete = unchanged = changed = created = retries = 0
    unchanged_refused = 0
    attempts_rows: list[tuple] = []
    rejections: list[tuple[str, str, int, int, str, dict]] = []
    incomplete_ids: list[str] = []
    pushed_state_rows: list[tuple] = []
    delivered_ids: list[str] = []
    examples: dict[tuple[str, str], dict] = {}

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

        # There is already an open question about this record, and nothing about
        # it has changed since. The target will say exactly the same thing, and
        # the only result would be a second copy of a question the consultant
        # already has -- which is what made a partly-answered class look like it
        # kept coming back. A corrected record hashes differently and goes.
        if already_refused.get(employee_id) == digest:
            unchanged_refused += 1
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
                delay = _backoff_seconds(attempt, policy)
                retries += 1
                # The first few are reported individually so the backoff is
                # visible as it happens, then in batches: a run with hundreds of
                # transient failures should not bury everything else in the feed.
                if retries <= 3 or retries % 50 == 0:
                    emit(run_id, "retry",
                         f"{employee_id} got HTTP {status} from the target, "
                         f"waiting {delay:.1f}s before try {attempt + 2} of "
                         f"{cfg['max_retries'] + 1}"
                         + (f" ({retries:,} transient failures retried so far)"
                            if retries > 3 else ""),
                         stage="pushing", employee_id=employee_id, status=status,
                         wait_seconds=round(delay, 2), attempt=attempt + 2,
                         max_attempts=cfg["max_retries"] + 1, retries_so_far=retries)
                time.sleep(delay)
                continue
            break

        ticker.tick()
        if status == 200:
            pushed += 1
            pushed_state_rows.append((employee_id, digest, run_id, utcnow()))
            delivered_ids.append(record["id"])
        else:
            rejected += 1
            # The payload travels with the rejection: a corrected value can only
            # be proposed later against what was actually sent.
            rejections.append((record["id"], employee_id, status, cfg["max_retries"] + 1,
                               str(body.get("error", "")), data))


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
        # "The target never answered for this one" stops being true the moment
        # it does. Left open, it would be a question with nothing to answer and
        # a count that never goes down.
        for record_id in delivered_ids:
            conn.execute(
                """UPDATE escalations SET status = 'resolved', resolved_at = ?
                   WHERE run_id = ? AND reason_code = 'PUSH_UNREACHABLE'
                     AND entity_id = ? AND status = 'open'""",
                (utcnow(), run_id, record_id),
            )

        for record_id in incomplete_ids:
            conn.execute(
                "UPDATE records SET status = 'incomplete', updated_at = ? WHERE id = ?",
                (utcnow(), record_id),
            )
        for record_id, employee_id, status, attempts, target_error, payload in rejections:
            decision = decide_push(employee_id, status, attempts, policy, target_error)
            if decision.reason_code:
                decision.context["employee_id"] = employee_id
                decision.context["payload"] = payload
                create_or_merge_escalation(conn, run_id, decision, entity_id=record_id)
                # Keyed on the target's *exact* message, not on the signature.
                # The signature deliberately strips the offending value so that
                # one reason is one question -- but the repair is not shared:
                # 102 different misspelled job titles are one question and 102
                # different corrections. Proposing per signature would have
                # rewritten every one of them to whichever title came first.
                examples.setdefault(
                    (signature("PUSH_REJECTED", decision.scope_key), target_error),
                    payload)
        record_change(
            conn, run_id=run_id, actor="agent", stage="pushed", entity_type="run", entity_id=run_id,
            after="done",
            note=(f"pushed {pushed} ({created} new, {changed} changed), "
                  f"skipped {unchanged} unchanged, rejected {rejected}, incomplete {incomplete}"),
        )
        conn.execute("UPDATE runs SET stage = 'done', updated_at = ? WHERE id = ?", (utcnow(), run_id))

    # Deliberately after the transaction above has closed. These are network
    # calls, and making them while holding SQLite's single write lock is how the
    # progress stream deadlocked before.
    if examples:
        _propose_repairs(run_id, schema, examples)

    total = pushed + rejected
    failure_rate = (rejected / total) if total else 0.0
    emit(run_id, "finished",
         (f"Pushed {pushed:,} ({created:,} new, {changed:,} changed), "
          f"skipped {unchanged:,} unchanged, {rejected:,} refused"
          + (f", {unchanged_refused:,} left alone because the target already "
             "refused them unchanged" if unchanged_refused else "")),
         stage="done", pushed=pushed, rejected=rejected, unchanged=unchanged,
         incomplete=incomplete, failure_rate=round(failure_rate, 4))
    return {
        "pushed": pushed,
        "created": created,
        "changed": changed,
        "unchanged_skipped": unchanged,
        "refused_before_skipped": unchanged_refused,
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

    emit(run_id, "stage", f"Rolling back {len(pushed):,} employee(s) from the target",
         stage="pushing", total=len(pushed))
    ticker = Ticker(run_id, "pushing", "Rolled back", total=len(pushed), every=100)

    removed = 0
    removed_ids: list[str] = []
    for row in pushed:
        ticker.tick()
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
