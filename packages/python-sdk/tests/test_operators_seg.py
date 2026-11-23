"""分割算子：UNet 训练/推理、掩膜写回与分割质量评估。

无 torch/MONAI 环境（未装 [dl] extra）自动跳过。64×64、单轮、单折保证 CPU 秒级完成。
"""

import numpy as np
import pytest

from radstudio.operator import ArtifactStore, OperatorRequest, invoke
from radstudio.runner import identity

pytest.importorskip("torch", reason="Segmentation operators require the [dl] extra")
pytest.importorskip("monai", reason="Segmentation operators require MONAI in the [dl] extra")

TRAIN_PARAMS = {
    "backbone": "unet",
    "imageSize": 64,
    "epochs": 1,
    "batchSize": 4,
    "learningRate": 0.001,
    "earlyStopping": 0,
    "loss": "dice_bce",
    "augmentation": "none",
    "threshold": 0.5,
    "foldIndices": [0],
}


def _make_samples(tmp_path, count=8):
    """每样本一张 16×16 灰度图与中心矩形真值掩膜，患者互不相同。"""
    rng = np.random.default_rng(7)
    samples = []
    for index in range(count):
        image = rng.uniform(0.0, 255.0, (16, 16))
        mask = np.zeros((16, 16), dtype=np.uint8)
        mask[4:12, 4:12] = 255
        image_path = tmp_path / f"img{index}.npy"
        roi_path = tmp_path / f"roi{index}.npy"
        np.save(image_path, image)
        np.save(roi_path, mask)
        samples.append(
            {
                "imageId": f"i{index}",
                "patientId": f"p{index}",
                "label": index % 2,
                "imagePath": str(image_path),
                "roiPath": str(roi_path),
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
                    "label": sample.get("label"),
                }
                for sample in samples
            ]
        }
    )


