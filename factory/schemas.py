"""Validate factory documents against schema/<kind>.schema.json (draft 2020-12)."""
from __future__ import annotations

import functools
import json
import pathlib

from jsonschema import Draft202012Validator

SCHEMA_DIR = pathlib.Path(__file__).resolve().parent.parent / "schema"
KINDS = ("factory", "card", "evidence", "judge")


class SchemaError(ValueError):
    """A document failed its schema; the message lists every failing JSON path."""


@functools.cache
def load(kind: str) -> Draft202012Validator:
    if kind not in KINDS:
        raise SchemaError(f"unknown schema kind: {kind!r}")
    schema = json.loads((SCHEMA_DIR / f"{kind}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(kind: str, doc: dict) -> None:
    errors = sorted(load(kind).iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [f"{e.json_path}: {e.message}" for e in errors]
        raise SchemaError(f"{kind} invalid:\n" + "\n".join(lines))
