import copy

import pytest

from radstudio import operator as framework
from radstudio.execution_contract import (
    freeze_parameter_contract,
    invocation_artifact_ports,
    validate_execution_contract,
)
from radstudio.operator import ArtifactStore, Operator, OperatorRequest, invoke, operator

FEATURES = {
    "featureNames": ["x"],
    "records": [{"imageId": "i", "patientId": "p", "label": 1, "values": [0.5]}],
}
PORT = {
    "name": "features",
    "type": "feature_table",
    "required": True,
    "contract": {"id": "radstudio/feature-table", "version": "1"},
}


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    from radstudio import runner

    runner._load_builtin_operators()
    monkeypatch.setattr(framework, "OPERATOR_REGISTRY", dict(framework.OPERATOR_REGISTRY))


def envelope(kind="ArtifactContractProbe", inputs=None, outputs=None):
    return freeze_parameter_contract(
        kind,
        "1.0.0",
        {},
        {"version": 1, "contentContracts": 1, "inputs": inputs, "outputs": outputs},
    )


@pytest.mark.parametrize(
    "mode", ["valid", "bad-input", "bad-output", "tampered-ports", "duplicate-ports"]
)
def test_invoke_validates_frozen_ports_and_content_before_publication(tmp_path, mode):
    called = []

    @operator("ArtifactContractProbe", version="1.0.0")
    class Probe(Operator):
        def run(self, ctx):
            called.append(True)
            value = copy.deepcopy(ctx.input_json("features"))
            if mode == "bad-output":
                value["records"][0]["values"] = []
            return {"features": ctx.output_json("output.json", value, "feature_table")}

    value = copy.deepcopy(FEATURES)
    if mode == "bad-input":
        value["records"][0]["values"] = []
    store = ArtifactStore(tmp_path)
    ref = store.write_json("input.json", value, "feature_table")
    ports = [copy.deepcopy(PORT)]
    if mode == "duplicate-ports":
        ports *= 2
    contract = envelope(inputs=ports, outputs=[PORT])
    if mode == "tampered-ports":
        contract["artifactPorts"]["outputs"][0]["type"] = "report"
    request = {
        "requestId": "q",
        "runId": "r",
        "nodeId": "n",
        "operatorType": "ArtifactContractProbe",
        "operatorVersion": "1.0.0",
        "parameters": {},
        "inputs": {"features": ref},
        "parameterContract": contract,
    }
    events = []
    result = invoke(request, store, lambda kind, payload: events.append(kind)).to_dict()
    if mode == "valid":
        assert result["status"] == "completed"
        assert result["executionContract"]["parameterDigest"] == contract["digest"]
        assert events.count("artifact.created") == 1
    else:
        assert result["status"] == "failed"
        assert "artifact.created" not in events
        assert "node.completed" not in events
        assert result["error"]["code"] == (
            "operator.input_invalid"
            if mode == "bad-input"
            else "operator.output_invalid"
            if mode == "bad-output"
            else "operator.contract_invalid"
        )
        if mode != "bad-output":
            assert not called


@pytest.mark.parametrize("kind", ["IBSI2DFeatureExtraction", "DLCNNTraining", "DLCNNInference"])
def test_merge_ports_are_derived_only_from_frozen_outputs(kind):
    predictions = {
        "name": "predictions",
        "type": "prediction_table",
        "contract": {"id": "radstudio/prediction-table", "version": "1"},
    }
    model = {"name": "model", "type": "model"}
    inputs = [{"name": "samples", "type": "sample_collection", "required": True}]
    outputs = (
        [PORT]
        if kind == "IBSI2DFeatureExtraction"
        else [model, predictions]
        if kind == "DLCNNTraining"
        else [predictions]
    )
    contract = envelope(kind, inputs, outputs)
    request = OperatorRequest(
        "q",
        "r",
        "n",
        kind,
        {},
        inputs={},
        operatorVersion="1.0.0",
        parameterContract=contract,
        executionControls={"version": 1, "mergeParts": 2, "shardCount": 2},
    )
    validate_execution_contract(request)
    incoming, outgoing, content = invocation_artifact_ports(request)
    assert outgoing == outputs
    assert content
    expected = (
        {"part0", "part1"}
        if kind == "IBSI2DFeatureExtraction"
        else {"samples", "part0Model", "part1Model", "part0Pred", "part1Pred"}
        if kind == "DLCNNTraining"
        else {"part0Pred", "part1Pred"}
    )
    assert {port["name"] for port in incoming} == expected
    assert (
        contract["artifactPorts"]["inputs"] == inputs
    )  # Per-call derivation never mutates frozen declarations.


