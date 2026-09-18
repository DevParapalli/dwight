import hashlib
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date

import yaml

from app.settings import settings

_cached_policy: dict | None = None
_cached_policy_path = None


def load_policy(path=None) -> dict:
    """Cached per path -- called once per column/value/record, not once per row."""
    global _cached_policy, _cached_policy_path
    path = path or settings.policy_path
    if _cached_policy is None or _cached_policy_path != path:
        _cached_policy = yaml.safe_load(path.read_text())
        _cached_policy_path = path
    return _cached_policy


def signature(reason_code: str, scope_key: str) -> str:
    return hashlib.sha256(f"{reason_code}:{scope_key}".encode()).hexdigest()


# PLAN.md's reason-code table's "Affects" column distinguishes two shapes:
# class-scoped codes (affect "all rows in the file/column" or "all rows sharing
# the raw value") get ONE escalation whose affected_count grows as more rows hit
# the same signature -- this is the "escalate classes, not instances" principle.
# Record-scoped codes (affect "one record") always get their own escalation row,
# even when their signature (e.g. field:rule_id) coincides with another record's
# -- that signature is for the future decisions-table lookup, not for merging.
REASON_CODE_SCOPE = {
    "MAP_AMBIGUOUS": "column",
    "MAP_UNMAPPED": "column",
    "VALUE_LOW_CONFIDENCE": "value",
    "DATE_FORMAT_AMBIGUOUS": "column",
    "VALIDATE_TWICE": "record",
    "LOGIC_CONTRADICTION": "record",
    "DUPE_AMBIGUOUS": "pair",
    "CONFLICT_ACROSS_SOURCES": "value",
    "PUSH_REJECTED": "record",
}
CLASS_SCOPED_REASON_CODES = {
    "MAP_AMBIGUOUS", "MAP_UNMAPPED", "VALUE_LOW_CONFIDENCE", "DATE_FORMAT_AMBIGUOUS",
    # Class-scoped since its signature became field-plus-sources rather than
    # field-plus-values: one question per disagreement shape, not per employee.
    "CONFLICT_ACROSS_SOURCES",
}

# Only these carry an answer that is safe to replay onto a later run without
# asking again, because their signature identifies a stable, reusable question:
# "what does this column map to", "what does this typo mean", "is this column
# day-first", "are these two people the same".
#
# CONFLICT_ACROSS_SOURCES joined this list when its signature changed from the
# specific values (which rarely recur) to the field and the set of sources that
# disagree (which recurs on every import). "For department, trust the HRIS" is a
# durable policy; "this employee is in Sales" was not.
#
# The rest are deliberately excluded. LOGIC_CONTRADICTION's signature is
# field:rule_id, so replaying one record's answer would silently apply it to
# every other record that breaks the same rule with entirely different values.
# VALIDATE_TWICE has an empty scope_key, so all of them share one signature. For
# those, a wrong replay would be exactly the silent, permanent auto-correction
# the escalation policy exists to prevent, so they get re-asked every run even
# after a human has answered a similar one.
LEARNABLE_REASON_CODES = {
    "MAP_AMBIGUOUS", "MAP_UNMAPPED", "VALUE_LOW_CONFIDENCE",
    "DATE_FORMAT_AMBIGUOUS", "DUPE_AMBIGUOUS", "CONFLICT_ACROSS_SOURCES",
}


@dataclass
class PolicyDecision:
    reason_code: str | None  # None means auto-accept -- no escalation
    scope_key: str = ""
    question: str = ""
    evidence: str = ""
    suggested_action: str = ""
    # What the agent would do if approved, and what else it could do instead.
    # Without these the queue can only offer a blind "approve": the consultant
    # has to be able to see the proposal before endorsing it, and to adjust it
    # rather than retype it.
    suggested_value: str | None = None
    options: list[str] = dc_field(default_factory=list)
    # Machine-readable detail a later step needs, kept apart from `evidence`,
    # which is prose for a person to read.
    context: dict = dc_field(default_factory=dict)


@dataclass
class Issue:
    """Pairs a PolicyDecision with which field it's about -- used by both
    app/agent/validate.py (per-record cleaning) and app/agent/reconcile.py
    (cross-record survivorship), so it lives next to PolicyDecision rather than
    in either caller."""
    decision: PolicyDecision
    field: str | None = None


