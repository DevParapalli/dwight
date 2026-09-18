import hashlib
import json
from datetime import UTC, datetime

from app.db import connect, new_id
from app.llm.client import LLMNotConfigured, complete_json
from app.llm.prompts import build_value_normalization_prompt
from app.settings import settings


# PLAN.md allows up to fifty values per call; the practical ceiling is whatever
# keeps a batch's expected output under the provider's per-minute token limit,
# so it comes from settings rather than a constant.
def _batch_size() -> int:
    """Per backend: the hosted tier's batch is capped by its per-minute token
    limit, the local one isn't metered and can take PLAN.md's full fifty."""
    if not settings.groq_env_key and settings.local_fallback_model:
        return settings.local_value_batch_size
    return settings.llm_value_batch_size


def _cache_key(field_name: str, raw_value: str) -> str:
    """Deliberately not the (model, whole-batch-prompt) hash used for mapping --
    per the caching invariant, value normalization is cached per distinct
    (field, raw_value), never per row and never per batch composition."""
    return hashlib.sha256(f"value_norm:{field_name}:{raw_value}".encode()).hexdigest()


def _get_cached(field_name: str, raw_value: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT response FROM llm_cache WHERE model = ? AND prompt_hash = ?",
            (settings.groq_model, _cache_key(field_name, raw_value)),
        ).fetchone()
    return json.loads(row["response"]) if row else None


def _store_cached(field_name: str, raw_value: str, result: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO llm_cache (id, model, prompt_hash, response, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_id(), settings.groq_model, _cache_key(field_name, raw_value),
             json.dumps(result), datetime.now(UTC).isoformat()),
        )


def normalize_values(field_name: str, allowed_values: list[str], raw_values: list[str]) -> dict[str, dict]:
    """Normalizes distinct raw values for one enum field against the allowed set.
    Cached per (field, raw_value) so the same question is never re-asked; batches
    whatever isn't cached into provider-sized batches per LLM call."""
    results: dict[str, dict] = {}
    unresolved = []
    for v in raw_values:
        cached = _get_cached(field_name, v)
        if cached is not None:
            results[v] = cached
        else:
            unresolved.append(v)

    batch_size = _batch_size()
    for i in range(0, len(unresolved), batch_size):
        batch = unresolved[i:i + batch_size]
        try:
            system_prompt, user_prompt = build_value_normalization_prompt(field_name, allowed_values, batch)
            response, _latency_ms = complete_json(system_prompt, user_prompt)
        except LLMNotConfigured:
            # Not cached: this is an absence of capability, not a judgment. Once
            # an LLM is configured these values must be tried again, not treated
            # as permanently unresolved.
            for v in batch:
                results[v] = {"value": None, "confidence": 0.0, "rationale": "no LLM configured"}
            continue

        by_raw = {r["raw"]: r for r in response["results"]}
        for v in batch:
            r = by_raw.get(v, {"value": None, "confidence": 0.0, "rationale": "no result returned for this value"})
            result = {
                "value": r.get("value"),
                "confidence": float(r.get("confidence", 0.0)),
                "rationale": r.get("rationale", ""),
            }
            results[v] = result
            _store_cached(field_name, v, result)

    return results
