"""基准对比算子：温度校准、双模型融合与配对基准汇总。"""

import numpy as np
import pytest

from radstudio.operator import ArtifactStore, OperatorRequest, invoke


def _records(labels, probabilities, prefix="i"):
    return [
        {
            "imageId": f"{prefix}{index}",
            "patientId": f"p{index}",
            "label": int(label),
            "probability": float(probability),
            "fold": None,
        }
        for index, (label, probability) in enumerate(zip(labels, probabilities))
    ]


def _table(records, evaluation="external"):
    return {"evaluation": evaluation, "records": records}


def _request(node_id, operator_type, parameters, inputs, version="1.0.0"):
    return OperatorRequest(
        requestId=f"req-{node_id}",
        runId="run-1",
        nodeId=node_id,
        operatorType=operator_type,
        parameters=parameters,
        inputs=inputs,
        operatorVersion=version,
    )


def test_bench_operators_registered():
    import radstudio.operators_bench  # noqa: F401
    from radstudio.operator import resolve_operator

    for operator_type in ("TemperatureScaling", "EnsembleFusion", "BenchmarkSummary"):
        cls = resolve_operator(operator_type, "1.0.0")
        assert cls.operator_type == operator_type
        assert cls.operator_version == "1.0.0"


def _overconfident_table():
    labels = np.array([0] * 20 + [1] * 20)
    probabilities = np.where(labels == 1, 0.97, 0.03)
    # 制造少量错误，让「极端自信」成为可校正的过度自信
    probabilities[[0, 8, 20, 28]] = 1.0 - probabilities[[0, 8, 20, 28]]
    return _table(_records(labels, probabilities))