def decide_mapping(
    column_name: str, filename: str, target_field: str | None, confidence: float,
    alternatives: list[dict], null_rate: float, policy: dict,
) -> PolicyDecision:
    """This is the only function that decides whether a proposed column mapping
    gets auto-accepted or flagged for review. MAP_UNMAPPED/MAP_AMBIGUOUS scope_key
    is file:column, matching PLAN.md's signature convention."""
    cfg = policy["mapping"]
    scope_key = f"{filename}:{column_name}"

    if confidence < cfg["unmapped_below"]:
        if null_rate >= 0.99:
            return PolicyDecision(None)  # nothing there to argue about
        return PolicyDecision(
            "MAP_UNMAPPED", scope_key,
            question=f"The column {column_name!r} in {filename} doesn't clearly match any target field.",
            evidence=f"best candidate confidence {confidence:.2f}, below the {cfg['unmapped_below']} threshold",
            suggested_action="map it manually, or drop the column",
            suggested_value=None,
        )

    top2_margin = confidence - (alternatives[0]["confidence"] if alternatives else 0.0)
    if confidence < cfg["auto_accept_min_confidence"] or top2_margin < cfg["ambiguous_top2_margin"]:
        runner_up = alternatives[0]["field"] if alternatives and alternatives[0].get("field") else None
        names = [n for n in (target_field, runner_up) if n]
        candidates_text = " or ".join(names) if names else "no clear target field"
        return PolicyDecision(
            "MAP_AMBIGUOUS", scope_key,
            question=f"The column {column_name!r} in {filename} could be {candidates_text}.",
            evidence=f"top candidate confidence {confidence:.2f}, next-best within {top2_margin:.2f}",
            suggested_action=(f"map it to {target_field}" if target_field else "map it manually"),
            suggested_value=target_field,
            options=[n for n in (target_field, runner_up) if n],
        )

    return PolicyDecision(None)


def decide_enum_coercion(field_name: str, raw_value: str, edit_distance: int, policy: dict,
                         nearest: str | None = None,
                         allowed_values: list[str] | None = None) -> PolicyDecision:
    cfg = policy["values"]
    if edit_distance <= cfg["enum_auto_max_edit_distance"]:
        return PolicyDecision(None)
    scope_key = f"{field_name}:{raw_value}"
    return PolicyDecision(
        "VALUE_LOW_CONFIDENCE", scope_key,
        question=(f"The value {raw_value!r} for {field_name} doesn't clearly match an allowed value."
                  + (f" The closest is {nearest!r}." if nearest else "")),
        evidence=f"nearest allowed value is {edit_distance} edits away",
        suggested_action=(f"read it as {nearest}" if nearest else "pick the correct value"),
        suggested_value=nearest,
        # The whole allowed set, so the card offers a list to choose from rather
        # than a free-text box and a hint about the nearest one.
        options=list(allowed_values or []),
    )


def decide_llm_normalization(field_name: str, raw_value: str, confidence: float, policy: dict,
                            proposed: str | None = None,
                            allowed_values: list[str] | None = None) -> PolicyDecision:
    cfg = policy["values"]
    if confidence >= cfg["auto_accept_min_confidence"]:
        return PolicyDecision(None)
    scope_key = f"{field_name}:{raw_value}"
    return PolicyDecision(
        "VALUE_LOW_CONFIDENCE", scope_key,
        question=(f"The value {raw_value!r} for {field_name} isn't confidently normalized."
                  + (f" The model read it as {proposed!r}." if proposed else "")),
        evidence=f"normalizer confidence {confidence:.2f}",
        suggested_action=(f"read it as {proposed}" if proposed else "set the correct value"),
        suggested_value=proposed,
        options=list(allowed_values or []),
    )


def _date_readings(raw_value: str) -> tuple[str, str] | None:
    """The two calendar dates a slash date could mean, as words."""
    match = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", str(raw_value or ""))
    if not match:
        return None
    a, b, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    try:
        return (date(year, b, a).strftime("%-d %B %Y"),
                date(year, a, b).strftime("%-d %B %Y"))
    except ValueError:
        return None


