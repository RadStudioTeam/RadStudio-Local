import hashlib
import json

from radstudio.json_contract import canonical_manifest_json
from radstudio.operator import ArtifactStore, Operator, invoke, operator


def contract():
    value = {
        "version": 1,
        "dialect": "radstudio-parameters/1",
        "operatorType": "ContractProbe",
        "operatorVersion": "1.0.0",
        "schema": {
            "type": "object",
            "properties": {"value": {"type": "integer", "minimum": 1}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    return {**value, "digest": hashlib.sha256(canonical_manifest_json(value)).hexdigest()}


@operator("ContractProbe", version="1.0.0")
class ContractProbe(Operator):
    def run(self, ctx):
        return {"out": ctx.output_json("out.json", {"parameters": ctx.parameters}, "report")}


def request():
    return {
        "requestId": "r",
        "runId": "run",
        "nodeId": "n",
        "operatorType": "ContractProbe",
        "operatorVersion": "1.0.0",
        "parameters": {"value": 1},
        "inputs": {},
        "parameterContract": contract(),
    }


def test_frozen_parameter_contract_validates_before_operator_runs(tmp_path):
    value = request()
    value["parameters"]["value"] = 0
    result = invoke(value, ArtifactStore(tmp_path)).to_dict()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "operator.parameters_invalid"
    assert result["error"]["details"]["issues"][0]["fieldPath"] == "/value"
    assert not (tmp_path / "out.json").exists()


def test_parameter_contract_digest_and_binding_are_checked(tmp_path):
    for key, changed in (("digest", "0" * 64), ("operatorType", "Other"), ("version", 2)):
        value = request()
        value["parameterContract"][key] = changed
        result = invoke(value, ArtifactStore(tmp_path)).to_dict()
        assert result["error"]["code"] == "operator.contract_invalid"


def test_receipt_binds_to_validated_definition(tmp_path):
    value = request()
    result = invoke(value, ArtifactStore(tmp_path)).to_dict()
    assert result["status"] == "completed"
    assert result["executionContract"] == {
        "version": 1,
        "parameterDigest": value["parameterContract"]["digest"],
    }
    assert json.loads((tmp_path / "out.json").read_text())["parameters"] == {"value": 1}


def test_shared_frozen_digest():
    assert (
        contract()["digest"] == "4d6a118fa40484895cbac12685ced83f1dcd98541d1e5657ccc43c573362e85e"
    )


def test_controls_remain_outside_logical_parameters(monkeypatch, tmp_path):
    from radstudio import operator as framework
    from radstudio.execution_contract import execution_parameters

    monkeypatch.setattr(framework, "OPERATOR_REGISTRY", dict(framework.OPERATOR_REGISTRY))

    @operator("DLCNNTraining", version="1.0.0")
    class ControlProbe(Operator):
        def run(self, ctx):
            effective = execution_parameters(ctx)
            assert effective["foldIndices"] == [0, 1]
            assert "foldIndices" not in ctx.parameters
            assert effective["version"] == "logical-version"
            return {}

    value = request()
    value.pop("parameterContract")
    value["operatorType"] = "DLCNNTraining"
    value["parameters"] = {"version": "logical-version"}
    value["executionControls"] = {
        "version": 1,
        "shardIndex": 0,
        "shardCount": 2,
        "foldIndices": [0, 1],
    }
    result = invoke(value, ArtifactStore(tmp_path)).to_dict()
    assert result["status"] == "completed"
    assert result["executionContract"] == {"version": 1, "parameterDigest": ""}


def test_invalid_controls_fail_before_execution(tmp_path):
    for controls in (
        {"version": 2},
        {"version": 1, "shardCount": 0},
        {"version": 1, "shardIndex": 2, "shardCount": 2},
        {"version": 1, "foldIndices": [0, 0]},
        {"version": 1, "untrusted": "value"},
    ):
        value = request()
        value.pop("parameterContract")
        value["operatorType"] = "DLCNNTraining"
        value["executionControls"] = controls
        result = invoke(value, ArtifactStore(tmp_path)).to_dict()
        assert result["error"]["code"] == "operator.contract_invalid"
    assert not (tmp_path / "out.json").exists()


def test_invalid_contract_does_not_download_input(monkeypatch):
    from radstudio import operator_serve

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid contract downloaded input")

    monkeypatch.setattr(operator_serve, "_materialize_input_manifest", forbidden)
    value = request()
    value["parameters"]["value"] = 0
    value["transfer"] = {
        "mode": "object",
        "protocolVersion": 2,
        "inputManifestUrl": "https://invalid.example/synthetic",
    }
    result = operator_serve.execute_payload({"request": value}).to_dict()
    assert result["error"]["code"] == "operator.parameters_invalid"


def test_runtime_json_preserves_decimal_and_native_scientific_values():
    from decimal import Decimal

    from radstudio.json_contract import decode_runtime_json, encode_runtime_json

    native = {"tiny": 1e-200, "labels": {0: "negative", 1: "positive"}}
    assert (
        encode_runtime_json(native)
        == json.dumps(native, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    )
    value = {1: Decimal("0.10000000000000001"), 2: [1e-200, Decimal("1e-200")]}
    loaded = decode_runtime_json(encode_runtime_json(value))
    assert loaded["1"] == Decimal("0.10000000000000001")
    assert loaded["2"] == [Decimal("1e-200"), Decimal("1e-200")]


def test_runtime_json_rejects_ambiguous_and_nonfinite_values():
    from decimal import Decimal

    import pytest

    from radstudio.json_contract import decode_runtime_json, encode_runtime_json

    for raw in (b'{"a":1,"a":2}', b'{"a":"\\ud800"}', b'{"a":NaN}', b"{} {}", bytes([34, 255, 34])):
        with pytest.raises((ValueError, UnicodeError)):
            decode_runtime_json(raw)
    for value in (Decimal("NaN"), Decimal("Infinity"), float("nan")):
        with pytest.raises(ValueError):
            encode_runtime_json(value)


def test_decimal_terminal_status_is_serialized(monkeypatch):
    from decimal import Decimal

    import httpx

    from radstudio import operator_serve
    from radstudio.json_contract import decode_runtime_json

    captured = []

    def accept(request):
        captured.append(decode_runtime_json(request.content))
        return httpx.Response(200)

    monkeypatch.setattr(
        operator_serve,
        "_transfer_client",
        lambda: httpx.Client(transport=httpx.MockTransport(accept)),
    )
    writer = operator_serve._StatusWriter({"statusUrl": "https://example.invalid/status"})
    writer._put(
        {"status": "completed", "result": {"metrics": {"exact": Decimal("0.10000000000000001")}}}
    )
    assert len(captured) == 1
    assert captured[0]["result"]["metrics"]["exact"] == Decimal("0.10000000000000001")


def test_contract_envelope_does_not_reduce_schema_depth_budget(tmp_path):
    from radstudio.parameter_validation import validate_parameter_values

    schema, nested = {"type": "integer"}, 1
    for _ in range(61):
        schema, nested = {"type": "array", "items": schema}, [nested]
    schema = {
        "type": "object",
        "properties": {"nested": schema},
        "required": ["nested"],
        "additionalProperties": False,
    }
    value = request()
    value["parameters"] = {"nested": nested}
    value["parameterContract"]["schema"] = schema
    definition = {key: item for key, item in value["parameterContract"].items() if key != "digest"}
    value["parameterContract"]["digest"] = hashlib.sha256(
        canonical_manifest_json(definition)
    ).hexdigest()
    assert not validate_parameter_values(value["parameters"], schema)
    result = invoke(value, ArtifactStore(tmp_path)).to_dict()
    assert result["status"] == "completed", result


def bound_request():
    from radstudio.execution_contract import freeze_invocation_contract
    from radstudio.operator import OperatorRequest

    value = request()
    value["invocationContract"] = freeze_invocation_contract(OperatorRequest.from_dict(value))
    return value


def test_invocation_receipt_binds_actual_request(tmp_path):
    value = bound_request()
    result = invoke(value, ArtifactStore(tmp_path)).to_dict()
    assert result["status"] == "completed"
    assert result["executionContract"] == {
        "version": 2,
        "parameterDigest": value["parameterContract"]["digest"],
        "invocationDigest": value["invocationContract"]["digest"],
    }


def test_invocation_tampering_fails_before_execution(tmp_path):
    import copy

    changes = {
        "requestId": "another-request",
        "runId": "another-run",
        "nodeId": "another-node",
        "parameters": {"value": 2},
        "inputs": {"source": "different.json"},
        "environment": {"imageDigest": "changed"},
    }
    for key, changed in changes.items():
        value = copy.deepcopy(bound_request())
        value[key] = changed
        events = []
        result = invoke(
            value,
            ArtifactStore(tmp_path / key),
            emit=lambda event, data, captured=events: captured.append(event),
        )
        assert result.status == "failed", key
        assert result.error.code == "operator.contract_invalid", key
        assert not result.artifacts and not result.outputs
        assert "node.started" not in events and "node.completed" not in events


def test_invocation_invalid_digest_prevents_input_download(monkeypatch):
    from radstudio import operator_serve

    monkeypatch.setattr(
        operator_serve,
        "_materialize_input_manifest",
        lambda *args: (_ for _ in ()).throw(AssertionError("downloaded")),
    )
    value = bound_request()
    value["invocationContract"]["digest"] = "0" * 64
    value["transfer"] = {
        "mode": "object",
        "protocolVersion": 2,
        "inputManifestUrl": "https://invalid.example/synthetic",
    }
    result = operator_serve.execute_payload({"request": value})
    assert result.status == "failed"
    assert result.error.code == "operator.contract_invalid"


def test_serve_injects_workspace_root_after_invocation_check():
    """serve 注入工作区根必须发生在校验之后，否则第二次校验会判定摘要不符。"""
    from radstudio import operator_serve

    value = bound_request()
    result = operator_serve.execute_payload({"request": value})
    assert result.status == "completed", result.error
    assert result.executionContract["invocationDigest"] == value["invocationContract"]["digest"]


def test_invocation_controls_and_exact_values_change_digest():
    from decimal import Decimal

    from radstudio.execution_contract import freeze_invocation_contract
    from radstudio.operator import OperatorRequest

    value = request()
    value["operatorType"] = "DLCNNTraining"
    value.pop("parameterContract")
    from radstudio.execution_contract import freeze_parameter_contract

    value["parameterContract"] = freeze_parameter_contract("DLCNNTraining", "1.0.0", {})
    value["parameters"] = {"threshold": Decimal("0.12345678901234567890123456789")}
    req = OperatorRequest.from_dict(value)
    first = freeze_invocation_contract(req)
    req.executionControls = {"version": 1, "shardIndex": 0, "shardCount": 2}
    second = freeze_invocation_contract(req)
    req.executionControls["shardIndex"] = 1
    third = freeze_invocation_contract(req)
    req.parameters["threshold"] = Decimal("0.12345678901234567890123456790")
    fourth = freeze_invocation_contract(req)
    assert len({item["digest"] for item in (first, second, third, fourth)}) == 4


def test_invocation_contract_rejects_null_and_bad_shape(tmp_path):
    for binding in (
        {},
        {"version": 2, "digest": "0" * 64},
        {"version": 1, "digest": "0" * 64, "unknown": True},
    ):
        value = request()
        value["invocationContract"] = binding
        result = invoke(value, ArtifactStore(tmp_path)).to_dict()
        assert result["status"] == "failed"
        assert not result["outputs"]


def test_invocation_contract_explicit_null_rejected():
    import pytest

    from radstudio.operator import OperatorRequest

    value = request()
    value["invocationContract"] = None
    with pytest.raises(ValueError, match="invocationContract"):
        OperatorRequest.from_dict(value)


def test_invocation_controls_tamper_is_rejected():
    import pytest

    from radstudio.execution_contract import (
        ExecutionContractError,
        freeze_invocation_contract,
        freeze_parameter_contract,
        validate_execution_contract,
    )
    from radstudio.operator import OperatorRequest

    value = request()
    value["operatorType"] = "DLCNNTraining"
    value["parameterContract"] = freeze_parameter_contract("DLCNNTraining", "1.0.0", {})
    value["executionControls"] = {"version": 1, "shardIndex": 0, "shardCount": 2}
    req = OperatorRequest.from_dict(value)
    req.invocationContract = freeze_invocation_contract(req)
    req.executionControls["shardIndex"] = 1
    with pytest.raises(ExecutionContractError, match="调用摘要"):
        validate_execution_contract(req)


def test_invocation_transfer_urls_are_not_identity():
    from radstudio.execution_contract import freeze_invocation_contract
    from radstudio.operator import OperatorRequest

    req = OperatorRequest.from_dict(request())
    first = freeze_invocation_contract(req)
    req.transfer = {"mode": "object", "inputManifestUrl": "https://expired.example"}
    assert freeze_invocation_contract(req) == first
    req.transfer["inputManifestUrl"] = "https://renewed.example"
    assert freeze_invocation_contract(req) == first
    req.environment = {}
    assert freeze_invocation_contract(req) == first


def test_invocation_json_budget_rejected():
    import pytest

    from radstudio.execution_contract import ExecutionContractError, freeze_invocation_contract
    from radstudio.operator import OperatorRequest

    req = OperatorRequest.from_dict(request())
    req.parameters = {"value": "x" * (512 << 10)}
    with pytest.raises(ExecutionContractError, match="JSON预算"):
        freeze_invocation_contract(req)


def test_shared_invocation_digest_vectors():
    from pathlib import Path

    from radstudio.execution_contract import freeze_invocation_contract
    from radstudio.json_contract import decode_runtime_json
    from radstudio.operator import OperatorRequest

    path = (
        Path(__file__).resolve().parents[2] / "operator-schema/tests/invocation-digest-cases.json"
    )
    for case in decode_runtime_json(path.read_bytes()):
        assert (
            freeze_invocation_contract(OperatorRequest.from_dict(case["request"]))
            == case["binding"]
        )


def test_invocation_helper_rejects_invalid_wire_fields():
    import pytest

    from radstudio.execution_contract import ExecutionContractError, freeze_invocation_contract
    from radstudio.operator import OperatorRequest

    for key, invalid in (
        ("requestId", ""),
        ("operatorVersion", None),
        ("inputs", {"a": 1}),
        ("environment", []),
        ("executionControls", False),
        ("parameters", []),
    ):
        req = OperatorRequest.from_dict(request())
        setattr(req, key, invalid)
        with pytest.raises(ExecutionContractError):
            freeze_invocation_contract(req)


def test_receipt_schema_discriminates_legacy_and_bound():
    from radstudio.execution_contract import _definitions
    from radstudio.parameter_validation import schema_value_issues

    schema = _definitions()["receipt"]
    for value, valid in (
        ({"version": 1, "parameterDigest": ""}, True),
        ({"version": 2, "parameterDigest": "a" * 64, "invocationDigest": "b" * 64}, True),
        ({"version": 2, "parameterDigest": "a" * 64}, False),
        ({"version": 1, "parameterDigest": "", "invocationDigest": "b" * 64}, False),
        ({"version": 3, "parameterDigest": ""}, False),
    ):
        assert bool(schema_value_issues(schema, value)) != valid


def test_invocation_large_environment_keeps_parameter_budget_separate():
    from radstudio.execution_contract import freeze_invocation_contract
    from radstudio.operator import OperatorRequest

    req = OperatorRequest.from_dict(request())
    req.environment = {"description": "x" * (600 << 10)}
    assert len(freeze_invocation_contract(req)["digest"]) == 64
