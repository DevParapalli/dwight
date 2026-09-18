import hashlib
import json
import re
import time
from datetime import UTC, datetime

from app.db import connect, new_id
from app.llm.client import LLMNotConfigured, active_model, complete_json_reported
from app.llm.prompts import build_push_repair_prompt
from app.progress import emit
from app.schema.loader import Schema
from app.settings import settings


def _cache_key(target_error: str, record: dict) -> str:
    payload = json.dumps(record, sort_keys=True, default=str)
    return hashlib.sha256(f"push_repair:{target_error}:{payload}".encode()).hexdigest()


def _cached(key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT response FROM llm_cache WHERE model = ? AND prompt_hash = ?",
            (active_model(), key),
        ).fetchone()
    return json.loads(row["response"]) if row else None


def _store(key: str, result: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO llm_cache (id, model, prompt_hash, response, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_id(), active_model(), key, json.dumps(result), datetime.now(UTC).isoformat()),
        )


def mentioned_fields(target_error: str, schema: Schema) -> set[str]:
    """Which target fields the refusal message actually names."""
    words = set(re.findall(r"[a-z_][a-z0-9_]*", target_error.lower()))
    return {name for name in schema.fields if name.lower() in words}


def _validate(proposal: dict, target_error: str, record: dict, schema: Schema) -> tuple[dict | None, str]:
    """The policy layer for a model-proposed repair. Returns (accepted, why_not).

    Nothing the model says reaches a record without passing every one of these.
    The model chooses nothing on its own: it suggests a field and a value, and
    this decides whether that suggestion is even allowed to be shown to a person.
    """
    field_name = proposal.get("field")
    if not field_name:
        return None, proposal.get("rationale") or "the model could not identify a field to change"

    field = schema.fields.get(field_name)
    if field is None:
        # A field that is not in the schema is the clearest sign the model has
        # gone off the rails, or that a refusal message has steered it.
        return None, f"proposed a field that is not in the target schema ({field_name})"

    mentioned = mentioned_fields(target_error, schema)
    if mentioned and field_name not in mentioned:
        # The target told us what it objected to. A proposal to change something
        # else is out of scope no matter how plausible it looks.
        return None, (f"proposed changing {field_name}, which the target's message "
                      f"does not mention ({', '.join(sorted(mentioned))})")

    value = proposal.get("proposed_value")
    if value in (None, ""):
        return None, f"proposed emptying {field_name} rather than correcting it"

    value = str(value)
    if field.values and value not in field.values:
        return None, f"proposed {value!r}, which is not an allowed value for {field_name}"

    if field.pattern and not re.fullmatch(field.pattern, value):
        return None, f"proposed {value!r}, which does not match the required format for {field_name}"

    if record.get(field_name) in (None, ""):
        # Correcting a wrong value and supplying an absent one are different
        # acts. The second is inventing an employee's data, and no confidence
        # score makes it acceptable -- so it stops here regardless of whether the
        # schema calls the field required. A date of birth is the clearest case:
        # nothing else in the record implies it, and a plausible guess is worse
        # than an empty field because it looks like knowledge.
        return None, (f"{field_name} is missing from the record and cannot be worked out "
                      "from the rest of it -- this needs a person who knows the answer")

    return {
        "field": field_name,
        "current_value": record.get(field_name),
        "proposed_value": value,
        "confidence": float(proposal.get("confidence") or 0.0),
        "rationale": str(proposal.get("rationale") or "")[:300],
    }, ""


def propose_repair(target_error: str, record: dict, schema: Schema,
                   run_id: str | None = None) -> tuple[dict | None, str]:
    """Asks the model for one corrected field value for a refused record.

    Returns (proposal, why_not). A proposal is a suggestion for a person to
    approve or edit -- it is never applied here, and a refused proposal is not an
    error, it just means the consultant gets the question with no suggestion
    attached, which is what used to happen for every rejection.
    """
    def _say(message: str, kind: str = "llm", **detail) -> None:
        if run_id:
            emit(run_id, kind, message, stage="pushing", model=active_model(),
                 what="repair proposal", **detail)

    def _report(kind: str, message: str) -> None:
        _say(message, kind=kind)

    if not target_error:
        return None, "the target gave no reason, so there is nothing to work from"

    key = _cache_key(target_error, record)
    hit = _cached(key)
    if hit is not None:
        proposal = hit.get("proposal")
        _say("Answered from cache: "
             + (f"set {proposal['field']} to {proposal['proposed_value']}" if proposal
                else "no safe fix"), cached=True)
        return proposal, hit.get("why_not", "")

    _say(f"Asking {active_model()} how to fix: {target_error[:90]}", cached=False)

    system_prompt, user_prompt = build_push_repair_prompt(target_error, record, schema)
    started = time.monotonic()
    try:
        # Same reasoning-model ceiling as the duplicate judge: 400 is under what
        # one spends thinking before it emits the correction.
        result, _, _served_by = complete_json_reported(
            system_prompt, user_prompt, max_tokens=settings.llm_max_output_tokens,
            report=_report)
    except LLMNotConfigured:
        _say("No model configured, so no correction was proposed")
        return None, "no model is configured, so no correction was proposed"
    elapsed = time.monotonic() - started

    proposal, why_not = _validate(result, target_error, record, schema)
    if proposal:
        _say(f"Proposes {proposal['field']} = {proposal['proposed_value']!r} "
             f"at {proposal['confidence']:.0%} confidence ({elapsed:.1f}s)",
             field=proposal["field"], value=proposal["proposed_value"],
             confidence=proposal["confidence"], seconds=round(elapsed, 2))
    else:
        # Saying why a suggestion was refused is the interesting half: it is
        # usually the policy layer stopping the model, not the model giving up.
        _say(f"No proposal accepted ({elapsed:.1f}s) -- {why_not}",
             refused=True, seconds=round(elapsed, 2))
    _store(key, {"proposal": proposal, "why_not": why_not})
    return proposal, why_not