def decide_date(field_name: str, filename: str, ambiguous: bool, policy: dict,
                raw_value: str = "") -> PolicyDecision:
    if not ambiguous or not policy["dates"]["escalate_when_multiple_formats_parse_to_different_dates"]:
        return PolicyDecision(None)
    scope_key = f"{filename}:{field_name}"

    # Both readings, spelled out. "Two valid calendar dates are possible" is true
    # and useless: nobody can choose day-first over month-first without seeing
    # that 03/04/2024 is either the 3rd of April or the 4th of March.
    readings = _date_readings(raw_value)
    evidence = "two valid calendar dates are possible for the same value"
    if readings:
        day_first, month_first = readings
        evidence = (f"{raw_value} reads as {day_first} if the day comes first, "
                    f"or {month_first} if the month does")

    return PolicyDecision(
        "DATE_FORMAT_AMBIGUOUS", scope_key,
        question=f"Some dates in {field_name!r} ({filename}) could be read two different ways (day/month order).",
        evidence=evidence,
        suggested_action="read this column as day-first (DD/MM)",
        suggested_value="day_first",
        options=["day_first", "month_first"],
    )


RULE_DESCRIPTIONS = {
    "R1": "the termination date is before the hire date",
    "R2": "the record is marked terminated but has no termination date",
    "R3": "the record isn't marked terminated but has a termination date",
    "R4": "the employee would have been under 16 years old at hire",
    "R5": "the manager is set to the employee's own id",
    "R6": "the manager id doesn't match any known employee",
}


def decide_logic_contradiction(rule_id: str, field_name: str, policy: dict) -> PolicyDecision:
    scope_key = f"{field_name}:{rule_id}"
    description = RULE_DESCRIPTIONS.get(rule_id, f"rule {rule_id} failed")
    return PolicyDecision(
        "LOGIC_CONTRADICTION", scope_key,
        question=f"This record has a problem with no safe automatic fix: {description}.",
        evidence=f"cross-field rule {rule_id}: {description}",
        suggested_action="review and correct the record by hand",
        # The rule names the field it is about, which is what lets these be
        # corrected row by row. Each record breaks the rule with its own values,
        # so there is no single answer to give -- the same reason a refused
        # column of values cannot be fixed from one text box.
        context={"repair_field": field_name, "rule_id": rule_id},
    )


def decide_validate_twice(policy: dict) -> PolicyDecision:
    return PolicyDecision(
        "VALIDATE_TWICE", scope_key="",
        question="This record still fails validation after one automatic correction attempt.",
        evidence="Pydantic validation failed twice",
        suggested_action="review and correct the record by hand",
    )


def dupe_scope_key(identity_a: str, identity_b: str) -> str:
    """Built from stable identities (natural key or email), never from record
    ids: record ids are regenerated on every import, so a decision keyed on them
    could never be matched again on a re-run. Sorted so the pair keys the same
    way regardless of which record was seen first."""
    left, right = sorted((identity_a, identity_b))
    return f"{left}|{right}"


def decide_dupe_ambiguous(identity_a: str, identity_b: str, score: float, policy: dict,
                          judgement: dict | None = None) -> PolicyDecision:
    """Below the escalate band: not a duplicate, no action. Above auto_merge_above:
    the caller merges automatically, no escalation. Only the band in between asks.

    A model judgement, when one is supplied, can close the pair without asking --
    but in one direction only. It may conclude the two are different people,
    which leaves both records exactly as they are. It can never conclude they are
    the same, because that would merge two employees on a model's say-so and a
    merge cannot be undone. The asymmetry is the point: the automatic path is the
    one where being wrong changes nothing.
    """
    # The band's own bounds decide this, not auto_merge_above. They hold the
    # same value today, but reading the merge threshold here would silently drop
    # every pair between the two the moment someone tuned them apart.
    lo, hi = policy["dedupe"]["escalate_band"]
    if score < lo or score >= hi:
        return PolicyDecision(None)

    cfg = policy["dedupe"]
    if (judgement
            and judgement.get("verdict") == "different"
            and judgement.get("confidence", 0.0) >= cfg["llm_auto_separate_min_confidence"]):
        return PolicyDecision(None)

    scope_key = dupe_scope_key(identity_a, identity_b)
    evidence = (f"similarity score {score:.2f}, inside the {lo}-{hi} band "
                "where nothing is merged automatically")
    if judgement and judgement.get("rationale"):
        # The model looked and could not separate them either. Saying so is more
        # useful to the reader than a bare score, and it is presented as a second
        # opinion rather than an answer.
        evidence += f". The AI reviewed both records and was not confident either: {judgement['rationale']}"

    return PolicyDecision(
        "DUPE_AMBIGUOUS", scope_key,
        question="Two records look like they might be the same person, but it isn't certain.",
        evidence=evidence,
        suggested_action="keep them separate unless you recognise them as one person",
        suggested_value="keep_separate",
        options=["merge", "keep_separate"],
    )


