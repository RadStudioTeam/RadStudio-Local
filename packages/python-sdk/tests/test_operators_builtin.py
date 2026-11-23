"""内置算子迁移到统一框架后的 serve 模式共用性验证。

单体 runner 路径由 test_workflow_runner.py 覆盖；此处验证同一份算子实现
在 serve 模式（BundleArtifactStore + invoke）下可正确串联执行，
以及 ReadImages/Report 的上下文边界行为符合预期。
"""

import io

import numpy as np
from PIL import Image

from radstudio.operator import (
    BundleArtifactStore,
    OperatorRequest,
    invoke,
    resolve_operator,
)


def _write_pair(store, name):
    """写一对 12x12 影像与掩膜，返回 samples 可用的相对路径。"""
    image = np.zeros((12, 12), dtype=np.uint8)
    image[2:9, 2:9] = 7
    mask = np.zeros((12, 12), dtype=np.uint8)
    mask[3:7, 3:7] = 255
    paths = []
    for suffix, value in (("image", image), ("roi", mask)):
        buffer = io.BytesIO()
        Image.fromarray(value).save(buffer, format="PNG")
        paths.append(store.write_local(f"{name}_{suffix}.png", buffer.getvalue()))
    return paths

BUILTIN_TYPES = [
    "ReadImages",
    "ImageFormatConversion",
    "QualityControl",
    "ROIValidation",
    "Preprocessing2D",
    "IBSI2DFeatureExtraction",
    "DataSplit",
    "FeatureFilter",
    "LogisticRegression",
    "Evaluation",
    "Report",
]


