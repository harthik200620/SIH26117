import pytest
from pydantic import BaseModel

from yantra_server.gateway.engines.base import Constraint, MalformedOutput, ToolSpec
from yantra_server.gateway.structured import (
    action_schema,
    choices_to_gbnf,
    llamacpp_structured_fields,
    schema_for,
    tighten,
    validate_output,
    vllm_structured_fields,
)


class Inner(BaseModel):
    x: int


class Outer(BaseModel):
    name: str
    inner: Inner
    items: list[Inner]


def test_inline_refs_self_contained() -> None:
    schema = schema_for(Outer)
    assert "$defs" not in schema and "$ref" not in str(schema)
    assert schema["properties"]["inner"]["properties"]["x"]["type"] == "integer"
    assert schema["properties"]["items"]["items"]["properties"]["x"]["type"] == "integer"


def test_action_schema_oneof_tools_plus_finish() -> None:
    tools = [
        ToolSpec(
            name="read_file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        ToolSpec(
            name="grep",
            parameters={
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        ),
    ]
    finish = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    schema = action_schema(tools, finish)
    branches = schema["properties"]["action"]["oneOf"]
    consts = [b["properties"]["tool"]["const"] for b in branches]
    assert consts == ["read_file", "grep", "finish"]
    # Explanations are truncated by the executor; tool argument validation is strict.
    import json

    constraint = Constraint(kind="json_schema", json_schema=schema)
    action = {
        "thought": "Read the source evidence. " * 30,
        "action": {"tool": "read_file", "args": {"path": "source.pdf"}},
    }
    assert validate_output(json.dumps(action), constraint)["action"]["tool"] == "read_file"
    action["action"]["tool"] = "not_allowed"
    with pytest.raises(MalformedOutput):
        validate_output(json.dumps(action), constraint)
    import jsonschema

    jsonschema.validate(
        {"thought": "read it", "action": {"tool": "read_file", "args": {"path": "a.txt"}}}, schema
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"thought": "x", "action": {"tool": "not_a_tool", "args": {}}}, schema)


def test_vllm_forms() -> None:
    js = vllm_structured_fields(Constraint(kind="json_schema", json_schema={"type": "object"}))
    assert js["response_format"]["type"] == "json_schema"
    assert js["response_format"]["json_schema"]["strict"] is True

    rx = vllm_structured_fields(Constraint(kind="regex", regex="[A-Z]-\\d+"))
    assert rx == {"structured_outputs": {"regex": "[A-Z]-\\d+"}}

    ch = vllm_structured_fields(Constraint(kind="choice", choices=["a", "b"]))
    assert ch == {"structured_outputs": {"choice": ["a", "b"]}}

    gr = vllm_structured_fields(Constraint(kind="grammar", grammar='root ::= "x"'))
    assert gr == {"structured_outputs": {"grammar": 'root ::= "x"'}}

    assert vllm_structured_fields(None) == {}


def test_llamacpp_forms() -> None:
    js = llamacpp_structured_fields(Constraint(kind="json_schema", json_schema={"type": "object"}))
    assert js["response_format"]["type"] == "json_schema"

    ch = llamacpp_structured_fields(Constraint(kind="choice", choices=["yes", "no"]))
    assert ch == {"grammar": 'root ::= "yes" | "no"'}

    gr = llamacpp_structured_fields(Constraint(kind="grammar", grammar="root ::= [0-9]+"))
    assert gr == {"grammar": "root ::= [0-9]+"}


def test_choices_to_gbnf_escapes() -> None:
    assert choices_to_gbnf(['a"b']) == 'root ::= "a\\"b"'


def test_tighten_adds_additional_properties() -> None:
    schema = {
        "type": "object",
        "properties": {"nested": {"type": "object", "properties": {"a": {"type": "string"}}}},
    }
    tightened = tighten(schema)
    assert tightened["additionalProperties"] is False
    assert tightened["properties"]["nested"]["additionalProperties"] is False


def test_validate_output_json_paths() -> None:
    constraint = Constraint(kind="json_schema", json_schema=schema_for(Inner))
    parsed = validate_output('{"x": 3}', constraint, Inner)
    assert isinstance(parsed, Inner) and parsed.x == 3
    # fenced output from a reasoning model
    parsed2 = validate_output('Here is the answer:\n```json\n{"x": 4}\n```', constraint, Inner)
    assert isinstance(parsed2, Inner) and parsed2.x == 4
    with pytest.raises(MalformedOutput):
        validate_output('{"x": "not-an-int"}', constraint, Inner)
    with pytest.raises(MalformedOutput):
        validate_output("{broken", constraint, Inner)


def test_validate_output_choice_and_regex() -> None:
    assert validate_output(" yes ", Constraint(kind="choice", choices=["yes", "no"])) == "yes"
    with pytest.raises(MalformedOutput):
        validate_output("maybe", Constraint(kind="choice", choices=["yes", "no"]))
    assert validate_output("P-101", Constraint(kind="regex", regex=r"P-\d+")) == "P-101"
    with pytest.raises(MalformedOutput):
        validate_output("nope", Constraint(kind="regex", regex=r"P-\d+"))