def should_retry_push(status_code: int, policy: dict) -> bool:
    return status_code in policy["push"]["retry_on"]


def decide_push(employee_id: str, status_code: int, attempts: int, policy: dict,
                target_error: str = "") -> PolicyDecision:
    """A deterministic 4xx from the target is never retried into success and is
    never auto-corrected -- the target knows a rule the agent doesn't.

    The signature keys on the target's stated reason rather than the employee
    id, because a whole batch usually gets refused for one shared reason. Each
    rejection still gets its own escalation row (it is a per-record problem),
    but they collapse into a single question in the queue with an
    apply-to-all, instead of hundreds of identical cards."""
    if status_code not in policy["push"]["escalate_on"]:
        return PolicyDecision(None)

    reason = _normalise_target_error(target_error) or f"http_{status_code}"
    detail = f" ({target_error})" if target_error else ""
    return PolicyDecision(
        "PUSH_REJECTED", f"{status_code}:{reason}",
        question=f"The target system refuses these employees and won't accept a retry{detail}.",
        evidence=f"HTTP {status_code} after {attempts} attempt(s); first seen on {employee_id}",
        suggested_action="correct the records to satisfy the target's rule, or skip them",
        context={"status_code": status_code, "target_error": target_error,
                 "normalised_reason": reason},
    )


_QUOTED_VALUE_RE = re.compile(r"'[^']*'")


def _normalise_target_error(message: str) -> str:
    """Strips the specific offending value so rejections that differ only by
    which record tripped the rule share one signature."""
    return _QUOTED_VALUE_RE.sub("'?'", message).strip().lower()


def _precedence_rank(filename: str, policy: dict) -> int:
    """Lower wins. Mirrors reconcile._source_rank so the queue proposes the same
    value survivorship would have picked."""
    for i, name in enumerate(policy["cross_source"]["precedence"]):
        if filename.lower().startswith(name.lower()):
            return i
    return 999


def decide_cross_source_conflict(field_name: str, values_by_source: dict[str, object], policy: dict) -> PolicyDecision:
    """Only called for material fields where two-plus sources gave different
    non-null values -- non-material conflicts are resolved by source precedence
    without ever reaching here (policy.cross_source.escalate_conflicts_only_on_material_fields)."""
    # The signature is the field plus *which sources* disagree, not the specific
    # values. Every employee whose department is disputed between the same three
    # files is the same question asked over and over, and the useful answer is
    # not "this employee is in Sales", it is "for department, trust this file".
    # Keying it this way turns hundreds of per-employee cards into one decision
    # that establishes a truth ordering, and makes the answer reusable, which a
    # value-keyed signature never could be.
    sources = sorted(values_by_source)
    scope_key = f"{field_name}:{'|'.join(sources)}"
    winner = min(values_by_source, key=lambda src: _precedence_rank(src, policy))
    example = "; ".join(f"{src}: {v!r}" for src, v in sorted(values_by_source.items()))
    return PolicyDecision(
        "CONFLICT_ACROSS_SOURCES", scope_key,
        question=(f"{len(sources)} sources disagree on {field_name}. "
                  f"Which one should win whenever that happens?"),
        evidence=f"sources involved: {', '.join(sources)}. For example -- {example}",
        suggested_action=f"trust {winner} for {field_name}",
        suggested_value=winner,
        options=sources,
    )
