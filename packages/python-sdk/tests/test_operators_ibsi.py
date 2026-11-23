"""IBSI 参考值比对算子：容差判定、无容差与不可比对分支。"""

import pytest

from radstudio.operator import ArtifactStore, OperatorRequest, invoke


def _request(parameters, inputs, node_id="ibsi-comparison"):
    return OperatorRequest(
        requestId=f"req-{node_id}",
        runId="run-1",
        nodeId=node_id,
        operatorType="IBSIReferenceComparison",
        parameters=parameters,
        inputs=inputs,
        operatorVersion="1.0.0",
    )


def _reference(rows):
    return {
        "columns": [
            "code",
            "name",
            "family",
            "tableIndex",
            "dataset",
            "aggregation",
            "value",
            "tolerance",
            "consensus",
        ],
        "records": rows,
    }


def _mapping(rows):
    return {
        "columns": [
            "code",
            "name",
            "family",
            "tableIndex",
            "status",
            "pyradiomicsClass",
            "pyradiomicsFeature",
            "note",
        ],
        "records": rows,
    }


def _reference_row(code, value, tolerance, aggregation="2D", dataset="dig. phantom"):
    return {
        "code": code,
        "name": code,
        "family": "Grey level co-occurrence based features",
        "tableIndex": 1,
        "dataset": dataset,
        "aggregation": aggregation,
        "value": value,
        "tolerance": tolerance,
        "consensus": "strong",
    }


def _mapping_row(code, status="mapped", class_name="glcm", feature="MaximumProbability"):
    return {
        "code": code,
        "name": code,
        "family": "Grey level co-occurrence based features",
        "tableIndex": 1,
        "status": status,
        "pyradiomicsClass": class_name,
        "pyradiomicsFeature": feature,
        "note": "",
    }


def _run(tmp_path, parameters, feature_table, reference, mapping):
    import radstudio.operators_ibsi  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    inputs = {
        "features": store.write_json("features.json", feature_table, "feature_table"),
        "reference": store.write_json("reference.json", reference, "table"),
        "mapping": store.write_json("mapping.json", mapping, "table"),
    }
    return store, invoke(_request(parameters, inputs), store)


def test_ibsi_comparison_registered():
    import radstudio.operators_ibsi  # noqa: F401
    from radstudio.operator import resolve_operator

    cls = resolve_operator("IBSIReferenceComparison", "1.0.0")
    assert cls.operator_type == "IBSIReferenceComparison"


def test_pass_and_fail_use_published_tolerance(tmp_path):
    features = {"featureNames": ["original_glcm_MaximumProbability"], "records": [{"imageId": "p", "values": [0.52]}]}
    reference = _reference(
        [
            _reference_row("GYBY", 0.5, 0.03),
            _reference_row("ACUI", 0.63, 0.01),
        ]
    )
    mapping = _mapping(
        [
            _mapping_row("GYBY"),
            _mapping_row("ACUI", class_name="glcm", feature="Contrast"),
        ]
    )
    features["featureNames"].append("original_glcm_Contrast")
    features["records"][0]["values"].append(0.6)

    store, result = _run(tmp_path, {}, features, reference, mapping)
    assert result.status == "completed", result.error

    table = store.read_json(result.outputs["comparison"])
    conclusions = {row["ibsiCode"]: row["conclusion"] for row in table["records"]}
    assert conclusions == {"GYBY": "pass", "ACUI": "fail"}
    assert table["records"][0]["absoluteDeviation"] == pytest.approx(0.02)

    summary = store.read_json(result.outputs["summary"])
    assert summary["passRate"] == 0.5
    assert summary["passRateDenominator"] == 2
    assert summary["noSelfInventedThresholds"] is True


def test_no_tolerance_reports_deviation_without_counting(tmp_path):
    features = {"featureNames": ["original_glcm_MaximumProbability"], "records": [{"imageId": "p", "values": [0.52]}]}
    reference = _reference([_reference_row("GYBY", 0.5, None)])
    mapping = _mapping([_mapping_row("GYBY")])

    store, result = _run(tmp_path, {}, features, reference, mapping)
    assert result.status == "completed", result.error

    row = store.read_json(result.outputs["comparison"])["records"][0]
    assert row["conclusion"] == "no_tolerance"
    assert row["absoluteDeviation"] == pytest.approx(0.02)

    summary = store.read_json(result.outputs["summary"])
    assert summary["passRate"] is None
    assert summary["passRateDenominator"] == 0


