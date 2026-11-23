import copy
import json
from pathlib import Path

import pytest

from radstudio import runner
from radstudio.catalog_snapshot import capture_catalog_snapshot
from radstudio.json_contract import encode_contract_json
from radstudio.port_contract import PortContractError, port_contract_error


def cases():
    return json.loads(
        (
            Path(__file__).resolve().parents[2] / "operator-schema/tests/port-contract-cases.json"
        ).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("case", cases(), ids=lambda item: item["name"])
def test_shared_port_contract_cases(case):
    assert (port_contract_error(case["output"], case["input"]) is None) == case["valid"]


@pytest.mark.parametrize("matching", [True, False])
def test_frozen_graph_checks_semantic_dimension_before_output(
    tmp_path, matching, monkeypatch, capsys
):
    definitions = json.loads(runner.catalog_path().read_text(encoding="utf-8"))
    selected = [
        copy.deepcopy(next(d for d in definitions if d["type"] == kind and d["version"] == "1.0.0"))
        for kind in ("ReadImages", "QualityControl")
    ]
    contract = {"id": "radstudio/sample-manifest", "version": "1", "dimension": "2d"}
    selected[0]["outputs"][0]["contract"] = contract
    selected[1]["inputs"][0]["contract"] = {**contract, "dimension": "2d" if matching else "3d"}
    nodes = [
        {
            "id": nid,
            "type": definition["type"],
            "operatorVersion": "1.0.0",
            "parameters": definition["defaults"],
        }
        for nid, definition in zip(("source", "target"), selected)
    ]
    edge = {
        "source": "source",
        "target": "target",
        "sourceOutput": selected[0]["outputs"][0]["name"],
        "targetInput": selected[1]["inputs"][0]["name"],
    }
    ir = {"schemaVersion": "2", "nodes": nodes, "edges": [edge]}
    snapshot = capture_catalog_snapshot(ir, selected)
    if matching:
        assert runner.validate_graph(ir, catalog_snapshot=snapshot)[2] == ["source", "target"]
    else:
        with pytest.raises(PortContractError) as error:
            runner.run(
                {"runId": "bad-edge", "ir": ir, "operatorCatalogSnapshot": snapshot},
                tmp_path / "output",
                lambda *_: None,
            )
        assert error.value.code == "edge.type"
        assert error.value.details["edge"] == edge
        assert not (tmp_path / "output").exists()
        request_path = tmp_path / "request.json"
        request_path.write_bytes(
            encode_contract_json(
                {"runId": "bad-edge", "ir": ir, "operatorCatalogSnapshot": snapshot}
            )
        )
        monkeypatch.setattr(
            "sys.argv",
            ["runner", "--request", str(request_path), "--output", str(tmp_path / "cli-output")],
        )
        assert runner.main() == 1
        event = json.loads(capsys.readouterr().out)
        assert event["payload"]["errorCode"] == "edge.type"
        assert event["payload"]["details"]["edge"] == edge
        assert not (tmp_path / "cli-output").exists()


@pytest.mark.parametrize(
    "contract",
    [
        {},
        {"id": "x"},
        {"id": "x", "version": "1", "dimension": "4d"},
        {"id": "x", "version": "1", "task": False},
    ],
)
def test_invalid_contract_declaration_rejected_before_execution(contract):
    from radstudio.catalog_snapshot import CatalogSnapshotError

    definition = copy.deepcopy(
        next(
            d
            for d in json.loads(runner.catalog_path().read_text(encoding="utf-8"))
            if d["type"] == "ReadImages" and d["version"] == "1.0.0"
        )
    )
    definition["outputs"][0]["contract"] = contract
    ir = {
        "schemaVersion": "2",
        "nodes": [
            {
                "id": "source",
                "type": "ReadImages",
                "operatorVersion": "1.0.0",
                "parameters": definition["defaults"],
            }
        ],
        "edges": [],
    }
    with pytest.raises(CatalogSnapshotError) as error:
        capture_catalog_snapshot(ir, [definition])
    assert error.value.details["nodeId"] == "source"
    assert (
        error.value.details["fieldPath"]
        == "operatorCatalogSnapshot.nodes.source.definition.outputs.0.contract"
    )


@pytest.mark.parametrize(
    "case",
    json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "operator-schema/tests/port-contract-declaration-cases.json"
        ).read_text(encoding="utf-8")
    ),
    ids=lambda item: item["name"],
)
def test_shared_port_contract_declarations(case):
    from radstudio.port_contract import validate_port_contract

    if case["valid"]:
        validate_port_contract(case["contract"])
    else:
        with pytest.raises(ValueError):
            validate_port_contract(case["contract"])