def table(n=300):
    rng = np.random.default_rng(42)
    return {
        "featureNames": ["signal", "noise", "constant", "duplicate"],
        "records": [
            {
                "imageId": f"i{i}",
                "patientId": f"p{i // 2}",
                "label": (i // 2) % 2,
                "values": [
                    float((i // 2) % 2 + rng.normal()),
                    float(rng.normal()),
                    1,
                    float(i % 3),
                ],
            }
            for i in range(n)
        ],
    }


def _request(node_id, operator_type, parameters, inputs):
    return OperatorRequest(
        requestId=f"req-{node_id}",
        runId="run-1",
        nodeId=node_id,
        operatorType=operator_type,
        parameters=parameters,
        inputs=inputs,
    )


def test_all_builtin_operators_registered():
    import radstudio.operators_builtin  # 导入即注册
    import radstudio.operators_readers  # noqa: F401  # 读取节点由 readers 注册

    for operator_type in BUILTIN_TYPES:
        operator_class = resolve_operator(operator_type, "1.0.0")
        assert operator_class.operator_type == operator_type
        assert operator_class.operator_version == "1.0.0"


def test_serve_mode_pipeline_filter_split_train_evaluate(tmp_path):
    """serve 模式串联 FeatureFilter→DataSplit→LogisticRegression→Evaluation。"""
    import radstudio.operators_builtin  # noqa: F401

    store = BundleArtifactStore(tmp_path / "workspace")
    features = store.write_json("features.json", table(), "feature_table")

    filter_result = invoke(
        _request(
            "filter",
            "FeatureFilter",
            {
                "missingRateThreshold": 0.2,
                "varianceThreshold": 0.01,
                "correlationThreshold": 0.95,
            },
            {},
        ),
        store,
    )
    assert filter_result.status == "completed"

    split_result = invoke(
        _request("split", "DataSplit", {"folds": 5, "randomSeed": 42}, {"features": features}),
        store,
    )
    assert split_result.status == "completed"
    split = store.read_json(split_result.outputs["split"])
    assert len(split["folds"]) == 5

    train_result = invoke(
        _request(
            "train",
            "LogisticRegression",
            {"C": 1.0, "maxIterations": 1000},
            {
                "features": features,
                "split": split_result.outputs["split"],
                "strategy": filter_result.outputs["strategy"],
            },
        ),
        store,
    )
    assert train_result.status == "completed"
    model = store.read_json(train_result.outputs["model"])
    assert model["kind"] == "five_fold_ensemble"
    assert any(a.type == "model" for a in train_result.artifacts)

    eval_result = invoke(
        _request(
            "eval",
            "Evaluation",
            {"threshold": 0.5},
            {"predictions": train_result.outputs["predictions"]},
        ),
        store,
    )
    assert eval_result.status == "completed"
    metrics = store.read_json(eval_result.outputs["metrics"])
    assert metrics["evaluation"] == "out_of_fold"
    assert metrics["n"] == 300
    assert 0.0 <= metrics["auc"] <= 1.0

    # serve 扩展：输出工作区可导出为 bundle 并在新的工作区完整读回
    bundle = store.export_bundle()
    restored = BundleArtifactStore(tmp_path / "restored")
    restored.import_bundle(bundle)
    assert restored.read_json(eval_result.outputs["metrics"])["n"] == 300


def test_serve_mode_readimages_requires_snapshot_manifest(tmp_path):
    import radstudio.operators_readers  # noqa: F401

    store = BundleArtifactStore(tmp_path / "workspace")
    result = invoke(
        _request(
            "input",
            "ReadImages",
            {
                "imageRole": "image",
                "roiRole": "",
                "requireRoi": False,
                "patientField": "patientId",
            },
            {},
        ),
        store,
    )
    assert result.status == "failed"
    assert "manifest" in result.error.message


def test_serve_mode_report_reproduction_unavailable(tmp_path):
    import radstudio.operators_builtin as _builtin  # noqa: F401  # 注册内置算子
    import radstudio.operators_figures as _figures  # noqa: F401  # Report 1.1.0 由此注册，缺它则「latest」退化为 1.0.0

    store = BundleArtifactStore(tmp_path / "workspace")
    metrics = store.write_json("metrics.json", {"metrics": {"auc": 0.9}}, "metrics")
    legacy = _request("report", "Report", {}, {"metrics": metrics})
    legacy.operatorVersion = "1.0.0"
    result = invoke(legacy, store)
    assert result.status == "failed"
    assert "monolithic runner context" in result.error.message
    latest = invoke(_request("report", "Report", {}, {"metrics": metrics}), store)
    assert latest.status == "completed"


def _validation_samples(store):
    """写 5 个 qcStatus 各异的样本（影像与掩膜齐备），返回 samples 产物引用。"""
    statuses = ["accepted", "warning", "pending", "blocked", "excluded"]
    rows = []
    for index, status in enumerate(statuses):
        image_path, roi_path = _write_pair(store, f"case-{index}")
        rows.append(
            {
                "imageId": f"case-{index}",
                "patientId": f"p{index}",
                "label": index % 2,
                "imagePath": image_path,
                "roiPath": roi_path,
                "qcStatus": status,
            }
        )
    return store.write_json("samples.json", rows, "sample_collection"), statuses


def test_serve_mode_quality_control_filters_qc_blocked_samples(tmp_path):
    """qcStatus 的合法域来自 dataset_samples 的库内 CHECK：blocked 必须被拦下。

    改造前这里比对的是 ["failed", "excluded"]，而 "failed" 从不出现在域内，
    也没有任何算子写入它，因此被质控判为 blocked 的样本会一路通过
    QualityControl 与 ROIValidation 进入特征提取。
    """
    import radstudio.operators_builtin  # noqa: F401

    store = BundleArtifactStore(tmp_path / "qc")
    samples, statuses = _validation_samples(store)

    result = invoke(
        _request("qc", "QualityControl", {"excludeFailed": True}, {"samples": samples}),
        store,
    )
    assert result.status == "completed"
    passed = store.read_json(result.outputs["samples"])
    report = store.read_json(result.outputs["report"])
    assert [row["qcStatus"] for row in passed] == statuses[:3]
    assert [entry["imageId"] for entry in report["excluded"]] == ["case-3", "case-4"]
    assert all("QC" in entry["reason"] for entry in report["excluded"])


def test_serve_mode_roi_validation_fails_fast_on_qc_blocked_sample(tmp_path):
    """ROIValidation 遇错即失败：第一个被拦下的样本必须是 blocked，而不是域外值。"""
    import radstudio.operators_builtin  # noqa: F401

    store = BundleArtifactStore(tmp_path / "roi")
    samples, _ = _validation_samples(store)

    roi = invoke(
        _request(
            "roi",
            "ROIValidation",
            {"excludeFailed": True, "previewOverlay": False},
            {"samples": samples},
        ),
        store,
    )
    assert roi.status == "failed"
    assert "case-3" in roi.error.message


def _fixed_split_samples():
    parts = ["train"] * 6 + ["validation"] * 2 + ["test"] * 2
    return [
        {
            "imageId": f"i{index}",
            "patientId": f"p{index}",
            "label": index % 2,
            "officialSplit": parts[index],
        }
        for index in range(10)
    ]


def test_fixed_split_uses_dataset_partitions(tmp_path):
    """FixedSplit：按官方划分列切分，输出单折并带验证集 ID 与样本身份摘要。"""
    import radstudio.operators_builtin  # noqa: F401
    from radstudio.runner import identity

    store = BundleArtifactStore(tmp_path / "fixed_split")
    samples = _fixed_split_samples()
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    result = invoke(
        _request(
            "fixed",
            "FixedSplit",
            {
                "partitionField": "officialSplit",
                "trainValues": ["train"],
                "validationValues": ["validation"],
                "testValues": ["test"],
            },
            {"samples": samples_ref},
        ),
        store,
    )
    assert result.status == "completed", result.error
    split = store.read_json(result.outputs["split"])
    assert split["method"] == "FixedPartition"
    assert split["seed"] is None
    assert split["counts"] == {"train": 6, "validation": 2, "test": 2}
    fold = split["folds"][0]
    assert fold["fold"] == 0
    assert fold["trainIds"] == [f"i{i}" for i in range(6)]
    assert fold["validationIds"] == ["i6", "i7"]
    assert fold["testIds"] == ["i8", "i9"]
    assert split["identityHash"] == identity(
        {"records": [{k: s[k] for k in ("imageId", "patientId", "label")} for s in samples]}
    )


def test_fixed_split_rejects_uncovered_partition_value(tmp_path):
    """未匹配任何取值的样本必须报错并点名，不能静默丢弃。"""
    import radstudio.operators_builtin  # noqa: F401

    store = BundleArtifactStore(tmp_path / "fixed_split_bad")
    samples = _fixed_split_samples()
    samples.append(
        {"imageId": "i10", "patientId": "p10", "label": 1, "officialSplit": "unknown"}
    )
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    result = invoke(
        _request(
            "fixed_bad",
            "FixedSplit",
            {
                "partitionField": "officialSplit",
                "trainValues": ["train"],
                "validationValues": ["validation"],
                "testValues": ["test"],
            },
            {"samples": samples_ref},
        ),
        store,
    )
    assert result.status == "failed"
    assert "i10" in result.error.message
    assert "unknown" in result.error.message


def test_fixed_split_rejects_overlapping_values(tmp_path):
    """三类取值相交时直接失败：同一取值不能同时属于训练集与测试集。"""
    import radstudio.operators_builtin  # noqa: F401

    store = BundleArtifactStore(tmp_path / "fixed_split_overlap")
    samples_ref = store.write_json("samples.json", _fixed_split_samples(), "sample_collection")
    result = invoke(
        _request(
            "fixed_overlap",
            "FixedSplit",
            {
                "partitionField": "officialSplit",
                "trainValues": ["train", "validation"],
                "validationValues": ["validation"],
                "testValues": ["test"],
            },
            {"samples": samples_ref},
        ),
        store,
    )
    assert result.status == "failed"
    assert "overlap" in result.error.message

