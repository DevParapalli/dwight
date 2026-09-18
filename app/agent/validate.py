from dataclasses import dataclass
from dataclasses import field as dc_field

from pydantic import ValidationError

from app.agent.clean import (
    clean_email,
    clean_number,
    clean_phone,
    clean_string,
    coerce_enum,
    parse_date_value,
)
from app.agent.policy import (
    Issue,
    decide_date,
    decide_enum_coercion,
    decide_llm_normalization,
    decide_logic_contradiction,
    decide_validate_twice,
)
from app.llm.client import LLMNotConfigured, complete_json
from app.llm.prompts import build_correction_prompt
from app.schema.loader import Schema, SchemaField
from app.schema.pydantic_builder import build_model
from app.schema.rules import RuleContext, evaluate_rules

RULE_PRIMARY_FIELD = {
    "R1": "termination_date", "R2": "termination_date", "R3": "termination_date",
    "R4": "date_of_birth", "R5": "manager_employee_id", "R6": "manager_employee_id",
}


@dataclass
class CleanResult:
    cleaned: dict
    issues: list[Issue] = dc_field(default_factory=list)


def _clean_enum_field(f: SchemaField, raw_value, policy: dict, normalization_map: dict | None) -> tuple[str | None, list[Issue]]:
    issues: list[Issue] = []
    s = clean_string(raw_value)
    if s is None:
        return None, issues

    coerced, edit_distance, nearest = coerce_enum(s, f.values, policy["values"]["enum_auto_max_edit_distance"])
    if coerced is not None:
        return coerced, issues

    llm_result = (normalization_map or {}).get(s)
    if llm_result is None:
        decision = decide_enum_coercion(f.name, s, edit_distance, policy, nearest,
                                        allowed_values=f.values)
        if decision.reason_code:
            issues.append(Issue(decision, f.name))
        return None, issues

    decision = decide_llm_normalization(f.name, s, llm_result["confidence"], policy,
                                        llm_result.get("value"), allowed_values=f.values)
    if decision.reason_code:
        issues.append(Issue(decision, f.name))
        return None, issues
    return llm_result["value"], issues


def _clean_date_field(f: SchemaField, raw_value, filename: str, policy: dict) -> tuple[object, list[Issue]]:
    issues: list[Issue] = []
    parsed, ambiguous = parse_date_value(raw_value, policy["dates"]["accepted_formats"])
    if ambiguous:
        decision = decide_date(f.name, filename, True, policy, raw_value=str(raw_value or ""))
        if decision.reason_code:
            issues.append(Issue(decision, f.name))
    return parsed, issues


def _attempt_correction(cleaned: dict, errors: list[str], schema: Schema, model) -> tuple[dict, list[str]]:
    """The one permitted correction attempt (policy.validation.max_correction_attempts).
    Only touches fields the errors actually named -- the prompt says so, and this
    is the defense-in-depth check that doesn't trust the LLM to have obeyed it."""
    try:
        system_prompt, user_prompt = build_correction_prompt(cleaned, errors, schema)
        response, _latency_ms = complete_json(system_prompt, user_prompt)
    except LLMNotConfigured:
        return cleaned, errors

    error_fields = {e.split(":", 1)[0].strip() for e in errors}
    corrections = {k: v for k, v in response.get("corrections", {}).items() if k in error_fields}
    corrected = {**cleaned, **corrections}

    try:
        model(**corrected)
        return corrected, []
    except ValidationError as e:
        return corrected, [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]


def validate_and_clean_record(
    raw_record: dict, schema: Schema, filename: str, policy: dict,
    normalization_maps: dict[str, dict] | None = None,
) -> CleanResult:
    normalization_maps = normalization_maps or {}
    cleaned: dict = {}
    issues: list[Issue] = []

    for field_name, raw_value in raw_record.items():
        f = schema.fields.get(field_name)
        if f is None or raw_value in (None, "") or field_name == "annual_ctc":
            continue
        if f.type == "enum":
            value, field_issues = _clean_enum_field(f, raw_value, policy, normalization_maps.get(field_name))
        elif f.type == "date":
            value, field_issues = _clean_date_field(f, raw_value, filename, policy)
        elif f.type == "email":
            value, field_issues = clean_email(raw_value), []
        elif f.type == "phone":
            value, field_issues = clean_phone(raw_value, f.default_region or "IN"), []
        else:
            value, field_issues = clean_string(raw_value), []
        cleaned[field_name] = value
        issues.extend(field_issues)

    if raw_record.get("annual_ctc") not in (None, ""):
        amount, currency = clean_number(raw_record["annual_ctc"])
        cleaned["annual_ctc"] = amount
        if currency:
            cleaned["ctc_currency"] = currency

    model = build_model(schema, require_all=False)
    errors: list[str] = []
    try:
        model(**cleaned)
    except ValidationError as e:
        errors = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]

    if errors:
        cleaned, errors = _attempt_correction(cleaned, errors, schema, model)
    if errors:
        issues.append(Issue(decide_validate_twice(policy), None))

    # R6 ("manager_employee_id resolves to a known employee_id") needs the full
    # set of employee_ids across every source in the run, which only exists once
    # M5's reconciliation has seen all of them. Evaluating it per-row against an
    # empty RuleContext would make it fail for nearly every record that has a
    # manager at all -- deferred, not evaluated here.
    per_row_rules = [r for r in schema.cross_field_rules if r.id != "R6"]
    for rule_id in evaluate_rules(cleaned, per_row_rules, RuleContext()):
        rule_field = RULE_PRIMARY_FIELD[rule_id]
        issues.append(Issue(decide_logic_contradiction(rule_id, rule_field, policy), rule_field))

    return CleanResult(cleaned=cleaned, issues=issues)
