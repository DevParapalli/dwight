import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, create_model, field_validator

from app.schema.loader import Schema, SchemaField

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _base_type(f: SchemaField) -> type:
    if f.type == "string":
        return str
    if f.type == "email":
        return str
    if f.type == "phone":
        return str
    if f.type == "date":
        return date
    if f.type == "decimal":
        return Decimal
    if f.type == "enum":
        if not f.values:
            raise ValueError(f"enum field {f.name!r} has no values")
        return Literal[tuple(f.values)]
    raise ValueError(f"unknown schema field type {f.type!r} on {f.name!r}")


def _field_definition(f: SchemaField, require_all: bool) -> tuple[type, object]:
    py_type = _base_type(f)
    field_kwargs: dict = {}
    if f.type == "string" and f.pattern:
        field_kwargs["pattern"] = f.pattern

    if f.required and require_all:
        default = ... if not field_kwargs else Field(..., **field_kwargs)
    else:
        py_type = py_type | None
        default_value = f.default if f.default is not None else None
        default = default_value if not field_kwargs else Field(default_value, **field_kwargs)

    return py_type, default


def _validator_for(f: SchemaField):
    if f.type == "email":
        def check(cls, v: str | None) -> str | None:
            if v is not None and not _EMAIL_RE.match(v):
                raise ValueError(f"{f.name} is not a valid email: {v!r}")
            return v
        return check
    if f.type == "phone":
        def check(cls, v: str | None) -> str | None:
            if v is not None and not _E164_RE.match(v):
                raise ValueError(f"{f.name} is not E.164 formatted: {v!r}")
            return v
        return check
    return None


def build_model(schema: Schema, name: str = "Employee", require_all: bool = True) -> type[BaseModel]:
    """Build a Pydantic model from a schema at run time. Field shape comes entirely
    from the YAML; cross-record concerns (uniqueness, manager references) are
    handled by app/schema/rules.py and the dedupe stage, not here.

    require_all=False builds a lenient variant where every field is optional
    regardless of the schema's `required` flag. A single source row only ever
    supplies a subset of the schema (the rest comes from other sources at
    reconciliation), so per-row cleaning/validation must not fail on missingness
    -- only the fully-reconciled record, built in M5, is checked against the
    real required-field set."""
    field_defs: dict[str, tuple[type, object]] = {}
    validators: dict[str, classmethod] = {}

    for f in schema.fields.values():
        field_defs[f.name] = _field_definition(f, require_all)
        raw_check = _validator_for(f)
        if raw_check is not None:
            validators[f"_check_{f.name}"] = field_validator(f.name)(raw_check)

    return create_model(name, __validators__=validators, **field_defs)
