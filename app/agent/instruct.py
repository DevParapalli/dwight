from app.agent.clean import coerce_user_value
from app.agent.policy import load_policy
from app.llm.client import LLMNotConfigured, complete_json
from app.llm.prompts import build_bulk_instruction_prompt
from app.schema.loader import Schema
from app.settings import settings


def interpret_bulk_instruction(instruction: str, field_name: str, record_count: int,
                               schema: Schema,
                               current_values: list[str] | None = None) -> tuple[dict | None, str]:
    """Turns an instruction into a checked plan for filling the form in.

    Two shapes are allowed. **set_all** gives every record the same value, for
    "set it to 1919-01-01, we will remap later". **replace** maps named current
    values to named new ones and leaves every other record alone, for "replace
    Dircetor with Director" -- which is the shape that matters when one question
    covers many different wrong values.

    Returns (plan, why_not). The plan is a proposal to fill the form with, never
    something applied here: the point of reading an instruction back is that the
    consultant sees what was understood before anything happens.

    The model's freedom stays tiny. It cannot pick the field, because the field
    is already established by what the target refused, and it cannot invent a
    value the instruction did not contain. It reads one sentence and says what it
    thinks was meant.
    """
    field = schema.fields.get(field_name)
    if field is None:
        return None, f"{field_name} is not a field in the target schema"
    if not instruction.strip():
        return None, "no instruction given"

    accepted_formats = load_policy()["dates"]["accepted_formats"]
    system_prompt, user_prompt = build_bulk_instruction_prompt(
        field_name, field, record_count, instruction.strip(), current_values
    )
    try:
        # Same reasoning-model ceiling as the duplicate judge.
        result, _ = complete_json(system_prompt, user_prompt,
                                  max_tokens=settings.llm_max_output_tokens)
    except LLMNotConfigured:
        return None, "no model is configured, so instructions cannot be read"

    action = result.get("action")
    reading = str(result.get("reading") or "")[:300]

    if action == "set_all":
        value, why_not = coerce_user_value(str(result.get("value") or ""), field, accepted_formats)
        if value is None:
            return None, why_not
        return {
            "field": field_name, "action": "set_all", "value": value,
            "replacements": {}, "placeholder": bool(result.get("placeholder")),
            "reading": reading, "record_count": record_count,
        }, ""

    if action == "replace":
        raw = result.get("replacements") or {}
        if not isinstance(raw, dict) or not raw:
            return None, reading or "no replacements were named"

        # Each replacement is checked the same way a typed value would be, so a
        # model cannot smuggle a value past the field's own rules by putting it
        # on the right-hand side of a mapping.
        replacements = {}
        for old_value, new_value in raw.items():
            checked, why_not = coerce_user_value(str(new_value or ""), field, accepted_formats)
            if checked is None:
                return None, f"cannot replace {old_value!r}: {why_not}"
            replacements[str(old_value)] = checked

        return {
            "field": field_name, "action": "replace", "value": None,
            "replacements": replacements, "placeholder": False,
            "reading": reading, "record_count": record_count,
        }, ""

    return None, reading or "that instruction could not be turned into a change"
