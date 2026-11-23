from __future__ import annotations

import json
from pathlib import Path

import pytest

from radstudio.json_contract import decode_manifest_json
from radstudio.parameter_validation import validate_parameter_values

ROOT = Path(__file__).resolve().parents[3]
CASES = json.loads(
    (ROOT / "packages/operator-schema/tests/parameter-cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shared_parameter_cases(case):
    issues = validate_parameter_values(
        decode_manifest_json(case["value"].encode()), decode_manifest_json(case["schema"].encode())
    )
    assert (not issues) is case["valid"], issues
    assert any(issue["code"] == "parameter.schema_invalid" for issue in issues) is (
        not case["schemaValid"]
    )


def test_parameters_not_coerced_and_defaults_not_injected():
    value = {}
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer", "default": 1}},
        "required": ["x"],
    }
    issues = validate_parameter_values(value, schema)
    assert value == {}
    assert issues[0]["fieldPath"] == "/x"


def test_schema_cache_does_not_retain_mutable_caller_document():
    schema = {"type": "integer", "minimum": 1}
    assert not validate_parameter_values(1, schema)
    schema["minimum"] = 2
    assert validate_parameter_values(1, schema)
    assert not validate_parameter_values(2, schema)


def test_nested_pointer_and_json_bounds():
    assert (
        validate_parameter_values([1, "bad"], {"type": "array", "items": {"type": "integer"}})[0][
            "fieldPath"
        ]
        == "/1"
    )
    assert (
        validate_parameter_values(float("nan"), {"type": "number"})[0]["code"]
        == "parameter.json_invalid"
    )


def test_all_seeded_catalog_defaults_follow_parameter_contract():
    catalog = json.loads(
        (ROOT / "packages/operator-schema/operators.json").read_text(encoding="utf-8")
    )
    for definition in catalog:
        issues = validate_parameter_values(definition["defaults"], definition["parametersSchema"])
        assert not issues, (definition["type"], definition["version"], issues)


def test_parameter_meta_schema_copies_match():
    source = (ROOT / "packages/operator-schema/parameter-schema.schema.json").read_bytes()
    assert (
        source
        == (
            ROOT / "packages/python-sdk/radstudio/schemas/parameter-schema.schema.json"
        ).read_bytes()
    )


def test_monolith_graph_uses_same_ratio_bound():
    from radstudio.parameter_validation import ParameterValidationError
    from radstudio.runner import validate_graph

    catalog = json.loads(
        (ROOT / "packages/operator-schema/operators.json").read_text(encoding="utf-8")
    )
    definition = next(
        item for item in catalog if item["type"] == "HoldoutSplit" and item["version"] == "1.0.0"
    )
    parameters = dict(definition["defaults"], testRatio=0)
    ir = {
        "schemaVersion": "2",
        "nodes": [
            {
                "id": "holdout",
                "type": "HoldoutSplit",
                "operatorVersion": "1.0.0",
                "parameters": parameters,
            }
        ],
        "edges": [],
    }
    with pytest.raises(ParameterValidationError) as error:
        validate_graph(ir)
    assert any(issue["fieldPath"] == "/testRatio" for issue in error.value.issues)


def test_bound_messages_explain_exact_threshold():
    assert (
        validate_parameter_values(0, {"type": "number", "exclusiveMinimum": 0})[0]["message"]
        == "数值必须大于 0"
    )
    bound = decode_manifest_json(b"0.10000000000000000001")
    assert (
        str(bound)
        in validate_parameter_values(0, {"type": "number", "minimum": bound})[0]["message"]
    )


NUMBER_CASES = json.loads(
    (ROOT / "packages/operator-schema/tests/number-cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", NUMBER_CASES, ids=[case["name"] for case in NUMBER_CASES])
def test_shared_exact_number_budget(case):
    from radstudio.json_contract import canonical_manifest_json

    if not case["valid"]:
        with pytest.raises(ValueError):
            canonical_manifest_json(decode_manifest_json(case["raw"].encode()))
        return
    encoded = canonical_manifest_json(decode_manifest_json(case["raw"].encode()))
    assert encoded.decode() == case["canonical"]
    assert canonical_manifest_json(decode_manifest_json(encoded)) == encoded
