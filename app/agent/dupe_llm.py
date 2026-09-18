import hashlib
import json
import time
from datetime import UTC, datetime

from app.db import connect, new_id
from app.llm.client import LLMNotConfigured, active_model, complete_json_reported
from app.llm.prompts import build_duplicate_judgement_prompt
from app.progress import emit

# Fields that say something about identity. The rest (department, salary, job
# title) change over time for one person and would only add noise.
_IDENTITY_FIELDS = (
    "employee_id", "first_name", "last_name", "work_email", "phone",
    "date_of_birth", "date_of_joining", "termination_date",
)


def _comparable(data: dict) -> dict:
    return {k: data.get(k) for k in _IDENTITY_FIELDS if data.get(k) not in (None, "")}


def _norm_label(left: dict, right: dict) -> str:
    """Something a reader can recognise the pair by, in one short phrase."""
    def one(d):
        return (d.get("work_email") or d.get("employee_id")
                or f"{d.get('first_name', '')} {d.get('last_name', '')}".strip() or "a record")
    return f"{one(left)} and {one(right)}"


def _cache_key(left: dict, right: dict) -> str:
    """Keyed on the compared content, not on record ids: ids are regenerated on
    every import, so an id-keyed entry could never be hit again on a re-run."""
    payload = json.dumps(sorted([_comparable(left), _comparable(right)], key=str), sort_keys=True)
    return hashlib.sha256(f"dupe_judge:{payload}".encode()).hexdigest()


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


def judge_pair(left: dict, right: dict, run_id: str | None = None) -> dict | None:
    """Asks the model whether two records are different people. Returns None when
    no model is configured or the answer is unusable, which escalates as before.

    The model can only argue for separating the pair. It is never asked to
    confirm a merge, because a merge of two real employees cannot be undone and
    a model's agreement is not a good enough reason to take an action nobody can
    reverse. The worst a wrong answer here can do is leave two records unmerged
    and unasked about.
    """
    def _say(message: str, kind: str = "llm", **detail) -> None:
        if run_id:
            emit(run_id, kind, message, stage="reconciled", model=active_model(),
                 what="duplicate judgement", **detail)

    def _report(kind: str, message: str) -> None:
        _say(message, kind=kind)

    label = _norm_label(left, right)

    key = _cache_key(left, right)
    hit = _cached(key)
    if hit is not None:
        _say(f"{label}: {hit['verdict']} (already answered, from cache)",
             cached=True, verdict=hit["verdict"])
        return hit

    _say(f"Asking {active_model()} whether {label} are the same person", cached=False)

    system_prompt, user_prompt = build_duplicate_judgement_prompt(_comparable(left), _comparable(right))
    started = time.monotonic()
    try:
        result, _, _served_by = complete_json_reported(
            system_prompt, user_prompt, max_tokens=300, report=_report)
    except LLMNotConfigured:
        _say("No model configured, so this pair goes to a person")
        return None
    elapsed = time.monotonic() - started

    verdict = result.get("verdict")
    if verdict not in ("different", "unsure"):
        # Anything else -- including "same" -- is treated as no answer, so a
        # model that ignores the instructions cannot widen what it is trusted
        # with. The pair goes to a person.
        _say(f"Model answered {verdict!r}, which it is not allowed to conclude -- "
             f"sending the pair to a person ({elapsed:.1f}s)", verdict=verdict)
        return None

    judgement = {
        "verdict": verdict,
        "confidence": float(result.get("confidence") or 0.0),
        "rationale": str(result.get("rationale") or "")[:300],
    }
    _say(f"{label}: {judgement['verdict']} at {judgement['confidence']:.0%} confidence "
         f"({elapsed:.1f}s) -- {judgement['rationale']}",
         verdict=judgement["verdict"], confidence=judgement["confidence"],
         seconds=round(elapsed, 2))
    _store(key, judgement)
    return judgement