def _fixed_split(samples, held_out=2):
    ids = [sample["imageId"] for sample in samples]
    return {
        "method": "Holdout",
        "folds": [
            {
                "fold": 0,
                "trainIds": ids[:-held_out],
                "testIds": ids[-held_out:],
            }
        ],
        "seed": 42,
        "identityHash": _sample_identity(samples),
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


def test_seg_operators_registered():
    import radstudio.operators_seg  # noqa: F401
    from radstudio.operator import resolve_operator

    for operator_type in (
        "SegUNetTraining",
        "SegUNetInference",
        "SegMaskApply",
        "SegmentationEvaluation",
    ):
        cls = resolve_operator(operator_type, "1.0.0")
        assert cls.operator_type == operator_type
        assert cls.operator_version == "1.0.0"


def test_seg_pipeline_trains_infers_applies_and_evaluates(tmp_path):
    import radstudio.operators_seg  # noqa: F401

    samples = _make_samples(tmp_path)
    store = ArtifactStore(tmp_path / "workspace")
    # SegMaskApply 会写出同名 samples.json，原始样本必须放在不冲突的路径上，
    # 否则评估步骤读到的是被改写过 roiPath 的内容，哈希校验会直接失败
    samples_ref = store.write_json("inputs/samples.json", samples, "sample_collection")
    split_ref = store.write_json("split.json", _fixed_split(samples), "split_plan")

    trained = invoke(
        _request(
            "seg_train",
            "SegUNetTraining",
            TRAIN_PARAMS,
            {"samples": samples_ref, "split": split_ref},
            version="1.0.0",
        ),
        store,
    )
    assert trained.status == "completed", trained.error
    model = store.read_json(trained.outputs["model"])
    assert model["kind"] == "seg_unet_ensemble"
    assert model["imageSize"] == 64
    assert model["threshold"] == 0.5
    assert len(model["folds"]) == 1
    assert model["folds"][0]["testCount"] == 2

    import io

    import torch

    checkpoint = torch.load(
        io.BytesIO(store.read_bytes(model["checkpointArtifact"])),
        map_location="cpu",
        weights_only=True,
    )
    assert checkpoint["format"] == "radstudio.seg.checkpoint.v1"
    assert len(checkpoint["folds"]) == 1

    predictions = store.read_json(trained.outputs["predictions"])
    test_ids = _fixed_split(samples)["folds"][0]["testIds"]
    assert [record["imageId"] for record in predictions["records"]] == test_ids
    assert all(0.0 <= record["dice"] <= 1.0 for record in predictions["records"])
    assert all(0.0 <= record["iou"] <= 1.0 for record in predictions["records"])

    inferred = invoke(
        _request(
            "seg_infer",
            "SegUNetInference",
            {"threshold": 0.5},
            {"model": trained.outputs["model"], "samples": samples_ref},
            version="1.0.0",
        ),
        store,
    )
    assert inferred.status == "completed", inferred.error
    index = store.read_json(inferred.outputs["masks"])
    assert index["count"] == len(samples)
    assert index["threshold"] == 0.5
    # 单折下只有测试样本能走 OOF；训练样本退回到「未见该样本的折」，本测试里没有 → 全部折
    assert index["outOfFoldCount"] == 2
    entry = index["entries"][0]
    assert 0.0 <= entry["foregroundRatio"] <= 1.0
    from PIL import Image

    with Image.open(store.resolve_path(entry["file"])) as image:
        mask = np.asarray(image)
    # 掩膜尺寸必须还原到原图尺寸，下游 ROI 校验才不会因形状不符失败
    assert mask.shape == (16, 16)

    applied = invoke(
        _request(
            "seg_apply",
            "SegMaskApply",
            {"keepOriginalRoi": True},
            {"samples": samples_ref, "masks": inferred.outputs["masks"]},
            version="1.0.0",
        ),
        store,
    )
    assert applied.status == "completed", applied.error
    rewritten = store.read_json(applied.outputs["samples"])
    report = store.read_json(applied.outputs["report"])
    assert report["applied"] == len(samples)
    assert report["blocked"] == 0
    for sample in rewritten:
        assert sample["roiSource"] == "predicted"
        assert sample["groundTruthRoiPath"].endswith(".npy")
        assert sample["roiPath"].startswith("masks/")

    evaluated = invoke(
        _request(
            "seg_eval",
            "SegmentationEvaluation",
            {"threshold": 0.5, "bootstrap": 200},
            {"samples": samples_ref, "masks": inferred.outputs["masks"]},
            version="1.0.0",
        ),
        store,
    )
    assert evaluated.status == "completed", evaluated.error
    metrics = store.read_json(evaluated.outputs["metrics"])
    assert metrics["count"] == len(samples)
    assert metrics["emptyGroundTruth"] == []
    assert 0.0 <= metrics["dice"]["mean"] <= 1.0
    assert metrics["dice"]["ci95"][0] <= metrics["dice"]["mean"] <= metrics["dice"]["ci95"][1]
    assert metrics["hd95"]["mean"] is not None
    report = store.read_json(evaluated.outputs["report"])
    assert report["summary"]["evaluated"] == len(samples)


def test_seg_training_rejects_split_mismatch(tmp_path):
    import radstudio.operators_seg  # noqa: F401

    samples = _make_samples(tmp_path)
    split = _fixed_split(samples)
    split["identityHash"] = "0" * 64
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    split_ref = store.write_json("split.json", split, "split_plan")
    result = invoke(
        _request(
            "seg_train",
            "SegUNetTraining",
            TRAIN_PARAMS,
            {"samples": samples_ref, "split": split_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "failed"
    assert "identity" in result.error.message


def test_seg_inference_rejects_classification_model(tmp_path):
    import radstudio.operators_seg  # noqa: F401

    samples = _make_samples(tmp_path, count=2)
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    model_ref = store.write_json(
        "model.json", {"kind": "five_fold_ensemble", "estimator": "dl_cnn"}, "model"
    )
    result = invoke(
        _request(
            "seg_infer",
            "SegUNetInference",
            {},
            {"model": model_ref, "samples": samples_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "failed"
    assert "checkpointArtifact" in result.error.message


def test_seg_mask_apply_blocks_missing_and_empty_masks(tmp_path):
    import radstudio.operators_seg  # noqa: F401

    samples = _make_samples(tmp_path, count=3)
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    valid = np.zeros((16, 16), dtype=np.uint8)
    valid[4:12, 4:12] = 255
    empty = np.zeros((16, 16), dtype=np.uint8)
    valid_ref = store.write_bytes("masks/i0.png", _png(valid), "mask")
    empty_ref = store.write_bytes("masks/i1.png", _png(empty), "mask")
    index = {
        "entries": [
            {"imageId": "i0", "file": _path(valid_ref), "foregroundRatio": 0.25},
            {"imageId": "i1", "file": _path(empty_ref), "foregroundRatio": 0.0},
        ]
    }
    index_ref = store.write_json("mask-index.json", index, "mask_collection")
    result = invoke(
        _request(
            "seg_apply",
            "SegMaskApply",
            {"keepOriginalRoi": True},
            {"samples": samples_ref, "masks": index_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report == {"applied": 1, "blocked": 2, "count": 3}
    rewritten = {sample["imageId"]: sample for sample in store.read_json(result.outputs["samples"])}
    assert rewritten["i0"].get("qcStatus") != "blocked"
    assert rewritten["i0"]["roiPath"] == "masks/i0.png"
    assert rewritten["i1"]["qcStatus"] == "blocked"
    assert rewritten["i2"]["qcStatus"] == "blocked"
    # blocked 样本不得被下游当作有效 ROI 使用
    assert rewritten["i1"]["roiPath"].endswith("roi1.npy")
    assert rewritten["i2"]["roiPath"].endswith("roi2.npy")


def test_segmentation_evaluation_excludes_empty_ground_truth(tmp_path):
    import radstudio.operators_seg  # noqa: F401

    samples = _make_samples(tmp_path, count=2)
    empty_truth = np.zeros((16, 16), dtype=np.uint8)
    np.save(tmp_path / "roi1.npy", empty_truth)
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    predicted = np.zeros((16, 16), dtype=np.uint8)
    predicted[4:12, 4:12] = 255
    entries = []
    for sample in samples:
        reference = store.write_bytes(f"masks/{sample['imageId']}.png", _png(predicted), "mask")
        entries.append({"imageId": sample["imageId"], "file": _path(reference)})
    index_ref = store.write_json(
        "mask-index.json", {"entries": entries}, "mask_collection"
    )
    result = invoke(
        _request(
            "seg_eval",
            "SegmentationEvaluation",
            {"bootstrap": 100},
            {"samples": samples_ref, "masks": index_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "completed", result.error
    metrics = store.read_json(result.outputs["metrics"])
    assert metrics["count"] == 1
    assert metrics["emptyGroundTruth"] == ["i1"]
    # 完全重合的掩膜 Dice=1、HD95=0
    assert metrics["dice"]["mean"] == pytest.approx(1.0)
    assert metrics["hd95"]["mean"] == pytest.approx(0.0)


def test_segmentation_evaluation_requires_ground_truth(tmp_path):
    import radstudio.operators_seg  # noqa: F401

    samples = _make_samples(tmp_path, count=1)
    np.save(tmp_path / "roi0.npy", np.zeros((16, 16), dtype=np.uint8))
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    predicted = np.zeros((16, 16), dtype=np.uint8)
    reference = store.write_bytes("masks/i0.png", _png(predicted), "mask")
    index_ref = store.write_json(
        "mask-index.json", {"entries": [{"imageId": "i0", "file": _path(reference)}]}, "mask_collection"
    )
    result = invoke(
        _request(
            "seg_eval",
            "SegmentationEvaluation",
            {},
            {"samples": samples_ref, "masks": index_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "failed"
    assert "ground truth" in result.error.message


def test_hd95_is_zero_for_identical_masks_and_positive_for_disjoint():
    from radstudio.operators_seg import _hd95

    mask = np.zeros((16, 16), dtype=bool)
    mask[4:12, 4:12] = True
    assert _hd95(mask, mask) == pytest.approx(0.0)
    shifted = np.zeros((16, 16), dtype=bool)
    shifted[0:4, 0:4] = True
    assert _hd95(mask, shifted) > 0.0
    assert _hd95(np.zeros((16, 16), dtype=bool), mask) is None


def _png(mask):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.fromarray(mask).save(buffer, format="PNG")
    return buffer.getvalue()


def _path(reference):
    from radstudio.artifact_boundary import parse_reference

    return parse_reference(reference)["path"]


def test_segmentation_evaluation_nsd(tmp_path):
    import radstudio.operators_seg  # noqa: F401
    from radstudio.operators_seg import _nsd

    mask = np.zeros((16, 16), dtype=bool)
    mask[4:12, 4:12] = True
    assert _nsd(mask, mask) == pytest.approx(1.0)

    shifted = np.zeros((16, 16), dtype=bool)
    shifted[4:12, 5:13] = True
    assert _nsd(shifted, mask, tolerance=2.0) > 0.9

    disjoint = np.zeros((16, 16), dtype=bool)
    disjoint[0:2, 0:2] = True
    assert _nsd(disjoint, mask) == pytest.approx(0.0)

    assert _nsd(np.zeros((16, 16), dtype=bool), mask) is None

    samples = _make_samples(tmp_path, count=1)
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    predicted = np.zeros((16, 16), dtype=np.uint8)
    predicted[4:12, 4:12] = 255
    reference = store.write_bytes("masks/i0.png", _png(predicted), "mask")
    index_ref = store.write_json(
        "mask-index.json",
        {"entries": [{"imageId": "i0", "file": _path(reference)}]},
        "mask_collection",
    )

    legacy = invoke(
        _request(
            "seg_eval_v10",
            "SegmentationEvaluation",
            {"bootstrap": 100},
            {"samples": samples_ref, "masks": index_ref},
            version="1.0.0",
        ),
        store,
    )
    assert legacy.status == "completed", legacy.error
    legacy_metrics = store.read_json(legacy.outputs["metrics"])
    # 1.0.0 的输出结构必须保持不变，不得新增 nsd 字段
    assert "nsd" not in legacy_metrics

    current = invoke(
        _request(
            "seg_eval_v11",
            "SegmentationEvaluation",
            {"bootstrap": 100, "nsdTolerance": 2.0},
            {"samples": samples_ref, "masks": index_ref},
            version="1.1.0",
        ),
        store,
    )
    assert current.status == "completed", current.error
    metrics = store.read_json(current.outputs["metrics"])
    assert "nsd" in metrics
    assert metrics["nsd"]["mean"] == pytest.approx(1.0)
    assert metrics["dice"]["mean"] == pytest.approx(1.0)
    assert store.read_json(current.outputs["report"])["summary"]["nsdTolerance"] == 2.0


def test_medsam_operator_registered():
    import radstudio.operators_seg  # noqa: F401
    from radstudio.operator import resolve_operator

    cls = resolve_operator("MedSAMInference", "1.0.0")
    assert cls.operator_type == "MedSAMInference"
    assert cls.operator_version == "1.0.0"
    assert cls.__name__ == "MedSAMInferenceOperator"


def test_medsam_inference_with_stub_predictor(tmp_path, monkeypatch):
    import radstudio.operators_seg as seg

    calls = {}

    class StubPredictor:
        def predict(self, image_rgb, box):
            calls["shape"] = image_rgb.shape
            calls["dtype"] = image_rgb.dtype
            calls["box"] = list(box)
            size = image_rgb.shape[0]
            return np.full((size, size), 0.9, dtype=np.float32)

    monkeypatch.setattr(seg, "_build_predictor", lambda path, device: StubPredictor())

    samples = _make_samples(tmp_path, count=2)
    np.save(tmp_path / "roi1.npy", np.zeros((16, 16), dtype=np.uint8))
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    result = invoke(
        _request(
            "medsam_infer",
            "MedSAMInference",
            {"imageSize": 512, "boxMargin": 0.1, "threshold": 0.0},
            {"samples": samples_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "completed", result.error
    index = store.read_json(result.outputs["masks"])
    assert index["count"] == 1
    entry = index["entries"][0]
    assert entry["imageId"] == "i0"
    assert 0.0 <= entry["confidence"] <= 1.0
    assert 0.0 <= entry["foregroundRatio"] <= 1.0
    assert entry["fileRef"].startswith("file:masks/i0.png")
    assert calls["shape"] == (512, 512, 3)
    assert calls["dtype"] == np.uint8
    assert calls["box"][0] < calls["box"][2]
    assert calls["box"][1] < calls["box"][3]

    from PIL import Image

    with Image.open(store.resolve_path(entry["file"])) as image:
        mask = np.asarray(image)
    # 概率图必须缩放回原图尺寸
    assert mask.shape == (16, 16)

    report = store.read_json(result.outputs["report"])
    assert report["total"] == 2
    assert report["succeeded"] == 1
    assert report["blocked"] == [{"imageId": "i1", "reason": "emptyGroundTruth"}]


def test_medsam_inference_blocks_missing_roi_without_failure(tmp_path):
    import radstudio.operators_seg  # noqa: F401

    samples = _make_samples(tmp_path, count=1)
    del samples[0]["roiPath"]
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    result = invoke(
        _request(
            "medsam_missing_roi",
            "MedSAMInference",
            {"imageSize": 512},
            {"samples": samples_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report["succeeded"] == 0
    assert report["blocked"] == [{"imageId": "i0", "reason": "missingRoiPath"}]
    assert store.read_json(result.outputs["masks"])["count"] == 0


def test_medsam_inference_fails_on_shape_mismatch(tmp_path):
    import radstudio.operators_seg  # noqa: F401

    samples = _make_samples(tmp_path, count=1)
    np.save(tmp_path / "roi0.npy", np.zeros((8, 8), dtype=np.uint8))
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    result = invoke(
        _request(
            "medsam_shape",
            "MedSAMInference",
            {"imageSize": 512},
            {"samples": samples_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "failed"
    assert "mismatch" in result.error.message


def test_medsam_inference_reports_missing_checkpoint(tmp_path, monkeypatch):
    import radstudio.operators_seg as seg

    # 恢复真实 _build_predictor：缺依赖/权重时必须以 failed 结束并给出指引，
    # 但不能真正 import segment_anything（环境里本来就没有安装）。
    monkeypatch.setattr(seg, "_build_predictor", seg._build_predictor)

    samples = _make_samples(tmp_path, count=1)
    store = ArtifactStore(tmp_path / "workspace")
    samples_ref = store.write_json("samples.json", samples, "sample_collection")
    result = invoke(
        _request(
            "medsam_checkpoint",
            "MedSAMInference",
            {"checkpointPath": "", "imageSize": 512},
            {"samples": samples_ref},
            version="1.0.0",
        ),
        store,
    )
    assert result.status == "failed"
    message = result.error.message
    assert "权重" in message or "segment_anything" in message

