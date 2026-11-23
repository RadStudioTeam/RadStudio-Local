"""CNNFeatureExtraction 混合特征算子测试。

无 torch 环境（未装 [dl] extra）自动跳过。resnet18 + 64×64 + 单折单轮
保证 CPU 一两分钟内完成；训练与特征提取共用同一批 8 张 16×16 合成样本。
"""

from types import SimpleNamespace

import numpy as np
import pytest

from radstudio.operator import ArtifactStore, OperatorRequest, invoke
from radstudio.runner import identity

pytest.importorskip("torch", reason="CNNFeatureExtraction requires the [dl] extra")

TRAIN_PARAMS = {
    "backbone": "resnet18",
    "pretrained": False,
    "epochs": 1,
    "batchSize": 4,
    "learningRate": 0.001,
    "earlyStopping": 0,
    "imageSize": 64,
    # 只训折 0，单折 checkpoint 足够覆盖特征提取分支
    "foldIndices": [0],
}


def _make_samples(tmp_path, count=8):
    """8 样本 / 8 患者，类别间亮度可分。"""
    rng = np.random.default_rng(5)
    samples = []
    for index in range(count):
        label = index % 2
        image = tmp_path / f"img{index}.npy"
        values = (
            rng.uniform(120.0, 180.0, (16, 16))
            if label
            else rng.uniform(10.0, 60.0, (16, 16))
        )
        np.save(image, values)
        samples.append(
            {
                "imageId": f"i{index}",
                "patientId": f"p{index}",
                "label": label,
                "imagePath": str(image),
            }
        )
    return samples


def _sample_identity(samples):
    return identity(
        {
            "records": [
                {
                    "imageId": sample["imageId"],
                    "patientId": sample["patientId"],
                    "label": sample["label"],
                }
                for sample in samples
            ]
        }
    )


def _single_fold_split(samples, held_out=2):
    ids = [sample["imageId"] for sample in samples]
    return {
        "method": "Holdout",
        "folds": [
            {"fold": 0, "trainIds": ids[:-held_out], "testIds": ids[-held_out:]}
        ],
        "seed": 42,
        "identityHash": _sample_identity(samples),
    }


def _request(node_id, operator_type, parameters, inputs, version=None, input_sets=None):
    return OperatorRequest(
        requestId=f"req-{node_id}",
        runId="run-1",
        nodeId=node_id,
        operatorType=operator_type,
        parameters=parameters,
        inputs=inputs,
        operatorVersion=version,
        inputSets=input_sets or {},
    )


def test_cnn_feature_extraction_registered():
    import radstudio.operators_hybrid  # noqa: F401
    from radstudio.operator import resolve_operator

    cls = resolve_operator("CNNFeatureExtraction", "1.0.0")
    assert cls.operator_type == "CNNFeatureExtraction"
    assert cls.operator_version == "1.0.0"


