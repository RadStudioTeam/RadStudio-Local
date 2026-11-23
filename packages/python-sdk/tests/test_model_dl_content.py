import copy
import hashlib
import json
from pathlib import Path

import pytest

from radstudio.artifact_boundary import validate_inputs
from radstudio.content_contract import ContentContractError, source_identity, validate_content_file
from radstudio.json_contract import canonical_manifest_json

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[2] / "operator-schema/tests/dl-model-lineage-cases.json"
    ).read_text(encoding="utf-8")
)
MODEL_CONTRACT = {"id": "radstudio/model", "version": "1"}


def reference(name, data, kind):
    return reference_with_digest(name, hashlib.sha256(data).hexdigest(), kind)


def reference_with_digest(name, digest, kind):
    return "file:" + name + "#sha256=" + digest + ";type=" + kind


def write_scene(root, name, value):
    data = canonical_manifest_json(value)
    (root / name).write_bytes(data)
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build_scene(root, case):
    break_name = case["break"]
    table = copy.deepcopy(FIXTURE["table"])
    split = copy.deepcopy(
        FIXTURE["holdoutSplit"] if case.get("plan") == "holdout" else FIXTURE["split"]
    )
    model = copy.deepcopy(FIXTURE["model"])
    weights = FIXTURE["weights"].encode("utf-8")

    table_data = write_scene(root, "features.json", table)
    features = reference("features.json", table_data, "feature_table")
    if break_name == "split_alias_path":
        (root / "other.json").write_bytes(table_data)
        split["source"] = reference("other.json", table_data, "feature_table")
    else:
        split["source"] = features
    split["identityHash"] = source_identity(table)
    split_data = write_scene(root, "split.json", split)

    model["fit"]["features"] = features
    model["fit"]["split"] = reference("split.json", split_data, "split_plan")
    model["weights"]["sha256"] = digest(weights)
    model["weights"]["sizeBytes"] = len(weights)

    if break_name in ("", "split_alias_path", "features_missing"):
        pass
    elif break_name == "dimension_tabular":
        model["dimension"] = "tabular"
    elif break_name == "union_form":
        del model["fit"]["folds"]
        model["fit"]["trainedImageIds"] = ["i0", "i1", "i2"]
        model["fit"]["trainedCount"] = 3
    elif break_name == "features_hash_stale":
        model["fit"]["features"] = reference_with_digest("features.json", "0" * 64, "feature_table")
    elif break_name == "fold_set_missing":
        del model["fit"]["folds"][1]
    elif break_name == "fold_set_extra":
        model["fit"]["folds"].append(
            {
                "fold": 2,
                "trainedImageIds": ["i0", "i1"],
                "trainedCount": 2,
                "testCount": 2,
            }
        )
    elif break_name == "fold_duplicate":
        model["fit"]["folds"][1]["fold"] = 0
    elif break_name == "fold_train_union":
        model["fit"]["folds"][0]["trainedImageIds"] = ["i0", "i1", "i2", "i3"]
        model["fit"]["folds"][0]["trainedCount"] = 4
    elif break_name == "fold_test_sample_fitted":
        model["fit"]["folds"][0]["trainedImageIds"] = ["i2", "i3"]
    elif break_name == "fold_trained_unknown":
        model["fit"]["folds"][0]["trainedImageIds"] = ["i0", "i9"]
    elif break_name == "fold_count_mismatch":
        model["fit"]["folds"][0]["trainedCount"] = 3
    elif break_name == "fold_test_count_mismatch":
        model["fit"]["folds"][0]["testCount"] = 3
    elif break_name == "weights_tampered":
        weights = b"tampered-checkpoint-bytes"
    elif break_name == "weights_size":
        model["weights"]["sizeBytes"] = len(weights) + 1
    elif break_name == "weights_self_reference":
        model["weights"]["path"] = "model.json"
    elif break_name == "weights_escape":
        model["weights"]["path"] = "../outside/checkpoint.pt"
    elif break_name == "features_escape":
        model["fit"]["features"] = reference_with_digest(
            "../outside/features.json", digest(table_data), "feature_table"
        )
    else:
        raise AssertionError("unknown break " + break_name)

    (root / "checkpoint.pt").write_bytes(weights)
    model_data = write_scene(root, "model.json", model)
    if break_name == "features_missing":
        (root / "features.json").unlink()
    return reference("model.json", model_data, "model")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[case["name"] for case in FIXTURE["cases"]])
def test_shared_dl_model_lineage_cases(tmp_path, case):
    build_scene(tmp_path, case)
    declared = {"id": "radstudio/model", "version": "3"}
    for fixture_key, contract_key in (
        ("modelTask", "task"),
        ("modelDimension", "dimension"),
        ("modelFamily", "modelFamily"),
    ):
        if case.get(fixture_key):
            declared[contract_key] = case[fixture_key]
    error = None
    evidence = None
    try:
        evidence = validate_content_file(
            tmp_path / "model.json", "model", declared, reference_roots=[tmp_path]
        )
    except ContentContractError as caught:
        error = caught
    if not case["valid"]:
        assert error is not None, "forged lineage accepted: " + case["name"]
        assert error.details["fieldPath"] == case["fieldPath"]
        return
    assert error is None, "valid lineage refused: " + str(error)
    assert evidence["contract"] == "radstudio/model@3"
    assert evidence["records"] == sum(
        entry["trainedCount"] for entry in FIXTURE["model"]["fit"]["folds"]
    )


def test_dl_model_lineage_only_applies_to_declared_contracts(tmp_path):
    ref = build_scene(tmp_path, {"break": "features_missing"})
    validate_inputs(
        {"model": ref}, tmp_path, ports=[{"name": "model", "type": "model"}], verify_content=True
    )
    with pytest.raises(ContentContractError) as caught:
        validate_content_file(
            tmp_path / "model.json", "model", MODEL_CONTRACT, reference_roots=[tmp_path]
        )
    assert caught.value.details["contract"] == "radstudio/model@1"
    ports = [{"name": "model", "type": "model", "contract": MODEL_CONTRACT}]
    with pytest.raises(ValueError) as boundary:
        validate_inputs({"model": ref}, tmp_path, ports=ports, verify_content=True)
    assert getattr(boundary.value, "code", "") == "operator.input_invalid"