def test_temperature_scaling_calibrates_overconfident_probabilities(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    source = store.write_json("predictions.json", _overconfident_table(), "prediction_table")
    result = invoke(
        _request(
            "temperature",
            "TemperatureScaling",
            {"calibrationBins": 10, "seed": 42},
            {"predictions": source},
        ),
        store,
    )
    assert result.status == "completed", result.error

    metrics = store.read_json(result.outputs["metrics"])
    assert metrics["n"] == 40
    assert metrics["temperature"] > 1.0
    assert metrics["eceAfter"] <= metrics["eceBefore"] + 1e-9
    assert metrics["brierAfter"] <= metrics["brierBefore"] + 1e-9
    assert metrics["nllAfter"] <= metrics["nllBefore"] + 1e-9

    calibrated = store.read_json(result.outputs["predictions"])
    assert calibrated["evaluation"] == "external"
    assert calibrated["calibration"]["temperature"] == metrics["temperature"]
    for record in calibrated["records"]:
        assert 0.0 <= record["calibratedProbability"] <= 1.0
        assert "probability" in record
        assert "fold" in record
    assert all(
        record["calibratedProbability"] != record["probability"] for record in calibrated["records"]
    )

    chart = store.read_json(result.outputs["calibration"])
    assert chart["temperature"] == metrics["temperature"]
    assert len(chart["bins"]) == 10
    assert sum(row["count"] for row in chart["bins"]) == 40
    assert {
        "binLower",
        "binUpper",
        "count",
        "meanPredictedBefore",
        "meanPredictedAfter",
        "observed",
    } <= set(chart["bins"][0])


def test_temperature_scaling_handles_extreme_probabilities(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    labels = np.array([0] * 20 + [1] * 20)
    store = ArtifactStore(tmp_path / "workspace")
    source = store.write_json(
        "predictions.json", _table(_records(labels, labels.astype(float))), "prediction_table"
    )
    result = invoke(
        _request("temperature-extreme", "TemperatureScaling", {}, {"predictions": source}),
        store,
    )
    assert result.status == "completed", result.error
    calibrated = store.read_json(result.outputs["predictions"])
    assert all(0.0 <= row["calibratedProbability"] <= 1.0 for row in calibrated["records"])


def test_temperature_scaling_rejects_single_class(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    labels = np.zeros(20, dtype=int)
    store = ArtifactStore(tmp_path / "workspace")
    source = store.write_json(
        "predictions.json",
        _table(_records(labels, np.full(20, 0.4))),
        "prediction_table",
    )
    result = invoke(
        _request("temperature-single", "TemperatureScaling", {}, {"predictions": source}),
        store,
    )
    assert result.status == "failed"
    assert "两个类别" in result.error.message


def test_ensemble_fusion_mean_and_product(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    labels = [0, 1, 0, 1]
    rows_a = _records(labels, [0.2, 0.7, 0.4, 0.9], prefix="a")
    rows_b = _records(labels, [0.4, 0.5, 0.6, 0.1], prefix="a")
    store = ArtifactStore(tmp_path / "workspace")
    ref_a = store.write_json("a.json", _table(rows_a), "prediction_table")
    ref_b = store.write_json("b.json", _table(rows_b), "prediction_table")

    mean_result = invoke(
        _request(
            "fusion-mean",
            "EnsembleFusion",
            {"fusionMethod": "mean", "threshold": 0.5},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert mean_result.status == "completed", mean_result.error
    fused = store.read_json(mean_result.outputs["predictions"])
    assert [row["probability"] for row in fused["records"]] == pytest.approx([0.3, 0.6, 0.5, 0.5])
    assert all(row["fusedBy"] == "mean" for row in fused["records"])
    assert fused["fusedModels"] == 2
    assert fused["evaluation"] == "external"
    report = store.read_json(mean_result.outputs["report"])
    assert report["count"] == 4
    assert report["fusionMethod"] == "mean"
    assert report["agreement"] == pytest.approx(0.5)
    assert report["missing"] == []
    assert report["limitations"]

    product_result = invoke(
        _request(
            "fusion-product",
            "EnsembleFusion",
            {"fusionMethod": "product"},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert product_result.status == "completed", product_result.error
    product = store.read_json(product_result.outputs["predictions"])
    assert [row["probability"] for row in product["records"]] == pytest.approx(
        [0.08, 0.35, 0.24, 0.09]
    )


def test_ensemble_fusion_vote_uses_mean_probability_on_tie(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    labels = [0, 1]
    rows_a = _records(labels, [0.9, 0.2], prefix="v")
    rows_b = _records(labels, [0.1, 0.8], prefix="v")
    store = ArtifactStore(tmp_path / "workspace")
    ref_a = store.write_json("a.json", _table(rows_a), "prediction_table")
    ref_b = store.write_json("b.json", _table(rows_b), "prediction_table")
    result = invoke(
        _request(
            "fusion-vote",
            "EnsembleFusion",
            {"fusionMethod": "vote", "threshold": 0.5},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert result.status == "completed", result.error
    fused = store.read_json(result.outputs["predictions"])
    # 两条记录都是 1:0 平票，vote 输出的 probability 必须回落到均值
    assert [row["probability"] for row in fused["records"]] == pytest.approx([0.5, 0.5])
    assert all(row["fusedBy"] == "vote" for row in fused["records"])


def test_ensemble_fusion_sum_binary_keeps_raw_sum(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    labels = [0, 1, 0, 1]
    rows_a = _records(labels, [0.2, 0.7, 0.4, 0.9], prefix="s")
    rows_b = _records(labels, [0.4, 0.5, 0.6, 0.1], prefix="s")
    store = ArtifactStore(tmp_path / "workspace")
    ref_a = store.write_json("a.json", _table(rows_a), "prediction_table")
    ref_b = store.write_json("b.json", _table(rows_b), "prediction_table")
    result = invoke(
        _request(
            "fusion-sum",
            "EnsembleFusion",
            {"fusionMethod": "sum"},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert result.status == "completed", result.error
    fused = store.read_json(result.outputs["predictions"])
    # 二分类 sum 按规格直接相加，不归一化，因此会超过 1
    assert [row["probability"] for row in fused["records"]] == pytest.approx([0.6, 1.2, 1.0, 1.0])
    assert all(row["fusedBy"] == "sum" for row in fused["records"])

    report = store.read_json(result.outputs["report"])
    assert report["fusionMethod"] == "sum"
    assert any("超过 1" in limitation for limitation in report["limitations"])


def test_ensemble_fusion_multiclass_sum_is_normalized(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    rows_a = [
        {"imageId": "n0", "patientId": "p0", "label": 0, "classProbabilities": [0.1, 0.5, 0.2]},
        {"imageId": "n1", "patientId": "p1", "label": 1, "classProbabilities": [0.3, 0.1, 0.2]},
    ]
    rows_b = [
        {"imageId": "n0", "patientId": "p0", "label": 0, "classProbabilities": [0.2, 0.1, 0.1]},
        {"imageId": "n1", "patientId": "p1", "label": 1, "classProbabilities": [0.1, 0.3, 0.0]},
    ]
    store = ArtifactStore(tmp_path / "workspace")
    ref_a = store.write_json("a.json", _table(rows_a), "prediction_table")
    ref_b = store.write_json("b.json", _table(rows_b), "prediction_table")
    result = invoke(
        _request(
            "fusion-sum-multiclass",
            "EnsembleFusion",
            {"fusionMethod": "sum"},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert result.status == "completed", result.error
    fused = store.read_json(result.outputs["predictions"])
    # 逐元素求和后归一化：[0.3,0.6,0.3]/1.2 与 [0.4,0.4,0.2]/1.0
    assert fused["records"][0]["classProbabilities"] == pytest.approx([0.25, 0.5, 0.25])
    assert fused["records"][1]["classProbabilities"] == pytest.approx([0.4, 0.4, 0.2])
    for record in fused["records"]:
        assert sum(record["classProbabilities"]) == pytest.approx(1.0)
    assert [row["predictedClass"] for row in fused["records"]] == [1, 0]


def test_ensemble_fusion_rejects_image_id_mismatch(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    labels = [0, 1, 0]
    rows_a = _records(labels, [0.2, 0.7, 0.4], prefix="m")
    rows_b = _records(labels, [0.4, 0.5, 0.6], prefix="m")[:2]
    store = ArtifactStore(tmp_path / "workspace")
    ref_a = store.write_json("a.json", _table(rows_a), "prediction_table")
    ref_b = store.write_json("b.json", _table(rows_b), "prediction_table")
    result = invoke(
        _request(
            "fusion-mismatch",
            "EnsembleFusion",
            {"fusionMethod": "mean"},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert result.status == "failed"
    assert "m2" in result.error.message
    assert "predictionsB" in result.error.message


def test_ensemble_fusion_multiclass_mean(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    rows_a = [
        {"imageId": "c0", "patientId": "p0", "label": 0, "classProbabilities": [0.2, 0.7, 0.1]},
        {"imageId": "c1", "patientId": "p1", "label": 1, "classProbabilities": [0.5, 0.3, 0.2]},
    ]
    rows_b = [
        {"imageId": "c0", "patientId": "p0", "label": 0, "classProbabilities": [0.4, 0.4, 0.2]},
        {"imageId": "c1", "patientId": "p1", "label": 1, "classProbabilities": [0.1, 0.6, 0.3]},
    ]
    store = ArtifactStore(tmp_path / "workspace")
    ref_a = store.write_json("a.json", _table(rows_a), "prediction_table")
    ref_b = store.write_json("b.json", _table(rows_b), "prediction_table")
    result = invoke(
        _request(
            "fusion-multiclass",
            "EnsembleFusion",
            {"fusionMethod": "mean"},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert result.status == "completed", result.error
    fused = store.read_json(result.outputs["predictions"])
    assert fused["records"][0]["classProbabilities"] == pytest.approx([0.3, 0.55, 0.15])
    assert fused["records"][1]["classProbabilities"] == pytest.approx([0.3, 0.45, 0.25])
    assert [row["predictedClass"] for row in fused["records"]] == [1, 1]
    assert fused["fusedModels"] == 2


def _benchmark_probabilities():
    labels = np.array([0] * 20 + [1] * 20)
    offsets = np.linspace(0.02, 0.18, 20)
    probabilities = np.concatenate([0.18 - offsets, 0.82 + offsets])
    return labels, probabilities


def test_benchmark_summary_identical_models(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    labels, probabilities = _benchmark_probabilities()
    table = _table(_records(labels, probabilities))
    store = ArtifactStore(tmp_path / "workspace")
    ref_a = store.write_json("a.json", table, "prediction_table")
    ref_b = store.write_json("b.json", table, "prediction_table")
    result = invoke(
        _request(
            "benchmark-same",
            "BenchmarkSummary",
            {"threshold": 0.5, "bootstrap": 200, "seed": 42},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert result.status == "completed", result.error
    metrics = store.read_json(result.outputs["metrics"])
    assert metrics["n"] == 40
    assert metrics["threshold"] == 0.5
    comparison = metrics["comparison"]
    assert comparison["aucDifference"] == pytest.approx(0.0, abs=1e-9)
    assert comparison["delongP"] == pytest.approx(1.0)
    assert comparison["mcnemarP"] == pytest.approx(1.0)
    assert comparison["discordant"] == {"aOnly": 0, "bOnly": 0}
    for name in ("A", "B"):
        model = metrics["models"][name]
        assert 0.0 <= model["auc"] <= 1.0
        assert model["aucCi95"][0] <= model["auc"] <= model["aucCi95"][1]
        assert model["accuracyCi95"][0] <= model["accuracy"] <= model["accuracyCi95"][1]
        assert {"n", "auc", "accuracy", "sensitivity", "specificity", "f1", "brier", "ece"} <= set(
            model
        )

    report = store.read_json(result.outputs["report"])
    assert [row["model"] for row in report["forest"]] == ["A", "B"]
    for row in report["forest"]:
        assert row["ciLow"] <= row["auc"] <= row["ciHigh"]
    assert report["limitations"]


def test_benchmark_summary_distinct_models(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    labels, probabilities = _benchmark_probabilities()
    store = ArtifactStore(tmp_path / "workspace")
    ref_a = store.write_json("a.json", _table(_records(labels, probabilities)), "prediction_table")
    # 反向排序：把 A 的高分样本变成低分，AUC 明显下降
    reversed_probabilities = np.clip(1.0 - probabilities, 0.02, 0.98)
    ref_b = store.write_json(
        "b.json", _table(_records(labels, reversed_probabilities)), "prediction_table"
    )
    result = invoke(
        _request(
            "benchmark-different",
            "BenchmarkSummary",
            {"threshold": 0.5, "bootstrap": 200, "seed": 7},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert result.status == "completed", result.error
    metrics = store.read_json(result.outputs["metrics"])
    comparison = metrics["comparison"]
    assert metrics["models"]["A"]["auc"] > metrics["models"]["B"]["auc"]
    assert comparison["aucDifference"] > 0.0
    assert 0.0 <= comparison["delongP"] <= 1.0
    assert 0.0 <= comparison["mcnemarP"] <= 1.0
    for name in ("A", "B"):
        model = metrics["models"][name]
        assert model["aucCi95"][0] <= model["auc"] <= model["aucCi95"][1]
        assert model["accuracyCi95"][0] <= model["accuracy"] <= model["accuracyCi95"][1]
    assert comparison["discordant"]["aOnly"] + comparison["discordant"]["bOnly"] > 0


def test_benchmark_summary_rejects_missing_label(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    labels, probabilities = _benchmark_probabilities()
    rows = _records(labels, probabilities)
    rows[3]["label"] = None
    store = ArtifactStore(tmp_path / "workspace")
    ref_a = store.write_json("a.json", _table(rows), "prediction_table")
    ref_b = store.write_json("b.json", _table(_records(labels, probabilities)), "prediction_table")
    result = invoke(
        _request(
            "benchmark-missing-label",
            "BenchmarkSummary",
            {"bootstrap": 100},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert result.status == "failed"
    assert "label" in result.error.message
    assert "i3" in result.error.message


def test_benchmark_summary_decision_curve(tmp_path):
    import radstudio.operators_bench  # noqa: F401

    labels, probabilities = _benchmark_probabilities()
    reversed_probabilities = np.clip(1.0 - probabilities, 0.02, 0.98)
    store = ArtifactStore(tmp_path / "workspace")
    ref_a = store.write_json("a.json", _table(_records(labels, probabilities)), "prediction_table")
    ref_b = store.write_json(
        "b.json", _table(_records(labels, reversed_probabilities)), "prediction_table"
    )
    result = invoke(
        _request(
            "benchmark-dca",
            "BenchmarkSummary",
            {"threshold": 0.5, "bootstrap": 100, "seed": 11},
            {"predictionsA": ref_a, "predictionsB": ref_b},
        ),
        store,
    )
    assert result.status == "completed", result.error

    metrics = store.read_json(result.outputs["metrics"])
    dca = metrics["dca"]
    assert len(dca) == 81
    assert dca[0]["threshold"] == pytest.approx(0.10)
    assert dca[-1]["threshold"] == pytest.approx(0.90)
    thresholds = [row["threshold"] for row in dca]
    assert thresholds == pytest.approx([step / 100.0 for step in range(10, 91)])
    assert all(row["treatNone"] == 0.0 for row in dca)
    # pt 趋近 0.9 时 treat-all 的净获益必然转负（N=40，阳性率 0.5）
    assert dca[-1]["treatAll"] < 0.0
    for row in dca:
        assert np.isfinite(row["modelA"])
        assert np.isfinite(row["modelB"])

    # 模型 A 的排序优于随机，净获益应高于模型 B（B 的概率被反向排列）
    mean_a = float(np.mean([row["modelA"] for row in dca]))
    mean_b = float(np.mean([row["modelB"] for row in dca]))
    assert mean_a > mean_b

    report = store.read_json(result.outputs["report"])
    assert "净获益" in report["dcaSource"]
    assert report["netBenefitDifference"] == pytest.approx(mean_a - mean_b)
    assert np.isfinite(report["netBenefitDifference"])