@pytest.mark.parametrize("duplicate", [False, True])
def test_real_feature_merge_validates_derived_input_and_final_content(tmp_path, duplicate):
    import json

    from radstudio import operators_builtin, runner

    operator("IBSI2DFeatureExtraction", version="1.0.0")(
        operators_builtin.IBSI2DFeatureExtractionOperator
    )
    definition = next(
        d
        for d in json.loads(runner.catalog_path().read_text(encoding="utf-8"))
        if d["type"] == "IBSI2DFeatureExtraction" and d["version"] == "1.0.0"
    )
    store = ArtifactStore(tmp_path)
    first = copy.deepcopy(FEATURES)
    second = copy.deepcopy(FEATURES)
    if not duplicate:
        second["records"][0]["imageId"] = "i2"
        second["records"][0]["patientId"] = "p2"
    refs = {
        "part0": store.write_json("part0.json", first, "feature_table"),
        "part1": store.write_json("part1.json", second, "feature_table"),
    }
    outputs = [PORT, {"name": "report", "type": "report"}]
    contract = freeze_parameter_contract(
        "IBSI2DFeatureExtraction",
        "1.0.0",
        definition["parametersSchema"],
        {"version": 1, "contentContracts": 1, "inputs": definition["inputs"], "outputs": outputs},
    )
    result = invoke(
        {
            "requestId": "q",
            "runId": "r",
            "nodeId": "merge",
            "operatorType": "IBSI2DFeatureExtraction",
            "operatorVersion": "1.0.0",
            "parameters": definition["defaults"],
            "inputs": refs,
            "parameterContract": contract,
            "executionControls": {"version": 1, "mergeParts": 2, "shardCount": 2},
        },
        store,
    ).to_dict()
    if duplicate:
        assert result["status"] == "failed"
        assert result["error"]["code"] == "operator.output_invalid"
        assert not result["artifacts"]
    else:
        assert result["status"] == "completed", result
        assert len(store.read_json(result["outputs"]["features"])["records"]) == 2
        assert result["executionContract"]["parameterDigest"] == contract["digest"]


def test_serve_rejects_unsupported_port_contract_before_input_materialization(monkeypatch):
    from radstudio.operator import BundleArtifactStore
    from radstudio.operator_serve import execute_payload

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid frozen port contract reached input materialization")

    monkeypatch.setattr(BundleArtifactStore, "import_bundle", forbidden)
    unsupported = {**PORT, "contract": {"id": "org/unknown", "version": "1"}}
    request = {
        "requestId": "q",
        "runId": "r",
        "nodeId": "n",
        "operatorType": "ArtifactContractProbe",
        "operatorVersion": "1.0.0",
        "parameters": {},
        "inputs": {},
        "parameterContract": envelope(inputs=[], outputs=[unsupported]),
        "transfer": {"protocolVersion": 2, "mode": "inline", "mountInputs": []},
    }
    result = execute_payload(
        {"request": request, "inputBundle": "invalid base64 that must never be parsed"}
    ).to_dict()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "operator.contract_invalid"
    assert not result["artifacts"]


@pytest.mark.parametrize("above", [True, False])
def test_once_validates_exact_threshold_before_native_scientific_context(tmp_path, above):
    import json
    from decimal import Decimal

    from radstudio.json_contract import encode_contract_json
    from radstudio.operator_serve import run_once

    @operator("PrecisionArtifactProbe", version="1.0.0")
    class PrecisionProbe(Operator):
        def run(self, ctx):
            assert isinstance(ctx.parameters["threshold"], Decimal)
            assert ctx.parameters["threshold"] == ctx.request.parameters["threshold"]
            assert isinstance(ctx.request.parameters["threshold"], Decimal)
            return {
                "out": ctx.output_json(
                    "result.json", {"value": ctx.parameters["threshold"]}, "report"
                )
            }

    schema = {
        "type": "object",
        "properties": {
            "threshold": {"type": "number", "minimum": Decimal("0.12345678901234567890123456789")}
        },
        "required": ["threshold"],
        "additionalProperties": False,
    }
    ports = {
        "version": 1,
        "contentContracts": 1,
        "inputs": [],
        "outputs": [{"name": "out", "type": "report", "required": True}],
    }
    contract = freeze_parameter_contract("PrecisionArtifactProbe", "1.0.0", schema, ports)
    value = Decimal(
        "0.12345678901234567890123456790" if above else "0.12345678901234567890123456788"
    )
    request = {
        "requestId": "q",
        "runId": "r",
        "nodeId": "n",
        "operatorType": "PrecisionArtifactProbe",
        "operatorVersion": "1.0.0",
        "parameters": {"threshold": value},
        "inputs": {},
        "parameterContract": contract,
    }
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "result.json"
    input_path.write_bytes(encode_contract_json({"request": request}))
    assert run_once(str(input_path), str(output_path)) == (0 if above else 1)
    result = json.loads(output_path.read_text(encoding="utf-8"))["result"]
    if above:
        assert result["executionContract"]["parameterDigest"] == contract["digest"]
    else:
        assert result["error"]["code"] == "operator.parameters_invalid"
        assert not result["artifacts"]


