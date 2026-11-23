import copy
import json

import numpy as np
import pytest

from radstudio import operator as framework
from radstudio import runner
from radstudio.catalog_snapshot import capture_catalog_snapshot
from radstudio.content_contract import source_identity, validate_content_file
from radstudio.operator import ArtifactStore, Operator, invoke, operator


def table():
    rng = np.random.default_rng(42)
    return {
        "featureNames": ["signal", "noise"],
        "records": [
            {
                "imageId": f"i{i}",
                "patientId": f"p{i // 2}",
                "label": (i // 2) % 2,
                "values": [float((i // 2) % 2 + rng.normal(scale=0.1)), float(rng.normal())],
            }
            for i in range(40)
        ],
    }


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    runner._load_builtin_operators()
    monkeypatch.setattr(framework, "OPERATOR_REGISTRY", dict(framework.OPERATOR_REGISTRY))


@pytest.mark.parametrize(
    "kind,zero_validation", [("DataSplit", False), ("HoldoutSplit", False), ("HoldoutSplit", True)]
)
def test_real_adapter_and_source_bound_split_workflow(tmp_path, kind, zero_validation):
    @operator("SyntheticFeatures", version="1.0.0")
    class Source(Operator):
        def run(self, ctx):
            return {"features": ctx.output_json("features.json", table(), "feature_table")}

    definitions = json.loads(runner.catalog_path().read_text(encoding="utf-8"))
    chosen = [
        copy.deepcopy(next(d for d in definitions if d["type"] == name and d["version"] == version))
        for name, version in [("ValidateFeatureTable", "1.0.0"), (kind, "1.2.0")]
    ]
    source = copy.deepcopy(chosen[0])
    source.update(
        type="SyntheticFeatures", inputs=[], outputs=[{"name": "features", "type": "feature_table"}]
    )
    chosen = [source, *chosen]
    nodes = [
        {"id": nid, "type": d["type"], "operatorVersion": d["version"], "parameters": d["defaults"]}
        for nid, d in zip(("source", "validate", "split"), chosen)
    ]
    if zero_validation:
        nodes[-1]["parameters"]["validationRatio"] = 0
    edges = [
        {
            "source": "source",
            "target": "validate",
            "sourceOutput": "features",
            "targetInput": "features",
        },
        {
            "source": "validate",
            "target": "split",
            "sourceOutput": "features",
            "targetInput": "features",
        },
    ]
    ir = {"schemaVersion": "2", "nodes": nodes, "edges": edges}
    events = []
    root = tmp_path / "out"
    result = runner.run(
        {
            "runId": "source-bound",
            "ir": ir,
            "operatorCatalogSnapshot": capture_catalog_snapshot(ir, chosen),
        },
        root,
        lambda kind, payload: events.append(kind),
    )
    assert result["source"]["features"] == result["validate"]["features"]
    assert events.count("node.completed") == 3
    split_path = root / result["split"]["split"]["path"]
    plan = json.loads(split_path.read_text(encoding="utf-8"))
    assert plan["identityHash"] == source_identity(table())
    assert plan["source"].startswith("file:source/features.json#sha256=")
    validate_content_file(
        split_path,
        "split_plan",
        {"id": "radstudio/split-plan", "version": "1"},
        reference_roots=[root],
    )
    if kind == "HoldoutSplit":
        assert {"train", "validation", "test"}.issubset(result["split"])
        if zero_validation:
            validation_path = root / result["split"]["validation"]["path"]
            assert json.loads(validation_path.read_text(encoding="utf-8"))["records"] == []


def test_explicit_adapter_rejects_invalid_legacy_table(tmp_path):
    store = ArtifactStore(tmp_path)
    value = table()
    value["records"][0]["values"] = []
    reference = store.write_json("features.json", value, "feature_table")
    result = invoke(
        {
            "requestId": "q",
            "runId": "r",
            "nodeId": "n",
            "operatorType": "ValidateFeatureTable",
            "operatorVersion": "1.0.0",
            "parameters": {},
            "inputs": {"features": reference},
        },
        store,
    ).to_dict()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "operator.input_invalid"
    assert result["error"]["details"]["fieldPath"] == "/records/0/values"
    assert not result["artifacts"]


def test_split_source_keeps_scientific_numeric_policy(tmp_path):
    from decimal import Decimal

    from radstudio.json_contract import encode_runtime_json

    value = table()
    value["records"][0]["values"][0] = Decimal("1e-200")
    store = ArtifactStore(tmp_path)
    source = store.write_bytes("features.json", encode_runtime_json(value), "feature_table")
    split = runner.make_split(value)
    split.update(source=source, identityHash=source_identity(value))
    store.write_json("split.json", split, "split_plan")
    evidence = validate_content_file(
        tmp_path / "split.json", "split_plan", {"id": "radstudio/split-plan", "version": "1"}
    )
    assert evidence["records"] == 40


def test_split_source_non_file_has_structured_location(tmp_path):
    from pathlib import Path

    from radstudio.content_contract import ContentContractError

    cases = json.loads(
        (
            Path(__file__).resolve().parents[2] / "operator-schema/tests/split-source-cases.json"
        ).read_text(encoding="utf-8")
    )
    (tmp_path / "features.json").mkdir()
    path = tmp_path / "split.json"
    path.write_text(json.dumps(cases[0]["plan"]), encoding="utf-8")
    with pytest.raises(ContentContractError) as error:
        validate_content_file(path, "split_plan", {"id": "radstudio/split-plan", "version": "1"})
    assert error.value.details["fieldPath"] == "/source"


@pytest.mark.parametrize("kind", ["DataSplit", "HoldoutSplit"])
def test_source_bound_split_membership_does_not_require_complete_features(tmp_path, kind):
    value = table()
    value["records"][0]["values"][0] = None
    store = ArtifactStore(tmp_path)
    reference = store.write_json("features.json", value, "feature_table")
    definition = next(
        d
        for d in json.loads(runner.catalog_path().read_text(encoding="utf-8"))
        if d["type"] == kind and d["version"] == "1.2.0"
    )
    result = invoke(
        {
            "requestId": "q",
            "runId": "r",
            "nodeId": "split",
            "operatorType": kind,
            "operatorVersion": "1.2.0",
            "parameters": definition["defaults"],
            "inputs": {"features": reference},
        },
        store,
    )
    assert result.status == "completed", result.error
    validate_content_file(
        tmp_path / "split.json", "split_plan", {"id": "radstudio/split-plan", "version": "1"}
    )
    assert store.read_json(reference)["records"][0]["values"][0] is None
    if kind == "HoldoutSplit":
        rows = [
            row
            for name in ("train", "validation", "test")
            for row in store.read_json(result.outputs[name])["records"]
        ]
        assert next(row for row in rows if row["imageId"] == "i0")["values"][0] is None