def test_cnn_feature_extraction_end_to_end(tmp_path):
    import radstudio.operators_dl
    import radstudio.operators_hybrid  # noqa: F401

    samples = _make_samples(tmp_path)
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_ref = store.write_json(
        "split.json", _single_fold_split(samples), "split_plan"
    )

    trained = invoke(
        _request(
            "train",
            "DLCNNTraining",
            TRAIN_PARAMS,
            {"samples": samples_ref, "split": split_ref},
            version="1.0.0",
        ),
        store,
    )
    assert trained.status == "completed", trained.error
    model = store.read_json(trained.outputs["model"])
    assert model["backbone"] == "resnet18"
    assert len(model["folds"]) == 1

    result = invoke(
        _request(
            "features",
            "CNNFeatureExtraction",
            {"batchSize": 4},
            {"samples": samples_ref, "model": trained.outputs["model"]},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "completed", result.error
    table = store.read_json(result.outputs["features"])
    assert table["count"] == len(samples)
    assert [row["imageId"] for row in table["records"]] == [
        sample["imageId"] for sample in samples
    ]
    assert table["featureNames"]
    assert table["featureNames"][0] == "penultimate_0"
    assert all(
        len(row["values"]) == len(table["featureNames"]) for row in table["records"]
    )
    assert table["source"]["backbone"] == "resnet18"
    assert table["source"]["folds"] == 1
    assert table["source"]["imageSize"] == 64


def test_cnn_feature_extraction_rejects_non_dl_model(tmp_path):
    import radstudio.operators_hybrid  # noqa: F401

    samples = _make_samples(tmp_path, count=2)
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    model_ref = store.write_json(
        "model.json", {"kind": "logistic_regression", "estimator": "logistic_regression"}, "model"
    )

    result = invoke(
        _request(
            "features_bad_model",
            "CNNFeatureExtraction",
            {"batchSize": 4},
            {"samples": samples_ref, "model": model_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "failed"
    assert "checkpointArtifact" in result.error.message


def test_cnn_feature_extraction_rejects_empty_samples(tmp_path):
    import radstudio.operators_hybrid  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", [], "sample_collection")
    model_ref = store.write_json("model.json", {"kind": "dl_cnn"}, "model")

    result = invoke(
        _request(
            "features_empty",
            "CNNFeatureExtraction",
            {"batchSize": 4},
            {"samples": samples_ref, "model": model_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "failed"
    assert "samples" in result.error.message


FILTER_PARAMS = {
    "missingRateThreshold": 0.2,
    "varianceThreshold": 0.01,
    "correlationThreshold": 0.95,
    "method": "variance_correlation",
}


def _radiomics_table(samples, width=4):
    """与 CNN 特征表同键的合成 radiomics 风格特征表（列名不与 penultimate_* 冲突）。"""
    rng = np.random.default_rng(11)
    return {
        "featureNames": [f"radiomics_{index}" for index in range(width)],
        "records": [
            {
                "imageId": sample["imageId"],
                "patientId": sample["patientId"],
                "label": sample["label"],
                "values": rng.normal(size=width).round(6).tolist(),
            }
            for sample in samples
        ],
    }


@pytest.fixture(scope="module")
def cnn_feature_set(tmp_path_factory):
    """训练一次 tiny resnet18 并提取 12 个样本的 CNN 特征，供两条联通链路复用。"""
    import radstudio.operators_dl
    import radstudio.operators_hybrid  # noqa: F401

    base = tmp_path_factory.mktemp("hybrid-chain")
    samples = _make_samples(base, count=12)
    store = ArtifactStore(base / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_ref = store.write_json(
        "dl-split.json", _single_fold_split(samples, held_out=2), "split_plan"
    )

    trained = invoke(
        _request(
            "train",
            "DLCNNTraining",
            TRAIN_PARAMS,
            {"samples": samples_ref, "split": split_ref},
            version="1.0.0",
        ),
        store,
    )
    assert trained.status == "completed", trained.error

    extracted = invoke(
        _request(
            "features",
            "CNNFeatureExtraction",
            {"batchSize": 4},
            {"samples": samples_ref, "model": trained.outputs["model"]},
            version="1.0.0",
        ),
        store,
    )
    assert extracted.status == "completed", extracted.error
    table = store.read_json(extracted.outputs["features"])
    assert table["count"] == len(samples)
    return SimpleNamespace(
        store=store,
        samples=samples,
        table=table,
        features_ref=extracted.outputs["features"],
    )


def test_cnn_features_holdout_filter_logistic_regression_end_to_end(cnn_feature_set):
    """CNN 特征 → HoldoutSplit → FeatureFilter → LR 训练 → ModelApply 推理。"""
    import radstudio.operators_builtin
    import radstudio.operators_hybrid  # noqa: F401

    store = cnn_feature_set.store
    holdout = invoke(
        _request(
            "holdout",
            "HoldoutSplit",
            {
                "validationRatio": 0.0,
                "testRatio": 0.25,
                "randomSeed": 42,
                "stratify": True,
            },
            {"features": cnn_feature_set.features_ref},
            version="1.2.0",
        ),
        store,
    )
    assert holdout.status == "completed", holdout.error
    split = store.read_json(holdout.outputs["split"])
    train_table = store.read_json(holdout.outputs["train"])
    test_table = store.read_json(holdout.outputs["test"])
    assert split["scheme"] == "holdout"
    assert train_table["featureNames"] == cnn_feature_set.table["featureNames"]
    assert test_table["featureNames"] == cnn_feature_set.table["featureNames"]
    assert len(test_table["records"]) >= 1
    assert len(train_table["records"]) + len(test_table["records"]) == len(cnn_feature_set.samples)

    # LR 的 train() 需要至少两折交叉验证，因此训练集上再做分组二折划分。
    cross_validation = invoke(
        _request(
            "cv",
            "DataSplit",
            {"folds": 2, "randomSeed": 42},
            {"features": holdout.outputs["train"]},
            version="1.2.0",
        ),
        store,
    )
    assert cross_validation.status == "completed", cross_validation.error

    strategy = invoke(
        _request("filter", "FeatureFilter", dict(FILTER_PARAMS), {}, version="1.1.0"),
        store,
    )
    assert strategy.status == "completed", strategy.error

    trained = invoke(
        _request(
            "lr",
            "LogisticRegression",
            {"C": 1.0, "maxIterations": 1000},
            {
                "features": holdout.outputs["train"],
                "split": cross_validation.outputs["split"],
                "strategy": strategy.outputs["strategy"],
            },
            version="1.2.0",
        ),
        store,
    )
    assert trained.status == "completed", trained.error
    model = store.read_json(trained.outputs["model"])
    assert model["featureNames"] == train_table["featureNames"]
    oof = store.read_json(trained.outputs["predictions"])
    assert len(oof["records"]) == len(train_table["records"])
    assert all(0.0 <= row["probability"] <= 1.0 for row in oof["records"])

    applied = invoke(
        _request(
            "apply",
            "ModelApply",
            {},
            {"features": holdout.outputs["test"], "model": trained.outputs["model"]},
            version="1.0.0",
        ),
        store,
    )
    assert applied.status == "completed", applied.error
    predictions = store.read_json(applied.outputs["predictions"])
    assert predictions["evaluation"] == "external"
    assert len(predictions["records"]) == len(test_table["records"])
    assert [row["imageId"] for row in predictions["records"]] == [
        row["imageId"] for row in test_table["records"]
    ]
    assert all(0.0 <= row["probability"] <= 1.0 for row in predictions["records"])


def test_cnn_and_radiomics_feature_join_trains_logistic_regression(cnn_feature_set):
    """特征拼接由 FeatureTableJoin 承担：CNN 特征 + radiomics 特征 → LR。"""
    import radstudio.operators_builtin
    import radstudio.operators_hybrid  # noqa: F401

    store = cnn_feature_set.store
    radiomics = _radiomics_table(cnn_feature_set.samples)
    radiomics_ref = store.write_json("radiomics.json", radiomics, "feature_table")

    join = invoke(
        _request(
            "join",
            "FeatureTableJoin",
            {"joinKey": "imageId", "join": "inner", "conflict": "error"},
            {},
            version="1.0.0",
            input_sets={"features": [cnn_feature_set.features_ref, radiomics_ref]},
        ),
        store,
    )
    assert join.status == "completed", join.error
    joined = store.read_json(join.outputs["features"])
    assert len(joined["featureNames"]) == len(cnn_feature_set.table["featureNames"]) + len(
        radiomics["featureNames"]
    )
    assert len(joined["records"]) == len(cnn_feature_set.samples)
    assert all(len(row["values"]) == len(joined["featureNames"]) for row in joined["records"])
    report = store.read_json(join.outputs["report"])
    assert report["kept"] == len(cnn_feature_set.samples)

    cross_validation = invoke(
        _request(
            "joined_cv",
            "DataSplit",
            {"folds": 2, "randomSeed": 42},
            {"features": join.outputs["features"]},
            version="1.2.0",
        ),
        store,
    )
    assert cross_validation.status == "completed", cross_validation.error

    strategy = invoke(
        _request("joined_filter", "FeatureFilter", dict(FILTER_PARAMS), {}, version="1.1.0"),
        store,
    )
    assert strategy.status == "completed", strategy.error

    trained = invoke(
        _request(
            "joined_lr",
            "LogisticRegression",
            {"C": 1.0, "maxIterations": 1000},
            {
                "features": join.outputs["features"],
                "split": cross_validation.outputs["split"],
                "strategy": strategy.outputs["strategy"],
            },
            version="1.2.0",
        ),
        store,
    )
    assert trained.status == "completed", trained.error
    model = store.read_json(trained.outputs["model"])
    assert len(model["featureNames"]) == len(joined["featureNames"])
    predictions = store.read_json(trained.outputs["predictions"])
    assert len(predictions["records"]) == len(cnn_feature_set.samples)
    assert all(0.0 <= row["probability"] <= 1.0 for row in predictions["records"])