def test_transport_rejects_duplicate_json_keys():
    from radstudio.operator_serve import load_operator_payload

    with pytest.raises(ValueError):
        load_operator_payload(b'{"request":{},"request":{}}')


@pytest.mark.parametrize("above", [True, False])
def test_runner_transport_validates_exact_parameters_before_native_context(tmp_path, above):
    import json
    from decimal import Decimal

    from radstudio import runner
    from radstudio.catalog_snapshot import capture_catalog_snapshot, load_runner_request
    from radstudio.json_contract import encode_contract_json

    @operator("PrecisionRunnerProbe", version="1.0.0")
    class Probe(Operator):
        def run(self, ctx):
            assert isinstance(ctx.parameters["threshold"], Decimal)
            assert ctx.parameters["threshold"] == ctx.request.parameters["threshold"]
            assert isinstance(ctx.request.parameters["threshold"], Decimal)
            return {
                "out": ctx.output_json("out.json", {"value": ctx.parameters["threshold"]}, "report")
            }

    definition = copy.deepcopy(
        next(
            d
            for d in json.loads(runner.catalog_path().read_text(encoding="utf-8"))
            if d["type"] == "ReadImages" and d["version"] == "1.0.0"
        )
    )
    definition["type"] = "PrecisionRunnerProbe"
    definition["parametersSchema"] = {
        "type": "object",
        "properties": {
            "threshold": {"type": "number", "minimum": Decimal("0.12345678901234567890123456789")}
        },
        "required": ["threshold"],
        "additionalProperties": False,
    }
    definition["defaults"] = {}
    definition["inputs"] = []
    definition["outputs"] = [{"name": "out", "type": "report", "required": True}]
    value = Decimal(
        "0.12345678901234567890123456790" if above else "0.12345678901234567890123456788"
    )
    ir = {
        "schemaVersion": "2",
        "nodes": [
            {
                "id": "n",
                "type": definition["type"],
                "operatorVersion": "1.0.0",
                "parameters": {"threshold": value},
            }
        ],
        "edges": [],
    }
    request = load_runner_request(
        encode_contract_json(
            {
                "runId": "exact-runner",
                "ir": ir,
                "operatorCatalogSnapshot": capture_catalog_snapshot(ir, [definition]),
            }
        )
    )
    if above:
        assert "n" in runner.run(request, tmp_path / "output", lambda *_: None)
    else:
        with pytest.raises(ValueError):
            runner.run(request, tmp_path / "output", lambda *_: None)
        assert not (tmp_path / "output").exists()


def test_derived_merge_ports_cannot_shadow_frozen_input():
    from radstudio.execution_contract import ExecutionContractError

    outputs = [
        {"name": "model", "type": "model"},
        {"name": "predictions", "type": "prediction_table"},
    ]
    contract = envelope("DLCNNTraining", [{"name": "part0Model", "type": "report"}], outputs)
    request = OperatorRequest(
        "q",
        "r",
        "n",
        "DLCNNTraining",
        {},
        inputs={},
        operatorVersion="1.0.0",
        parameterContract=contract,
        executionControls={"version": 1, "mergeParts": 2, "shardCount": 2},
    )
    validate_execution_contract(request)
    with pytest.raises(ExecutionContractError):
        invocation_artifact_ports(request)


@pytest.mark.parametrize("schema", [False, [], 0, ""])
def test_freezing_does_not_erase_invalid_falsy_parameter_schema(schema):
    from radstudio.execution_contract import ExecutionContractError

    contract = freeze_parameter_contract("Probe", "1.0.0", schema)
    request = OperatorRequest(
        "q", "r", "n", "Probe", {}, operatorVersion="1.0.0", parameterContract=contract
    )
    with pytest.raises(ExecutionContractError):
        validate_execution_contract(request)


def test_frozen_physical_type_is_checked_before_materialization():
    from radstudio.execution_contract import ExecutionContractError

    contract = envelope(outputs=[{"name": "out", "type": "bad type"}])
    request = OperatorRequest(
        "q",
        "r",
        "n",
        "ArtifactContractProbe",
        {},
        operatorVersion="1.0.0",
        parameterContract=contract,
    )
    validate_execution_contract(request)
    with pytest.raises(ExecutionContractError) as error:
        invocation_artifact_ports(request)
    assert error.value.details["issues"][0]["fieldPath"] == "/artifactPorts/outputs/0/type"
