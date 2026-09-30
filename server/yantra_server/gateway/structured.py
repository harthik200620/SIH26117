"""Structured decoding (SPEC §7.3): constraint construction per engine + output validation.

Confirmed against vLLM ≥ 0.12 docs (2026-08): OpenAI-compatible `response_format:
{"type":"json_schema", ...}` plus the unified `structured_outputs` request field carrying
`json` / `regex` / `choice` / `grammar` (the old `guided_*` fields are deprecated).
llama.cpp `llama-server` accepts `response_format json_schema` and a top-level GBNF
`grammar` field. Each form has a unit test in tests/unit/test_structured.py.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ValidationError

from .engines.base import Constraint, MalformedOutput, ToolSpec

MAX_INLINE_DEPTH = 24


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local $defs/$ref into a self-contained schema (engines and mocks need this)."""
    defs = schema.get("$defs", {})

    def resolve(node: Any, depth: int) -> Any:
        if depth > MAX_INLINE_DEPTH:
            return node
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                name = node["$ref"].rsplit("/", 1)[-1]
                if name in defs:
                    merged = {k: v for k, v in node.items() if k != "$ref"}
                    resolved = resolve(copy.deepcopy(defs[name]), depth + 1)
                    if isinstance(resolved, dict):
                        resolved.update(merged)
                        return resolved
                return node
            return {k: resolve(v, depth + 1) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item, depth + 1) for item in node]
        return node

    result = resolve({k: v for k, v in schema.items() if k != "$defs"}, 0)
    assert isinstance(result, dict)
    return result


def require_const_props(schema: dict[str, Any]) -> dict[str, Any]:
    """Make every `const` property required, in place semantics on a copy.

    Discriminator fields (e.g. Check.kind) carry Python defaults, so Pydantic leaves them
    out of `required`; an instance omitting them then matches several oneOf branches and
    strict validation rejects it. Forcing consts to be emitted keeps grammar-constrained
    decoding unambiguous on every engine.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items()}
            props = out.get("properties")
            if isinstance(props, dict):
                const_keys = [k for k, v in props.items() if isinstance(v, dict) and "const" in v]
                if const_keys:
                    required = list(out.get("required", []))
                    for key in const_keys:
                        if key not in required:
                            required.append(key)
                    out["required"] = required
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    result = walk(copy.deepcopy(schema))
    assert isinstance(result, dict)
    return result


@lru_cache(maxsize=256)
def _schema_cached(model: type[BaseModel]) -> str:
    return json.dumps(require_const_props(inline_refs(model.model_json_schema())))


def schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Self-contained JSON schema for a Pydantic model, cached per class."""
    loaded: dict[str, Any] = json.loads(_schema_cached(model))
    return loaded


def constraint_for(model: type[BaseModel]) -> Constraint:
    return Constraint(kind="json_schema", json_schema=tighten(schema_for(model)))


def choice_constraint(choices: list[str]) -> Constraint:
    return Constraint(kind="choice", choices=choices)


def action_schema(
    tools: list[ToolSpec], finish_schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """`oneOf` over the tool schemas in play plus finish: a tool outside the manifest or an
    argument violating its schema becomes impossible to emit (SPEC §7.3)."""
    branches: list[dict[str, Any]] = []
    for tool in tools:
        params = inline_refs(tool.parameters) if tool.parameters else {"type": "object"}
        branches.append(
            {
                "type": "object",
                "properties": {
                    "tool": {"const": tool.name},
                    "args": params,
                },
                "required": ["tool", "args"],
                "additionalProperties": False,
            }
        )
    if finish_schema is not None:
        branches.append(
            {
                "type": "object",
                "properties": {"tool": {"const": "finish"}, "args": inline_refs(finish_schema)},
                "required": ["tool", "args"],
                "additionalProperties": False,
            }
        )
    return {
        "type": "object",
        "properties": {
            # Explanatory text is bounded by the executor before persistence.
            # Rejecting it here discards otherwise valid actions on engines that
            # cannot grammar-enforce string lengths. Tool arguments stay strict.
            "thought": {"type": "string", "description": "Brief reason for this action"},
            "action": {"oneOf": branches} if len(branches) != 1 else branches[0],
        },
        "required": ["thought", "action"],
        "additionalProperties": False,
    }


def tighten(schema: dict[str, Any]) -> dict[str, Any]:
    """additionalProperties:false everywhere — the retry form after a malformed output."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items()}
            if out.get("type") == "object" or "properties" in out:
                out.setdefault("additionalProperties", False)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    result = walk(copy.deepcopy(schema))
    assert isinstance(result, dict)
    return result


# ------------------------------------------------------------------ request payloads


def vllm_structured_fields(constraint: Constraint | None) -> dict[str, Any]:
    """Request fields for vLLM's OpenAI-compatible server."""
    if constraint is None:
        return {}
    if constraint.kind == "json_schema" and constraint.json_schema is not None:
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "yantra_output",
                    "schema": constraint.json_schema,
                    "strict": True,
                },
            }
        }
    if constraint.kind == "regex" and constraint.regex:
        return {"structured_outputs": {"regex": constraint.regex}}
    if constraint.kind == "choice" and constraint.choices:
        return {"structured_outputs": {"choice": constraint.choices}}
    if constraint.kind == "grammar" and constraint.grammar:
        return {"structured_outputs": {"grammar": constraint.grammar}}
    return {}


