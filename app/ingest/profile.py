import re
from dataclasses import dataclass, field

from app.ingest.readers import SourceTable

_NUMERIC_RE = re.compile(r"^[+-]?[\d,]*\.?\d+$")
_DATE_LIKE_RE = re.compile(r"^\d{1,4}[-/][A-Za-z0-9]{1,4}[-/]\d{1,4}$")
_TYPE_VOTE_SAMPLE = 200


@dataclass
class ColumnProfile:
    name: str
    inferred_type: str
    null_rate: float
    distinct_count: int
    distinct_capped: bool
    sample_values: list[str] = field(default_factory=list)


def _looks_numeric(v) -> bool:
    if isinstance(v, (int, float)):
        return True
    return bool(_NUMERIC_RE.match(str(v).replace(" ", "")))


def _infer_type(values: list) -> str:
    if not values:
        return "empty"
    numeric = sum(1 for v in values if _looks_numeric(v))
    date_like = sum(1 for v in values if _DATE_LIKE_RE.match(str(v)))
    if date_like / len(values) > 0.6:
        return "date_like"
    if numeric / len(values) > 0.6:
        return "numeric"
    return "text"


def profile_table(table: SourceTable, sample_size: int = 20, max_distinct: int = 10_000) -> list[ColumnProfile]:
    columns = table.columns
    total = 0
    null_counts = dict.fromkeys(columns, 0)
    distinct_sets: dict[str, set] = {c: set() for c in columns}
    capped = dict.fromkeys(columns, False)
    samples: dict[str, list] = {c: [] for c in columns}
    type_votes: dict[str, list] = {c: [] for c in columns}

    for row in table.rows():
        total += 1
        for c in columns:
            v = row.get(c)
            if v in (None, ""):
                null_counts[c] += 1
                continue
            if not capped[c]:
                distinct_sets[c].add(v)
                if len(distinct_sets[c]) > max_distinct:
                    capped[c] = True
            if len(samples[c]) < sample_size and v not in samples[c]:
                samples[c].append(v)
            if len(type_votes[c]) < _TYPE_VOTE_SAMPLE:
                type_votes[c].append(v)

    return [
        ColumnProfile(
            name=c,
            inferred_type=_infer_type(type_votes[c]),
            null_rate=round(null_counts[c] / total, 4) if total else 0.0,
            distinct_count=len(distinct_sets[c]),
            distinct_capped=capped[c],
            sample_values=[str(v) for v in samples[c]],
        )
        for c in columns
    ]
