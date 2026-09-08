"""The one JSON-schema validator behind the planner prompt, guard and executor.

Moved here from ``agent.py`` (Harness P1-A, constraint "同一规则只有一个实现"):
the planner payload advertises exactly these schemas, the guard validates
against exactly these schemas, and the executor re-validates the materialized
arguments with exactly the same function.  Deliberately lenient in the same
ways the Stage 5/6 contract requires: unknown keys are not rejected here
(the executor strips them and records a correction), ``rationale`` stays
optional, and there is intentionally NO provider ``oneOf`` schema.
"""
from __future__ import annotations

from typing import Any


def schema_errors(schema: dict[str, Any], value: Any, path: str = "proposal") -> list[str]:
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool),
             "boolean": isinstance(value, bool)}.get(kind, True)
    if not valid:
        return [f"{path} must be {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path} has an invalid value"]
    errors: list[str] = []
    if kind == "object":
        errors.extend(f"{path}.{key} is required" for key in schema.get("required", []) if key not in value)
        for key, item in schema.get("properties", {}).items():
            if key in value:
                errors.extend(schema_errors(item, value[key], f"{path}.{key}"))
    if kind == "array":
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path} requires at least one item")
        for item in value:
            errors.extend(schema_errors(schema.get("items", {}), item, path))
    if kind == "string" and not value.strip():
        errors.append(f"{path} must not be empty")
    return errors


def strip_unknown_keys(schema: dict[str, Any], arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Executor-side cleanliness: drop unknown argument keys, return what was
    dropped so the caller can record an auditable correction."""
    known = set(schema.get("properties", {}))
    dropped = sorted(set(arguments) - known)
    if not dropped:
        return arguments, []
    return {key: value for key, value in arguments.items() if key in known}, dropped