def _strip_string_lengths(schema: Any) -> Any:
    """Drop min/maxLength on strings for llama.cpp: its grammar compiler expands them into
    char{m,n} repetition rules and rejects the whole grammar past a complexity cap
    ("number of rules that are going to be repeated ... exceeds sane defaults"), which
    500s every request. The caller's pydantic validation still enforces the bounds."""
    if isinstance(schema, dict):
        cleaned = {
            k: _strip_string_lengths(v)
            for k, v in schema.items()
            if not (k in ("minLength", "maxLength") and schema.get("type") == "string")
        }
        return cleaned
    if isinstance(schema, list):
        return [_strip_string_lengths(item) for item in schema]
    return schema


def llamacpp_structured_fields(constraint: Constraint | None) -> dict[str, Any]:
    """Request fields for llama.cpp `llama-server` (GBNF via `grammar`, schema via
    `response_format json_schema`; choices/regex are compiled to a GBNF grammar)."""
    if constraint is None:
        return {}
    if constraint.kind == "json_schema" and constraint.json_schema is not None:
        schema = _strip_string_lengths(constraint.json_schema)
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "yantra_output", "schema": schema},
            }
        }
    if constraint.kind == "grammar" and constraint.grammar:
        return {"grammar": constraint.grammar}
    if constraint.kind == "choice" and constraint.choices:
        return {"grammar": choices_to_gbnf(constraint.choices)}
    if constraint.kind == "regex" and constraint.regex:
        # llama-server has no native regex constraint; post-validation still enforces it.
        return {}
    return {}


def choices_to_gbnf(choices: list[str]) -> str:
    alternatives = " | ".join(json.dumps(choice) for choice in choices)
    return f"root ::= {alternatives}"


# ------------------------------------------------------------------ output validation


def validate_output(
    text: str,
    constraint: Constraint | None,
    model: type[BaseModel] | None = None,
) -> Any:
    """Post-validate engine output (engines differ; SPEC §7.3). Returns the parsed value:
    a Pydantic instance when `model` is given, the raw object/string otherwise."""
    if constraint is None:
        return text
    if constraint.kind == "choice":
        stripped = text.strip().strip('"')
        if constraint.choices and stripped not in constraint.choices:
            raise MalformedOutput(f"output {stripped!r} not in choices {constraint.choices}")
        return stripped
    if constraint.kind == "regex":
        import re

        if constraint.regex and not re.fullmatch(constraint.regex, text.strip()):
            raise MalformedOutput(f"output does not match regex {constraint.regex!r}")
        return text.strip()
    if constraint.kind == "json_schema":
        try:
            obj = json.loads(_extract_json(text))
        except json.JSONDecodeError as exc:
            raise MalformedOutput(f"invalid JSON: {exc}") from exc
        if model is not None:
            try:
                return model.model_validate(obj)
            except ValidationError as exc:
                raise MalformedOutput(f"schema validation failed: {exc}") from exc
        if constraint.json_schema is not None:
            import jsonschema

            try:
                jsonschema.validate(obj, constraint.json_schema)
            except jsonschema.ValidationError as exc:
                raise MalformedOutput(f"schema validation failed: {exc.message}") from exc
        return obj
    return text


def _extract_json(text: str) -> str:
    """Trim reasoning-model wrappers: leading/trailing prose or code fences around the JSON."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
        stripped = stripped.strip()
    if stripped.startswith(("{", "[")):
        return stripped
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            return stripped[start : end + 1]
    return stripped