def test_unmapped_and_missing_feature_are_separate_conclusions(tmp_path):
    features = {"featureNames": ["original_glcm_MaximumProbability"], "records": [{"imageId": "p", "values": [0.52]}]}
    reference = _reference(
        [
            _reference_row("0GBI", 1.0, 0.01),
            _reference_row("ZZZZ", 1.0, 0.01),
            _reference_row("GYBY", 0.5, 0.03),
        ]
    )
    mapping = _mapping(
        [
            _mapping_row("0GBI", status="missing", class_name="", feature=""),
            _mapping_row("GYBY"),
        ]
    )
    mapping["records"][0]["note"] = "家族缺失"

    store, result = _run(tmp_path, {}, features, reference, mapping)
    assert result.status == "completed", result.error

    rows = {row["ibsiCode"]: row for row in store.read_json(result.outputs["comparison"])["records"]}
    assert rows["0GBI"]["conclusion"] == "not_comparable"
    assert rows["ZZZZ"]["conclusion"] == "not_comparable"
    assert rows["GYBY"]["conclusion"] == "pass"

    report = store.read_json(result.outputs["report"])
    assert report["validated"] is False
    assert report["selfAttestation"] is True
    assert report["uncomparableFeatures"] == ["0GBI", "ZZZZ"]


def test_feature_missing_when_key_absent(tmp_path):
    features = {"featureNames": ["original_glcm_MaximumProbability"], "records": [{"imageId": "p", "values": [0.52]}]}
    reference = _reference([_reference_row("ACUI", 0.5, 0.03)])
    mapping = _mapping([_mapping_row("ACUI", class_name="glcm", feature="Contrast")])

    store, result = _run(tmp_path, {}, features, reference, mapping)
    assert result.status == "completed", result.error

    row = store.read_json(result.outputs["comparison"])["records"][0]
    assert row["conclusion"] == "feature_missing"
    assert row["pyradiomicsKey"] == "original_glcm_Contrast"
    assert store.read_json(result.outputs["report"])["missingFeatureKeys"] == ["ACUI"]


def test_mean_aggregates_per_slice_records(tmp_path):
    features = {
        "featureNames": ["original_glcm_MaximumProbability"],
        "records": [
            {"imageId": "p-0", "values": [0.4]},
            {"imageId": "p-1", "values": [0.6]},
        ],
    }
    reference = _reference([_reference_row("GYBY", 0.5, 0.01)])
    mapping = _mapping([_mapping_row("GYBY")])

    store, result = _run(tmp_path, {"aggregation": "2D"}, features, reference, mapping)
    assert result.status == "completed", result.error

    row = store.read_json(result.outputs["comparison"])["records"][0]
    assert row["computedValue"] == pytest.approx(0.5)
    assert row["conclusion"] == "pass"
    assert store.read_json(result.outputs["summary"])["featureRecords"] == 2


@pytest.mark.parametrize("aggregation", ["2D, averaged", "2D, slice-merged"])
def test_mean_rejected_for_matrix_level_aggregation(tmp_path, aggregation):
    features = {"featureNames": ["original_glcm_MaximumProbability"], "records": [{"imageId": "p", "values": [0.5]}]}
    reference = _reference([_reference_row("GYBY", 0.5, 0.03, aggregation=aggregation)])
    mapping = _mapping([_mapping_row("GYBY")])

    _, result = _run(tmp_path, {"aggregation": aggregation}, features, reference, mapping)
    assert result.status == "failed"
    assert "矩阵级聚合" in str(result.error)


def test_single_rejects_multiple_records(tmp_path):
    features = {
        "featureNames": ["original_glcm_MaximumProbability"],
        "records": [
            {"imageId": "p-0", "values": [0.4]},
            {"imageId": "p-1", "values": [0.6]},
        ],
    }
    reference = _reference([_reference_row("GYBY", 0.5, 0.03)])
    mapping = _mapping([_mapping_row("GYBY")])

    _, result = _run(tmp_path, {"recordAggregation": "single_record"}, features, reference, mapping)
    assert result.status == "failed"
    assert "恰好 1 条记录" in str(result.error)


def test_missing_dataset_combination_lists_available(tmp_path):
    features = {"featureNames": ["original_glcm_MaximumProbability"], "records": [{"imageId": "p", "values": [0.5]}]}
    reference = _reference([_reference_row("GYBY", 0.5, 0.03, dataset="config. A", aggregation="config. A (2D)")])
    mapping = _mapping([_mapping_row("GYBY")])

    _, result = _run(tmp_path, {}, features, reference, mapping)
    assert result.status == "failed"
    assert "config. A (2D)" in str(result.error)
