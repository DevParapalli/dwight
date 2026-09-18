from app.llm.client import LLMNotConfigured, complete_json
from app.llm.prompts import build_bulk_instruction_prompt
from app.schema.loader import Schema


def interpret_bulk_instruction(instruction: str, field_name: str, record_count: int,
                               schema: Schema) -> tuple[dict | None, str]:
    """Turns "set birthdate to 1919-01-01, we'll remap later" into a checked plan.

    Returns (plan, why_not). The plan is a proposal to show the consultant, never
    something applied here -- the point of reading an instruction back is that
    they get to see whether it was understood before anything happens.

    The model's freedom is deliberately tiny. It cannot pick the field, because
    the field is already established by what the target refused; it cannot vary
    the value per record, because that is what the table on the same page is for;
    and it cannot invent a value the instruction did not contain. All it does is
    read one sentence and say what it thinks was meant.
    """
    field = schema.fields.get(field_name)
    if field is None:
        return None, f"{field_name} is not a field in the target schema"
    if not instruction.strip():
        return None, "no instruction given"

    system_prompt, user_prompt = build_bulk_instruction_prompt(
        field_name, field, record_count, instruction.strip()
    )
    try:
        result, _ = complete_json(system_prompt, user_prompt, max_tokens=400)
    except LLMNotConfigured:
        return None, "no model is configured, so instructions cannot be read"

    if result.get("action") != "set_all":
        return None, (result.get("reading")
                      or "that instruction could not be turned into a single change")

    value = result.get("value")
    if value in (None, ""):
        return None, "no value was named, so there is nothing to set"

    value = str(value)
    if field.values and value not in field.values:
        return None, f"{value!r} is not an allowed value for {field_name}"

    return {
        "field": field_name,
        "value": value,
        "placeholder": bool(result.get("placeholder")),
        "reading": str(result.get("reading") or "")[:300],
        "record_count": record_count,
    }, ""
