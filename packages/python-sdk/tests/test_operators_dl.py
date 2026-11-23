"""深度学习算子（DLCNNTraining/DLCNNInference）小尺寸张量冒烟测试。

无 torch 环境（未装 [dl] extra）自动跳过。pretrained=False 避免在线
下载权重；resnet18 + 64×64 + 单 epoch 保证 CPU 秒级完成。
"""

import numpy as np
import pytest

from radstudio.operator import ArtifactStore, OperatorRequest, invoke
from radstudio.runner import make_split

pytest.importorskip("torch", reason="DLCNN operators require the [dl] extra")

TRAIN_PARAMS = {
    "backbone": "resnet18",
    "pretrained": False,
    "epochs": 1,
    "batchSize": 8,
    "learningRate": 0.001,
    "earlyStopping": 0,
    "imageSize": 64,
}


def _make_samples(tmp_path, n=20):
    """20 样本 / 10 患者（每类 5 患者），类别间亮度可分。"""
    rng = np.random.default_rng(3)
    samples = []
    for i in range(n):
        label = (i // 2) % 2
        image = tmp_path / f"img{i}.npy"
        np.save(image, rng.uniform(120.0, 180.0, (16, 16)) if label else rng.uniform(10.0, 60.0, (16, 16)))
        samples.append(
            {
                "imageId": f"i{i}",
                "patientId": f"p{i // 2}",
                "label": label,
                "imagePath": str(image),
            }
        )
    return samples


def _feature_table(samples):
    """与样本身份一致的最小特征表（仅为生成 split_plan）。"""
    return {
        "featureNames": ["x"],
        "records": [
            {
                "imageId": s["imageId"],
                "patientId": s["patientId"],
                "label": s["label"],
                "values": [float(s["label"])],
            }
            for s in samples
        ],
    }


def _request(node_id, operator_type, parameters, inputs, version=None):
    return OperatorRequest(
        requestId=f"req-{node_id}",
        runId="run-1",
        nodeId=node_id,
        operatorType=operator_type,
        parameters=parameters,
        inputs=inputs,
        operatorVersion=version,
    )


def _sourced_split(store, samples):
    """DataSplit 1.2.0 口径的划分：携带来源特征表引用与来源样本身份摘要。"""
    from radstudio.content_contract import source_identity

    table = _feature_table(samples)
    features_ref = store.write_json("features.json", table, "feature_table")
    split = make_split(table)
    split["source"] = features_ref
    split["identityHash"] = source_identity(table)
    split_ref = store.write_json("split.json", split, "split_plan")
    return split, split_ref


def test_dl_operators_registered():
    import radstudio.operators_dl  # noqa: F401
    from radstudio.operator import resolve_operator

    for operator_type in ("DLCNNTraining", "DLCNNInference"):
        cls = resolve_operator(operator_type, "1.0.0")
        assert cls.operator_type == operator_type
    latest = resolve_operator("DLCNNInference", "1.1.0")
    assert latest.operator_version == "1.1.0"
    training = resolve_operator("DLCNNTraining", "1.1.0")
    assert training.operator_version == "1.1.0"
    inference = resolve_operator("DLCNNInference", "1.2.0")
    assert inference.operator_version == "1.2.0"


def test_dl_inference_evaluation_role():
    from radstudio.operators_dl import _evaluation_role

    assert _evaluation_role({}) == "inference"
    assert _evaluation_role({"evaluationRole": "external"}) == "external"
    assert _evaluation_role({"evaluationRole": "validation"}) == "validation"
    with pytest.raises(ValueError, match="evaluationRole"):
        _evaluation_role({"evaluationRole": "holdout"})


def test_dl_cnn_training_oof_and_checkpoint(tmp_path):
    import radstudio.operators_dl  # noqa: F401

    samples = _make_samples(tmp_path)
    split = make_split(_feature_table(samples))
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_ref = store.write_json("split.json", split, "split_plan")

    result = invoke(
        _request(
            "dl_train",
            "DLCNNTraining",
            TRAIN_PARAMS,
            {"samples": samples_ref, "split": split_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "completed", result.error
    model = store.read_json(result.outputs["model"])
    assert model["kind"] == "five_fold_ensemble"
    assert model["estimator"] == "dl_cnn"
    assert model["backbone"] == "resnet18"
    assert len(model["folds"]) == 5
    assert model["checkpointArtifact"]
    # checkpoint 可读且为五折权重
    import io

    import torch

    checkpoint = torch.load(
        io.BytesIO(store.read_bytes(model["checkpointArtifact"])),
        map_location="cpu",
        weights_only=True,
    )
    assert checkpoint["format"] == "radstudio.dl.checkpoint.v1"
    assert len(checkpoint["folds"]) == 5

    predictions = store.read_json(result.outputs["predictions"])
    assert predictions["evaluation"] == "out_of_fold"
    records = predictions["records"]
    assert len(records) == 20
    assert all(0.0 <= r["probability"] <= 1.0 for r in records)
    # OOF：每个样本恰好一个验证折
    ids = [r["imageId"] for r in records]
    assert len(ids) == len(set(ids))
    # 类别可分信号：阳性样本平均概率高于阴性
    positive = np.mean([r["probability"] for r in records if r["label"] == 1])
    negative = np.mean([r["probability"] for r in records if r["label"] == 0])
    assert positive > negative


def test_dl_cnn_training_rejects_split_mismatch(tmp_path):
    import radstudio.operators_dl  # noqa: F401

    samples = _make_samples(tmp_path)
    split = make_split(_feature_table(samples))
    split["identityHash"] = "0" * 64
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_ref = store.write_json("split.json", split, "split_plan")
    result = invoke(
        _request(
            "dl_train",
            "DLCNNTraining",
            TRAIN_PARAMS,
            {"samples": samples_ref, "split": split_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "failed"
    assert "identity" in result.error.message


def test_dl_cnn_inference_ensemble(tmp_path):
    import radstudio.operators_dl  # noqa: F401

    samples = _make_samples(tmp_path)
    split = make_split(_feature_table(samples))
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_ref = store.write_json("split.json", split, "split_plan")

    train_result = invoke(
        _request(
            "dl_train",
            "DLCNNTraining",
            TRAIN_PARAMS,
            {"samples": samples_ref, "split": split_ref},
            version="1.0.0",
        ),
        store,
    )
    assert train_result.status == "completed", train_result.error

    # 新样本集推理（4 张、复用训练数据目录中的图像）
    new_samples = [
        {"imageId": f"new{i}", "patientId": f"np{i}", "label": None, "imagePath": samples[i]["imagePath"]}
        for i in range(4)
    ]
    new_ref = store.write_json("new_samples.json", new_samples, "sample_collection")
    infer_result = invoke(
        _request(
            "dl_infer",
            "DLCNNInference",
            {"batchSize": 4},
            {"model": train_result.outputs["model"], "samples": new_ref},
        ),
        store,
    )
    assert infer_result.status == "completed", infer_result.error
    predictions = store.read_json(infer_result.outputs["predictions"])
    assert predictions["evaluation"] == "inference"
    assert predictions["folds"] == 5
    assert len(predictions["records"]) == 4
    assert all(0.0 <= r["probability"] <= 1.0 for r in predictions["records"])


def test_dl_cnn_monai_densenet_training_and_inference(tmp_path):
    """MONAI DenseNet121 + 医学增强 + FocalLoss：训练产物可被推理复用。"""
    import radstudio.operators_dl  # noqa: F401

    samples = _make_samples(tmp_path)
    split = make_split(_feature_table(samples))
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_ref = store.write_json("split.json", split, "split_plan")
    params = {
        **TRAIN_PARAMS,
        "backbone": "densenet121",
        "augmentation": "light",
        "loss": "focal",
        "focalGamma": 2.0,
        "foldIndices": [0],
    }
    result = invoke(
        _request(
            "dl_train_monai",
            "DLCNNTraining",
            params,
            {"samples": samples_ref, "split": split_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "completed", result.error
    model = store.read_json(result.outputs["model"])
    assert model["backbone"] == "densenet121"
    assert model["library"] == "monai"
    assert model["augmentation"] == "light"
    assert model["loss"] == "focal"
    assert len(model["folds"]) == 1
    expected_oof = len(next(fold for fold in split["folds"] if fold["fold"] == 0)["testIds"])
    assert model["metrics"]["count"] == expected_oof
    assert model["metrics"]["auc"] is not None

    import io

    import torch

    checkpoint = torch.load(
        io.BytesIO(store.read_bytes(model["checkpointArtifact"])),
        map_location="cpu",
        weights_only=True,
    )
    assert checkpoint["library"] == "monai"
    assert checkpoint["augmentation"] == "light"
    assert checkpoint["loss"] == "focal"
    assert len(checkpoint["folds"]) == 1

    new_samples = [
        {
            "imageId": f"new{i}",
            "patientId": f"np{i}",
            "label": samples[i]["label"],
            "imagePath": samples[i]["imagePath"],
        }
        for i in range(4)
    ]
    new_ref = store.write_json("monai_new_samples.json", new_samples, "sample_collection")
    infer_result = invoke(
        _request(
            "dl_infer_monai",
            "DLCNNInference",
            {"batchSize": 4},
            {"model": result.outputs["model"], "samples": new_ref},
        ),
        store,
    )
    assert infer_result.status == "completed", infer_result.error
    predictions = store.read_json(infer_result.outputs["predictions"])
    assert predictions["library"] == "monai"
    assert predictions["folds"] == 1
    assert predictions["metrics"]["count"] == 4
    assert [row["imageId"] for row in predictions["records"]] == [s["imageId"] for s in new_samples]


def test_dl_cnn_bce_class_balance_and_standard_augmentation(tmp_path):
    """BCE 类别平衡与 standard 增强组合可跑通，OOF 指标覆盖全部样本。"""
    import radstudio.operators_dl  # noqa: F401

    samples = _make_samples(tmp_path)
    split = make_split(_feature_table(samples))
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_ref = store.write_json("split.json", split, "split_plan")
    params = {
        **TRAIN_PARAMS,
        "augmentation": "standard",
        "loss": "bce",
        "balanceClasses": True,
        "foldIndices": [0],
    }
    result = invoke(
        _request(
            "dl_train_balanced",
            "DLCNNTraining",
            params,
            {"samples": samples_ref, "split": split_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "completed", result.error
    model = store.read_json(result.outputs["model"])
    assert model["library"] == "torchvision"
    assert model["augmentation"] == "standard"
    expected_oof = len(next(fold for fold in split["folds"] if fold["fold"] == 0)["testIds"])
    assert model["metrics"]["count"] == expected_oof
    assert 0.0 <= model["metrics"]["accuracy"] <= 1.0


def test_dl_cnn_training_fold_shards_merge(tmp_path):
    """按折分片训练后合并：checkpoint 覆盖五折，OOF 预测覆盖全部样本。"""
    import radstudio.operators_dl  # noqa: F401

    samples = _make_samples(tmp_path)
    split = make_split(_feature_table(samples))
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_ref = store.write_json("split.json", split, "split_plan")

    fold_groups = [[0, 1, 2], [3, 4]]
    merge_inputs = {"samples": samples_ref}
    for i, fold_indices in enumerate(fold_groups):
        result = invoke(
            _request(
                f"dl_train_shard_{i}",
                "DLCNNTraining",
                {**TRAIN_PARAMS, "shardIndex": i, "shardCount": len(fold_groups), "foldIndices": fold_indices},
                {"samples": samples_ref, "split": split_ref},
                version="1.0.0",
            ),
            store,
        )
        assert result.status == "completed", result.error
        model = store.read_json(result.outputs["model"])
        assert [entry["fold"] for entry in model["folds"]] == fold_indices
        merge_inputs[f"part{i}Model"] = store.write_bytes(
            f"part{i}_model.pt", store.read_bytes(model["checkpointArtifact"]), "model"
        )
        merge_inputs[f"part{i}Pred"] = store.write_bytes(
            f"part{i}_pred.json", store.read_bytes(result.outputs["predictions"]), "prediction_table"
        )

    merged = invoke(
        _request(
            "dl_train_merge",
            "DLCNNTraining",
            {**TRAIN_PARAMS, "mergeParts": len(fold_groups)},
            merge_inputs,
            version="1.0.0",
        ),
        store,
    )
    assert merged.status == "completed", merged.error
    model = store.read_json(merged.outputs["model"])
    assert model["kind"] == "five_fold_ensemble"
    assert [entry["fold"] for entry in model["folds"]] == [0, 1, 2, 3, 4]
    assert sum(entry["testCount"] for entry in model["folds"]) == len(samples)

    import io

    import torch

    checkpoint = torch.load(
        io.BytesIO(store.read_bytes(model["checkpointArtifact"])),
        map_location="cpu",
        weights_only=True,
    )
    assert [entry["fold"] for entry in checkpoint["folds"]] == [0, 1, 2, 3, 4]

    predictions = store.read_json(merged.outputs["predictions"])
    assert predictions["evaluation"] == "out_of_fold"
    assert predictions["identityHash"] == split["identityHash"]
    assert [row["imageId"] for row in predictions["records"]] == [s["imageId"] for s in samples]


def test_dl_cnn_inference_sample_shards_merge(tmp_path):
    """按样本分片推理后合并：预测按全局序号恢复原始样本顺序。"""
    import radstudio.operators_dl  # noqa: F401

    samples = _make_samples(tmp_path)
    split = make_split(_feature_table(samples))
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_ref = store.write_json("split.json", split, "split_plan")
    train_result = invoke(
        _request(
            "dl_train",
            "DLCNNTraining",
            TRAIN_PARAMS,
            {"samples": samples_ref, "split": split_ref},
            version="1.0.0",
        ),
        store,
    )
    assert train_result.status == "completed", train_result.error

    new_samples = [
        {"imageId": f"new{i}", "patientId": f"np{i}", "label": None, "imagePath": samples[i]["imagePath"]}
        for i in range(4)
    ]
    merge_inputs = {}
    for i, (start, end) in enumerate([(0, 2), (2, 4)]):
        part_ref = store.write_json(f"shard{i}_samples.json", new_samples[start:end], "sample_collection")
        result = invoke(
            _request(
                f"dl_infer_shard_{i}",
                "DLCNNInference",
                {"batchSize": 2, "shardIndex": i, "shardCount": 2, "shardOffset": start},
                {"model": train_result.outputs["model"], "samples": part_ref},
            ),
            store,
        )
        assert result.status == "completed", result.error
        merge_inputs[f"part{i}Pred"] = store.write_bytes(
            f"part{i}_pred.json", store.read_bytes(result.outputs["predictions"]), "prediction_table"
        )

    merged = invoke(
        _request("dl_infer_merge", "DLCNNInference", {"mergeParts": 2}, merge_inputs),
        store,
    )
    assert merged.status == "completed", merged.error
    predictions = store.read_json(merged.outputs["predictions"])
    assert predictions["evaluation"] == "inference"
    assert predictions["folds"] == 5
    assert [row["imageId"] for row in predictions["records"]] == [s["imageId"] for s in new_samples]
    assert [row["index"] for row in predictions["records"]] == [0, 1, 2, 3]


def test_dl_cnn_training_v11_emits_image_lineage(tmp_path):
    """DLCNNTraining 1.1.0：完整训练输出 model@3 影像血缘并通过契约校验。"""
    import hashlib
    import io

    import torch

    import radstudio.operators_dl  # noqa: F401
    from radstudio.artifact_boundary import parse_reference
    from radstudio.content_contract import validate_content_file

    samples = _make_samples(tmp_path)
    store = ArtifactStore(tmp_path / "workspace")
    split, split_ref = _sourced_split(store, samples)
    samples_ref = store.write_json("samples.json", samples, "sample_collection")

    result = invoke(
        _request(
            "dl_train_v11",
            "DLCNNTraining",
            TRAIN_PARAMS,
            {"samples": samples_ref, "split": split_ref},
            version="1.1.0",
        ),
        store,
    )
    assert result.status == "completed", result.error
    model = store.read_json(result.outputs["model"])
    assert model["task"] == "binary_classification"
    assert model["dimension"] == "2d"
    assert model["family"] == "resnet18"
    assert model["fit"]["features"] == split["source"]
    assert model["fit"]["split"] == split_ref
    assert [entry["fold"] for entry in model["fit"]["folds"]] == [0, 1, 2, 3, 4]
    assert [entry["trainedImageIds"] for entry in model["fit"]["folds"]] == [
        fold["trainIds"] for fold in split["folds"]
    ]
    assert [entry["testCount"] for entry in model["fit"]["folds"]] == [
        len(fold["testIds"]) for fold in split["folds"]
    ]
    assert sum(entry["trainedCount"] for entry in model["fit"]["folds"]) == sum(
        len(fold["trainIds"]) for fold in split["folds"]
    )
    assert model["checkpointArtifact"]  # 旧消费端仍可读取 checkpoint

    checkpoint_path = store.root / model["weights"]["path"]
    payload = checkpoint_path.read_bytes()
    assert model["weights"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert model["weights"]["sizeBytes"] == len(payload)
    checkpoint = torch.load(io.BytesIO(payload), map_location="cpu", weights_only=True)
    assert [entry["fold"] for entry in checkpoint["folds"]] == [0, 1, 2, 3, 4]

    model_path = store.root / parse_reference(result.outputs["model"])["path"]
    validate_content_file(
        model_path,
        "model",
        {"id": "radstudio/model", "version": "3"},
        reference_roots=[store.root],
    )


def test_dl_cnn_training_v11_partial_shard_omits_model(tmp_path):
    """折分片只训部分折不产出 model 端口，但 checkpoint 仍登记供合并读取。"""
    import radstudio.operators_dl  # noqa: F401

    samples = _make_samples(tmp_path)
    store = ArtifactStore(tmp_path / "workspace")
    _, split_ref = _sourced_split(store, samples)
    samples_ref = store.write_json("samples.json", samples, "sample_collection")

    result = invoke(
        _request(
            "dl_train_v11_shard",
            "DLCNNTraining",
            {**TRAIN_PARAMS, "shardIndex": 0, "shardCount": 2, "foldIndices": [0, 1]},
            {"samples": samples_ref, "split": split_ref},
            version="1.1.0",
        ),
        store,
    )
    assert result.status == "completed", result.error
    assert "model" not in result.outputs
    assert "predictions" in result.outputs
    assert any(
        meta.storagePath == "checkpoint.pt" and meta.type == "model"
        for meta in result.artifacts
    )
    assert not (store.root / "model.json").exists()


def test_dl_cnn_training_v11_merge_emits_complete_lineage(tmp_path):
    """折分片合并：合并调用输出完整五折 model@3，并可被推理 1.2.0 复用。"""
    import radstudio.operators_dl  # noqa: F401
    from radstudio.artifact_boundary import parse_reference
    from radstudio.content_contract import validate_content_file

    samples = _make_samples(tmp_path)
    store = ArtifactStore(tmp_path / "workspace")
    split, split_ref = _sourced_split(store, samples)
    samples_ref = store.write_json("samples.json", samples, "sample_collection")

    fold_groups = [[0, 1, 2], [3, 4]]
    merge_inputs = {"samples": samples_ref, "split": split_ref}
    for i, fold_indices in enumerate(fold_groups):
        result = invoke(
            _request(
                f"dl_train_v11_shard_{i}",
                "DLCNNTraining",
                {**TRAIN_PARAMS, "shardIndex": i, "shardCount": len(fold_groups), "foldIndices": fold_indices},
                {"samples": samples_ref, "split": split_ref},
                version="1.1.0",
            ),
            store,
        )
        assert result.status == "completed", result.error
        assert "model" not in result.outputs
        checkpoint = next(
            meta for meta in result.artifacts if meta.storagePath == "checkpoint.pt"
        )
        merge_inputs[f"part{i}Model"] = store.write_bytes(
            f"v11_part{i}_model.pt",
            (store.root / checkpoint.storagePath).read_bytes(),
            "model",
        )
        merge_inputs[f"part{i}Pred"] = store.write_bytes(
            f"v11_part{i}_pred.json", store.read_bytes(result.outputs["predictions"]),
            "prediction_table",
        )

    merged = invoke(
        _request(
            "dl_train_v11_merge",
            "DLCNNTraining",
            {**TRAIN_PARAMS, "mergeParts": len(fold_groups)},
            merge_inputs,
            version="1.1.0",
        ),
        store,
    )
    assert merged.status == "completed", merged.error
    model = store.read_json(merged.outputs["model"])
    assert model["fit"]["split"] == split_ref
    assert [entry["fold"] for entry in model["fit"]["folds"]] == [0, 1, 2, 3, 4]
    assert [entry["trainedImageIds"] for entry in model["fit"]["folds"]] == [
        fold["trainIds"] for fold in split["folds"]
    ]

    model_path = store.root / parse_reference(merged.outputs["model"])["path"]
    validate_content_file(
        model_path,
        "model",
        {"id": "radstudio/model", "version": "3"},
        reference_roots=[store.root],
    )

    new_samples = [
        {"imageId": f"new{i}", "patientId": f"np{i}", "label": None, "imagePath": samples[i]["imagePath"]}
        for i in range(4)
    ]
    new_ref = store.write_json("v11_new_samples.json", new_samples, "sample_collection")
    infer = invoke(
        _request(
            "dl_infer_v12",
            "DLCNNInference",
            {"batchSize": 2},
            {"model": merged.outputs["model"], "samples": new_ref},
            version="1.2.0",
        ),
        store,
    )
    assert infer.status == "completed", infer.error
    predictions = store.read_json(infer.outputs["predictions"])
    assert predictions["evaluation"] == "inference"
    assert predictions["folds"] == 5
    assert [row["imageId"] for row in predictions["records"]] == [s["imageId"] for s in new_samples]


def _multiclass_samples(tmp_path, per_class=6):
    """三分类样本：亮度区间互不重叠，官方划分固定为 train/validation/test。"""
    rng = np.random.default_rng(11)
    ranges = ((10.0, 50.0), (100.0, 140.0), (190.0, 230.0))
    samples = []
    index = 0
    for label, (low, high) in enumerate(ranges):
        for position in range(per_class):
            image = tmp_path / f"mc{index}.npy"
            np.save(image, rng.uniform(low, high, (16, 16)))
            if position < per_class - 2:
                part = "train"
            elif position == per_class - 2:
                part = "validation"
            else:
                part = "test"
            samples.append(
                {
                    "imageId": f"m{index}",
                    "patientId": f"mp{index}",
                    "label": label,
                    "partition": part,
                    "imagePath": str(image),
                }
            )
            index += 1
    return samples


def test_inception_v3_head_supports_binary_and_multiclass():
    """Inception v3 二分类/多分类分类头可正常前向（含辅助分类头改写）。"""
    import torch

    import radstudio.operators_dl as dl

    binary = dl._build_backbone(torch, "inception_v3", False, num_outputs=1)
    assert tuple(binary(torch.zeros(1, 3, 224, 224)).shape) == (1, 1)
    multiclass = dl._build_backbone(torch, "inception_v3", False, num_outputs=3)
    output = multiclass(torch.zeros(1, 3, 224, 224))
    assert tuple(output.shape) == (1, 3)


def test_dl_cnn_multiclass_training_uses_validation_ids(tmp_path):
    """多分类 + 官方验证集早停：predictions 输出 probabilities，metrics 含混淆矩阵。"""
    import radstudio.operators_builtin
    import radstudio.operators_dl  # noqa: F401

    samples = _multiclass_samples(tmp_path)
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_result = invoke(
        _request(
            "fixed_split",
            "FixedSplit",
            {
                "partitionField": "partition",
                "trainValues": ["train"],
                "validationValues": ["validation"],
                "testValues": ["test"],
            },
            {"samples": samples_ref},
            version="1.0.0",
        ),
        store,
    )
    assert split_result.status == "completed", split_result.error
    split = store.read_json(split_result.outputs["split"])
    assert split["folds"][0]["validationIds"]

    train_result = invoke(
        _request(
            "multiclass_train",
            "DLCNNTraining",
            {
                **TRAIN_PARAMS,
                "classMode": "multiclass",
                "numClasses": 3,
                "earlyStopping": 1,
                "foldIndices": [0],
            },
            {"samples": samples_ref, "split": split_result.outputs["split"]},
            version="1.2.0",
        ),
        store,
    )
    assert train_result.status == "completed", train_result.error
    model = store.read_json(train_result.outputs["model"])
    assert model["classMode"] == "multiclass"
    assert model["numClasses"] == 3
    assert model["evaluation"] == "external"
    assert model["folds"][0]["validationCount"] == len(split["folds"][0]["validationIds"])
    metrics = model["metrics"]
    assert metrics["confusion"] and len(metrics["confusion"]) == 3
    assert metrics["count"] == len(split["folds"][0]["testIds"])

    predictions = store.read_json(train_result.outputs["predictions"])
    assert predictions["evaluation"] == "external"
    assert all(len(row["probabilities"]) == 3 for row in predictions["records"])
    assert all(0 <= row["predictedClass"] < 3 for row in predictions["records"])

    infer = invoke(
        _request(
            "multiclass_infer",
            "DLCNNInference",
            {"batchSize": 6},
            {
                "model": train_result.outputs["model"],
                "samples": store.write_json(
                    "infer_samples.json", samples[:6], "sample_collection"
                ),
            },
            version="1.3.0",
        ),
        store,
    )
    assert infer.status == "completed", infer.error
    infer_predictions = store.read_json(infer.outputs["predictions"])
    assert len(infer_predictions["records"]) == 6
    assert all(len(row["probabilities"]) == 3 for row in infer_predictions["records"])

    grad_cam = invoke(
        _request(
            "gradcam",
            "GradCAM",
            {"sampleCount": 3, "colormap": "viridis", "alpha": 0.5},
            {"samples": samples_ref, "model": train_result.outputs["model"]},
            version="1.0.0",
        ),
        store,
    )
    assert grad_cam.status == "completed", grad_cam.error
    heatmaps = store.read_json(grad_cam.outputs["heatmaps"])
    assert heatmaps["count"] == 3
    assert heatmaps["classMode"] == "multiclass"
    assert len(heatmaps["entries"]) == 3
    assert all((store.root / entry["file"]).exists() for entry in heatmaps["entries"])
    assert heatmaps["entries"][0]["confidence"] >= heatmaps["entries"][-1]["confidence"]
    report = store.read_json(grad_cam.outputs["report"])
    assert report["summary"]["sampleCount"] == 3
    assert report["summary"]["foldCount"] == 1


def test_gradcam_rejects_positive_target_on_multiclass_model(tmp_path):
    """多分类 checkpoint 不接受 positive/negative：这两个目标只对二分类有定义。"""
    import radstudio.operators_builtin
    import radstudio.operators_dl  # noqa: F401

    samples = _multiclass_samples(tmp_path)
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_result = invoke(
        _request(
            "fixed_split_neg",
            "FixedSplit",
            {
                "partitionField": "partition",
                "trainValues": ["train"],
                "validationValues": ["validation"],
                "testValues": ["test"],
            },
            {"samples": samples_ref},
            version="1.0.0",
        ),
        store,
    )
    train_result = invoke(
        _request(
            "multiclass_train_neg",
            "DLCNNTraining",
            {
                **TRAIN_PARAMS,
                "classMode": "multiclass",
                "numClasses": 3,
                "foldIndices": [0],
            },
            {"samples": samples_ref, "split": split_result.outputs["split"]},
            version="1.2.0",
        ),
        store,
    )
    assert train_result.status == "completed", train_result.error
    grad_cam = invoke(
        _request(
            "gradcam_neg",
            "GradCAM",
            {"targetClass": "positive"},
            {"samples": samples_ref, "model": train_result.outputs["model"]},
            version="1.0.0",
        ),
        store,
    )
    assert grad_cam.status == "failed"
    assert "binary" in grad_cam.error.message
