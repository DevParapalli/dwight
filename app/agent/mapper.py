from dataclasses import dataclass

from rapidfuzz import fuzz

from app.ingest.profile import ColumnProfile
from app.llm.client import LLMNotConfigured, active_model, cached_complete_json
from app.llm.prompts import build_mapping_prompt
from app.schema.loader import Schema


def _normalize(name: str) -> str:
    return name.lower().replace("_", " ").replace("-", " ").strip()


def deterministic_scores(column_name: str, schema: Schema) -> list[tuple[str, float]]:
    """Free, offline baseline: fuzzy name similarity against every target field.
    Used as the mapping proposal when no LLM is configured, and always available
    as a sanity check regardless."""
    norm_col = _normalize(column_name)
    scored = [
        (f.name, fuzz.token_sort_ratio(norm_col, _normalize(f.name)) / 100)
        for f in schema.fields.values()
    ]
    return sorted(scored, key=lambda x: x[1], reverse=True)


@dataclass
class MappingProposal:
    target_field: str | None
    confidence: float
    rationale: str
    alternatives: list[dict]
    source: str  # "llm" | "deterministic_fallback"
    model: str | None
    latency_ms: int
    cache_hit: bool


def propose_mapping(column: ColumnProfile, schema: Schema) -> MappingProposal:
    try:
        system_prompt, user_prompt = build_mapping_prompt(column, schema)
        result, latency_ms, cache_hit = cached_complete_json(system_prompt, user_prompt)
    except LLMNotConfigured:
        scores = deterministic_scores(column.name, schema)
        best_field, best_score = scores[0]
        return MappingProposal(
            target_field=best_field,
            confidence=round(best_score, 4),
            rationale="deterministic name-similarity fallback (no LLM configured)",
            alternatives=[{"field": f, "confidence": round(s, 4)} for f, s in scores[1:4]],
            source="deterministic_fallback",
            model=None,
            latency_ms=0,
            cache_hit=False,
        )

    candidates = result["candidates"]
    valid_fields = set(schema.fields)
    for c in candidates:
        if c["field"] is not None and c["field"] not in valid_fields:
            raise ValueError(f"LLM proposed a field not in the schema: {c['field']!r}")

    best = candidates[0]
    return MappingProposal(
        target_field=best["field"],
        confidence=float(best["confidence"]),
        rationale=best["rationale"],
        alternatives=candidates[1:],
        source="llm",
        model=active_model(),
        latency_ms=latency_ms,
        cache_hit=cache_hit,
    )
