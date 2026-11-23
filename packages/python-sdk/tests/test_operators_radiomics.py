"""影像组学扩展算子（ImageFilterBank + 3 分类器）测试。

滤波数值正确性（square 平方、haar wavelet-LL 块均值）、定义域防御、
样本展开契约（imageId 后缀、filter 字段、includeOriginal），以及三个
分类器在 serve 模式下的折内训练与 OOF 输出结构。
"""

import numpy as np
import pytest

from radstudio.operator import (
    ArtifactStore,
    OperatorRequest,
    invoke,
    resolve_operator,
)
from radstudio.runner import make_split

RADIOMICS_TYPES = [
    "ImageFilterBank",
    "RandomForestClassifier",
    "SVMClassifier",
    "GradientBoostingClassifier",
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


def _samples_input(tmp_path, store):
    """构造两个正数灰度样本（含 ROI），写入工作区并返回产物引用。"""
    rng = np.random.default_rng(7)
    samples = []
    for i in range(2):
        image = tmp_path / f"img{i}.npy"
        roi = tmp_path / f"roi{i}.npy"
        np.save(image, rng.uniform(10, 100, (8, 8)))
        np.save(roi, np.ones((8, 8)))
        samples.append(
            {
                "imageId": f"i{i}",
                "patientId": f"p{i}",
                "label": i % 2,
                "imagePath": str(image),
                "roiPath": str(roi),
            }
        )
    return store.write_json("samples.json", samples, "sample_collection"), samples


def test_radiomics_operators_registered():
    import radstudio.operators_radiomics  # noqa: F401  # 导入即注册

    for operator_type in RADIOMICS_TYPES:
        operator_class = resolve_operator(operator_type, "1.0.0")
        assert operator_class.operator_type == operator_type
        assert operator_class.operator_version == "1.0.0"


def test_image_filter_bank_square_and_wavelet_derivation(tmp_path):
    import radstudio.operators_radiomics  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    samples_ref, samples = _samples_input(tmp_path, store)
    original = np.load(samples[0]["imagePath"])

    result = invoke(
        _request(
            "filter_bank",
            "ImageFilterBank",
            {"filters": ["square", "wavelet"], "includeOriginal": True, "wavelet": "haar"},
            {"samples": samples_ref},
        ),
        store,
    )
    assert result.status == "completed", result.error
    derived = store.read_json(result.outputs["samples"])

    # 展开契约：原始 + 2 滤波（square 1 派生 + wavelet 4 子带）= 每样本 6 条
    assert len(derived) == 2 * (1 + 1 + 4)
    by_key = {(s["sourceImageId"] if "filter" in s else s["imageId"], s.get("filter")): s for s in derived}
    square = by_key[("i0", "square")]
    assert square["imageId"] == "i0#square"
    assert square["patientId"] == "p0" and square["label"] == 0
    assert square["roiPath"] == samples[0]["roiPath"]
    # square 派生值 = 原图平方（float32）
    np.testing.assert_allclose(
        np.load(store.resolve_path(square["imagePath"])), original**2, rtol=1e-5
    )
    # haar wavelet-LL = 2x2 块均值（idwt2 单子带重建回原尺寸）
    wavelet_ll = by_key[("i0", "wavelet-LL")]
    ll = np.load(store.resolve_path(wavelet_ll["imagePath"]))
    assert ll.shape == original.shape
    blocks = original.reshape(4, 2, 4, 2).mean(axis=(1, 3))
    np.testing.assert_allclose(ll, np.repeat(np.repeat(blocks, 2, axis=0), 2, axis=1), rtol=1e-5)
    # 子带命名完整（原始样本无 filter 字段，不计入）
    assert {s["filter"] for s in derived if "filter" in s} == {
        "square",
        "wavelet-LL",
        "wavelet-LH",
        "wavelet-HL",
        "wavelet-HH",
    }


def test_image_filter_bank_excludes_original_when_disabled(tmp_path):
    import radstudio.operators_radiomics  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    samples_ref, _ = _samples_input(tmp_path, store)
    result = invoke(
        _request(
            "filter_bank",
            "ImageFilterBank",
            {"filters": ["square"], "includeOriginal": False},
            {"samples": samples_ref},
        ),
        store,
    )
    assert result.status == "completed"
    derived = store.read_json(result.outputs["samples"])
    assert len(derived) == 2
    assert all(s.get("filter") == "square" for s in derived)


@pytest.mark.parametrize(
    "filter_name, fixture, match",
    [
        ("squareroot", "mixed", "nonnegative"),
        ("logarithm", "mixed", "strictly positive"),
    ],
)
def test_image_filter_bank_domain_errors(tmp_path, filter_name, fixture, match):
    import radstudio.operators_radiomics  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    image = tmp_path / "mixed.npy"
    np.save(image, np.array([[1.0, 2.0], [-3.0, 4.0]]))
    samples_ref = store.write_json(
        "samples.json",
        [{"imageId": "i0", "patientId": "p0", "label": 0, "imagePath": str(image)}],
        "sample_collection",
    )
    result = invoke(
        _request("filter_bank", "ImageFilterBank", {"filters": [filter_name]}, {"samples": samples_ref}),
        store,
    )
    assert result.status == "failed"
    assert match in result.error.message


def test_image_filter_bank_unknown_filter_rejected(tmp_path):
    import radstudio.operators_radiomics  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    samples_ref, _ = _samples_input(tmp_path, store)
    result = invoke(
        _request(
            "filter_bank", "ImageFilterBank", {"filters": ["median"]}, {"samples": samples_ref}
        ),
        store,
    )
    assert result.status == "failed"
    assert "Unknown filters" in result.error.message


def _assert_oof_structure(train_result, store, n, estimator):
    assert train_result.status == "completed", train_result.error
    model = store.read_json(train_result.outputs["model"])
    predictions = store.read_json(train_result.outputs["predictions"])
    assert model["kind"] == "five_fold_ensemble"
    assert model["estimator"] == estimator
    assert model["evaluation"] == "out_of_fold"
    assert len(model["folds"]) == 5
    records = predictions["records"]
    assert len(records) == n
    # OOF：每个样本恰好出现在一个验证折
    ids = [r["imageId"] for r in records]
    assert len(ids) == len(set(ids))
    assert all(0.0 <= r["probability"] <= 1.0 for r in records)
    assert any(a.type == "model" for a in train_result.artifacts)


@pytest.mark.parametrize(
    "operator_type, parameters, estimator",
    [
        ("RandomForestClassifier", {"nEstimators": 10, "maxDepth": 4}, "random_forest"),
        ("SVMClassifier", {"C": 1.0, "kernel": "rbf", "gamma": "scale"}, "svm"),
        (
            "GradientBoostingClassifier",
            {"nEstimators": 10, "learningRate": 0.1, "maxDepth": 2},
            "gradient_boosting",
        ),
    ],
)
def test_classifier_operators_oof_training(tmp_path, operator_type, parameters, estimator):
    import radstudio.operators_builtin
    import radstudio.operators_radiomics  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    t = table()
    split = make_split(t)
    features = store.write_json("features.json", t, "feature_table")
    split_ref = store.write_json("split.json", split, "split_plan")
    strategy = store.write_json(
        "strategy.json",
        {"missingRateThreshold": 0.2, "varianceThreshold": 0.01, "correlationThreshold": 0.95},
        "filter_strategy",
    )

    train_result = invoke(
        _request(
            "train",
            operator_type,
            parameters,
            {"features": features, "split": split_ref, "strategy": strategy},
        ),
        store,
    )
    _assert_oof_structure(train_result, store, 300, estimator)


SAMPLE_CONTRACT = {"id": "radstudio/sample-collection", "version": "1"}


def _posix_samples(tmp_path):
    """两个样本、两个患者；路径用正斜杠，与平台存储键的真实形态一致。"""
    rng = np.random.default_rng(7)
    rows = []
    for i in range(2):
        image = tmp_path / f"img{i}.npy"
        roi = tmp_path / f"roi{i}.npy"
        np.save(image, rng.uniform(10, 100, (8, 8)))
        np.save(roi, np.ones((8, 8)))
        rows.append(
            {
                "imageId": f"i{i}",
                "patientId": f"p{i}",
                "label": i % 2,
                "imagePath": image.as_posix(),
                "roiPath": roi.as_posix(),
            }
        )
    return rows


def test_filter_bank_derivation_satisfies_sample_contract(tmp_path):
    """绑上契约后，(原图, 派生) 展开仍然成立：身份唯一、患者标签不冲突。

    派生 imageId 带 ``#<滤波名>`` 后缀，最长后缀 11 字符；本地库 1307 条样本里
    imageId 最长 15 字符，因此 255 的键宽有 229 字符余量。
    """
    import radstudio.operators_radiomics  # noqa: F401
    from radstudio.content_contract import validate_content_bytes

    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", _posix_samples(tmp_path), "sample_collection")
    result = invoke(
        _request(
            "filter_bank",
            "ImageFilterBank",
            {"filters": ["square", "wavelet"], "includeOriginal": True, "wavelet": "haar"},
            {"samples": samples_ref},
        ),
        store,
    )
    assert result.status == "completed", result.error
    published = store.read_bytes(result.outputs["samples"])
    derived = store.read_json(result.outputs["samples"])
    assert len(derived) == 12
    assert len({row["imageId"] for row in derived}) == 12
    assert max(len(row["imageId"]) for row in derived) == len("i1#wavelet-HH")
    assert validate_content_bytes(published, "sample_collection", SAMPLE_CONTRACT) == {
        "contract": "radstudio/sample-collection@1",
        "rules": "artifact-content/1",
        "records": 12,
    }


def test_filter_bank_output_rejects_windows_separated_roi(tmp_path):
    """掩膜路径带 Windows 分隔符时契约拒绝——这是绑定新增的拦截面，不是既有保证。"""
    import radstudio.operators_radiomics  # noqa: F401
    from radstudio.content_contract import ContentContractError, validate_content_bytes

    store = ArtifactStore(tmp_path / "workspace")
    rows = _posix_samples(tmp_path)
    rows[1]["roiPath"] = rows[1]["roiPath"].replace("/", "\\")
    samples_ref = store.write_json("samples.json", rows, "sample_collection")
    result = invoke(
        _request(
            "filter_bank",
            "ImageFilterBank",
            {"filters": ["square"], "includeOriginal": False},
            {"samples": samples_ref},
        ),
        store,
    )
    assert result.status == "completed", result.error
    with pytest.raises(ContentContractError) as error:
        validate_content_bytes(
            store.read_bytes(result.outputs["samples"]), "sample_collection", SAMPLE_CONTRACT
        )
    assert error.value.details["fieldPath"] == "/1/roiPath"
