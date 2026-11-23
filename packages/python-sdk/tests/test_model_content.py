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
        Path(__file__).resolve().parents[2] / "operator-schema/tests/model-lineage-cases.json"
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


def scene_forms(case):
    """契约版本决定默认场景：@2 走折内清单 + 交叉验证划分，@1 走全集清单 + holdout。"""
    contract = str(case.get("contract", 1))
    scene = case.get("scene") or ("fold" if contract == "2" else "union")
    plan = case.get("plan") or ("cv" if contract == "2" else "holdout")
    return contract, scene, plan


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build_scene(root, case):
    _, scene, plan = scene_forms(case)
    break_name = case["break"]
    table = copy.deepcopy(FIXTURE["table"])
    split = copy.deepcopy(FIXTURE["cvSplit"] if plan == "cv" else FIXTURE["split"])
    model = copy.deepcopy(FIXTURE["cvModel"] if scene == "fold" else FIXTURE["model"])
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

    # 折权重字节 = 同一份种子字节 + 折序号，所以逐折摘要天然互不相同；
    # 重复摘要只能是被伪造出来的破坏，而不是场景本身的重叠。
    fold_files = {}
    for position, entry in enumerate(model["fit"].get("folds", [])):
        data = (
            weights
            if break_name == "fold_weights_identical_bytes"
            else weights + str(position).encode()
        )
        fold_files[entry["weights"]["path"]] = data
        entry["weights"]["sha256"] = digest(data)
        entry["weights"]["sizeBytes"] = len(data)

    if break_name in (
        "",
        "split_alias_path",
        "fold_weights_identical_bytes",
        "fold_weights_tampered",
    ):
        pass
    elif break_name == "feature_order":
        model["featureNames"] = ["b", "a"]
    elif break_name == "unknown_trained":
        model["fit"]["trainedImageIds"] = ["i0", "i1", "i9"]
    elif break_name == "count_mismatch":
        model["fit"]["trainedCount"] = 2
    elif break_name == "holdout_leakage":
        model["fit"]["trainedImageIds"] = ["i0", "i1", "i2", "i3"]
        model["fit"]["trainedCount"] = 4
    elif break_name == "features_hash_stale":
        model["fit"]["features"] = reference_with_digest("features.json", "0" * 64, "feature_table")
    elif break_name == "weights_tampered":
        weights = b"tampered-weight-bytes"
    elif break_name == "weights_size":
        model["weights"]["sizeBytes"] = len(weights) + 1
    elif break_name == "weights_over_budget":
        model["weights"]["sizeBytes"] = 99999999999
    elif break_name == "weights_self_reference":
        model["weights"]["path"] = "model.json"
    elif break_name == "weights_escape":
        model["weights"]["path"] = "../outside/weights.bin"
    elif break_name == "features_escape":
        model["fit"]["features"] = reference_with_digest(
            "../outside/features.json", digest(table_data), "feature_table"
        )
    elif break_name == "fold_train_union":
        folds = model["fit"]["folds"]
        folds[0]["trainedImageIds"] = ["i0", "i1", "i2", "i3"]
        folds[0]["trainedCount"] = 4
    elif break_name == "fold_test_sample_fitted":
        model["fit"]["folds"][0]["trainedImageIds"] = ["i2", "i3"]
    elif break_name == "fold_trained_sample_unknown":
        model["fit"]["folds"][0]["trainedImageIds"] = ["i0", "i9"]
    elif break_name == "fold_count_mismatch":
        model["fit"]["folds"][0]["trainedCount"] = 3
    elif break_name == "fold_set_missing":
        del model["fit"]["folds"][1]
    elif break_name == "fold_set_extra":
        extra = weights + b"2"
        model["fit"]["folds"].append(
            {
                "fold": 2,
                "trainedImageIds": ["i0", "i1"],
                "trainedCount": 2,
                "weights": {
                    "path": "weights.fold2.bin",
                    "sha256": digest(extra),
                    "sizeBytes": len(extra),
                },
            }
        )
    elif break_name == "fold_duplicate":
        model["fit"]["folds"][1]["fold"] = 0
    elif break_name == "fold_weights_shared_path":
        model["fit"]["folds"][1]["weights"]["path"] = "weights.fold0.bin"
    elif break_name == "fold_weights_point_at_ensemble":
        model["fit"]["folds"][0]["weights"]["path"] = "weights.bin"
    elif break_name == "fold_weights_escape":
        model["fit"]["folds"][1]["weights"]["path"] = "../outside/weights.bin"
    elif break_name != "features_missing":
        raise AssertionError("unknown break " + break_name)

    (root / "weights.bin").write_bytes(weights)
    for name, data in fold_files.items():
        (root / name).write_bytes(data)
    if break_name == "fold_weights_tampered":
        (root / "weights.fold1.bin").write_bytes(b"tampered-fold-weights")
    model_data = write_scene(root, "model.json", model)
    if break_name == "features_missing":
        (root / "features.json").unlink()
    return reference("model.json", model_data, "model")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[case["name"] for case in FIXTURE["cases"]])
def test_shared_model_lineage_cases(tmp_path, case):
    build_scene(tmp_path, case)
    contract, _, _ = scene_forms(case)
    declared = {"id": "radstudio/model", "version": contract}
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
    assert evidence["contract"] == "radstudio/model@" + contract
    if contract == "2":
        assert evidence["records"] == sum(
            entry["trainedCount"] for entry in FIXTURE["cvModel"]["fit"]["folds"]
        )
    else:
        assert evidence["records"] == len(FIXTURE["model"]["fit"]["trainedImageIds"])


def test_model_lineage_only_applies_to_declared_contracts(tmp_path):
    ref = build_scene(tmp_path, {"break": "features_missing"})
    validate_inputs(
        {"model": ref}, tmp_path, ports=[{"name": "model", "type": "model"}], verify_content=True
    )
    with pytest.raises(ContentContractError) as caught:
        validate_content_file(
            tmp_path / "model.json", "model", MODEL_CONTRACT, reference_roots=[tmp_path]
        )
    assert caught.value.details["fieldPath"] == "/fit/features"
    ports = [{"name": "model", "type": "model", "contract": MODEL_CONTRACT}]
    with pytest.raises(ValueError) as boundary:
        validate_inputs({"model": ref}, tmp_path, ports=ports, verify_content=True)
    assert getattr(boundary.value, "code", "") == "operator.input_invalid"
    assert boundary.value.details["fieldPath"] == "/fit/features"
