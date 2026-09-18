from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class SchemaField:
    name: str
    type: str
    required: bool = False
    pattern: str | None = None
    unique: bool = False
    values: list[str] | None = None
    default: object = None
    format: str | None = None
    default_region: str | None = None
    references: str | None = None
    material: bool = False


@dataclass
class CrossFieldRule:
    id: str
    rule: str


@dataclass
class Schema:
    version: int
    entity: str
    natural_key: str
    fields: dict[str, SchemaField] = field(default_factory=dict)
    cross_field_rules: list[CrossFieldRule] = field(default_factory=list)


def load_schema(path: Path) -> Schema:
    raw = yaml.safe_load(path.read_text())
    fields = {
        name: SchemaField(name=name, **spec) for name, spec in raw["fields"].items()
    }
    rules = [CrossFieldRule(**r) for r in raw.get("cross_field_rules", [])]
    return Schema(
        version=raw["version"],
        entity=raw["entity"],
        natural_key=raw["natural_key"],
        fields=fields,
        cross_field_rules=rules,
    )
