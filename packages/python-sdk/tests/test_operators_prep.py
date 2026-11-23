"""数据准备算子：感知哈希近重复去重与 ROI 裁剪。

影像由 numpy 生成并直接存成 .npy（read_image 原生支持），不引入新依赖。
"""

import numpy as np
from PIL import Image

from radstudio.operator import ArtifactStore, OperatorRequest, invoke


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


def _blob(size=64):
    """平滑双高斯团块：低频能量集中，适合校验 pHash 对轻微噪声的稳定性。"""
    yy, xx = np.mgrid[0:size, 0:size].astype(float)
    return (
        120.0
        + 60.0 * np.exp(-((xx - 20) ** 2 + (yy - 20) ** 2) / (2 * 8.0**2))
        + 40.0 * np.exp(-((xx - 46) ** 2 + (yy - 44) ** 2) / (2 * 6.0**2))
    )


def _save(tmp_path, name, value):
    path = tmp_path / f"{name}.npy"
    np.save(path, value)
    return str(path)


def _dedup(store, samples, parameters=None):
    reference = store.write_json("inputs/samples.json", samples, "sample_collection")
    return invoke(
        _request(
            "dedup",
            "PerceptualHashDedup",
            parameters or {},
            {"samples": reference},
            version="1.0.0",
        ),
        store,
    )


def test_prep_operators_registered():
    import radstudio.operators_prep  # noqa: F401
    from radstudio.operator import resolve_operator

    for operator_type in ("PerceptualHashDedup", "ROICrop"):
        cls = resolve_operator(operator_type, "1.0.0")
        assert cls.operator_type == operator_type
        assert cls.operator_version == "1.0.0"


def test_phash_dedups_identical_near_duplicate_but_keeps_distinct(tmp_path):
    import radstudio.operators_prep  # noqa: F401

    base = _blob()
    noisy = base + np.random.default_rng(3).normal(0.0, 1.0, base.shape)
    striped = 100.0 + 80.0 * (np.sin(np.arange(64)[None, :] / 3.0) > 0)
    samples = [
        {"imageId": f"i{index}", "patientId": f"p{index}", "label": 0, "datasetId": "A",
         "imagePath": _save(tmp_path, name, value)}
        for index, (name, value) in enumerate(
            [("base", base), ("copy", base), ("noisy", noisy), ("striped", striped)]
        )
    ]
    # hashSize=8 对应 32×32 DCT 的低频 1/4 频段（标准 pHash 口径），对噪声最稳。
    store = ArtifactStore(tmp_path / "workspace")
    result = _dedup(store, samples, {"hashSize": 8})
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    kept = store.read_json(result.outputs["samples"])

    assert report["inputCount"] == 4
    assert report["parameters"] == {"hashSize": 8, "maxDistance": 6, "crossDataset": True}
    kept_ids = [sample["imageId"] for sample in kept]
    assert kept_ids == ["i0", "i3"]
    assert report["keptCount"] == 2
    assert report["duplicateCount"] == 2
    assert report["intraDatasetDuplicates"] == 2
    assert report["crossDatasetDuplicates"] == 0

    merged = {tuple(cluster["members"]) for cluster in report["clusters"]}
    assert ("i0", "i1", "i2") in merged
    distinct = [cluster for cluster in report["clusters"] if "i3" in cluster["members"]]
    assert len(distinct) == 1 and distinct[0]["members"] == ["i3"]

    for sample in kept:
        # 保留样本都要带可复现的感知哈希与去重簇标识
        assert isinstance(sample["perceptualHash"], str)
        int(sample["perceptualHash"], 16)
        assert sample["dedupClusterId"].startswith("c")

    cluster = next(item for item in report["clusters"] if item["representative"] == "i0")
    assert cluster["members"] == ["i0", "i1", "i2"]
    assert cluster["maxDistance"] <= 8
    assert cluster["datasets"] == ["A"]


def test_phash_defaults_dedup_identical_images(tmp_path):
    import radstudio.operators_prep  # noqa: F401

    image = _blob()
    samples = [
        {"imageId": name, "patientId": name, "label": 0,
         "imagePath": _save(tmp_path, name, image.copy())}
        for name in ("i0", "i1")
    ]
    store = ArtifactStore(tmp_path / "workspace")
    result = _dedup(store, samples)
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report["parameters"]["hashSize"] == 8
    assert report["keptCount"] == 1
    assert report["duplicateCount"] == 1
    assert len(store.read_json(result.outputs["samples"])) == 1


def test_phash_cross_dataset_switch(tmp_path):
    import radstudio.operators_prep  # noqa: F401

    image = _blob()
    samples = [
        {"imageId": f"i{index}", "patientId": f"p{index}", "label": 0,
         "datasetId": dataset, "imagePath": _save(tmp_path, f"img{index}", image.copy())}
        for index, dataset in enumerate(("A", "B"))
    ]
    store = ArtifactStore(tmp_path / "workspace")

    across = _dedup(store, samples, {"crossDataset": True})
    assert across.status == "completed", across.error
    report = store.read_json(across.outputs["report"])
    assert report["duplicateCount"] == 1
    assert report["crossDatasetDuplicates"] == 1
    assert report["intraDatasetDuplicates"] == 0
    assert report["clusters"][0]["datasets"] == ["A", "B"]

    within = _dedup(store, samples, {"crossDataset": False})
    assert within.status == "completed", within.error
    report = store.read_json(within.outputs["report"])
    assert report["duplicateCount"] == 0
    assert report["keptCount"] == 2


def test_phash_empty_input_and_unreadable_sample(tmp_path):
    import radstudio.operators_prep  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    empty = _dedup(store, [])
    assert empty.status == "completed", empty.error
    report = store.read_json(empty.outputs["report"])
    assert report["inputCount"] == 0
    assert report["keptCount"] == 0
    assert report["duplicateCount"] == 0
    assert report["clusters"] == []
    assert store.read_json(empty.outputs["samples"]) == []

    samples = [
        {"imageId": "ok", "patientId": "p0", "label": 0, "imagePath": _save(tmp_path, "ok", _blob())},
        {"imageId": "broken", "patientId": "p1", "label": 0, "imagePath": str(tmp_path / "absent.npy")},
    ]
    result = _dedup(store, samples)
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    # 读图失败的样本原样保留，不参与近重复合并
    assert report["unreadable"] == ["broken"]
    assert report["keptCount"] == 2
    assert report["duplicateCount"] == 0


def _crop(store, samples, parameters=None):
    reference = store.write_json("inputs/samples.json", samples, "sample_collection")
    return invoke(
        _request(
            "crop",
            "ROICrop",
            parameters or {},
            {"samples": reference},
            version="1.0.0",
        ),
        store,
    )


def _crop_samples(tmp_path, masks):
    """masks: [(imageId, roi array 或 None)]，影像统一 64×64 灰度 npz/npy。"""
    samples = []
    for index, (image_id, roi) in enumerate(masks):
        sample = {
            "imageId": image_id,
            "patientId": f"p{index}",
            "label": index % 2,
            "imagePath": _save(tmp_path, f"{image_id}-image", _blob()),
        }
        if roi is not None:
            sample["roiPath"] = _save(tmp_path, f"{image_id}-roi", roi)
        samples.append(sample)
    return samples


def test_roi_crop_updates_sample_and_matches_margin(tmp_path):
    import radstudio.operators_prep  # noqa: F401

    roi = np.zeros((64, 64), dtype=np.uint8)
    roi[16:32, 16:48] = 255
    samples = _crop_samples(tmp_path, [("i0", roi)])
    store = ArtifactStore(tmp_path / "workspace")
    result = _crop(store, samples, {"margin": 0.2, "minSize": 4})
    assert result.status == "completed", result.error

    report = store.read_json(result.outputs["report"])
    assert report["inputCount"] == 1
    assert report["croppedCount"] == 1
    assert report["blockedCount"] == 0
    assert report["parameters"] == {"margin": 0.2, "minSize": 4}
    # bbox 32×16，长边 32 → 外扩 6.4 像素后 [9,9,55,39]
    assert report["boxes"] == [
        {"imageId": "i0", "cropBox": [9, 9, 55, 39], "width": 46, "height": 30}
    ]

    cropped = store.read_json(result.outputs["samples"])[0]
    assert cropped["cropStatus"] == "cropped"
    assert cropped["cropBox"] == [9, 9, 55, 39]
    assert cropped["originalImagePath"].endswith("i0-image.npy")
    assert cropped["originalRoiPath"].endswith("i0-roi.npy")
    assert cropped["imagePath"] == "crops/i0-image.png"
    assert cropped["roiPath"] == "crops/i0-roi.png"

    with Image.open(store.resolve_path(cropped["imagePath"])) as image:
        assert np.asarray(image).shape == (30, 46)
    with Image.open(store.resolve_path(cropped["roiPath"])) as mask:
        values = np.asarray(mask)
    assert values.shape == (30, 46)
    assert set(np.unique(values).tolist()) <= {0, 255}
    assert (values > 0).any()


def test_roi_crop_blocks_empty_and_missing_roi(tmp_path):
    import radstudio.operators_prep  # noqa: F401

    empty = np.zeros((64, 64), dtype=np.uint8)
    samples = _crop_samples(tmp_path, [("i0", empty), ("i1", None)])
    samples.append(
        {
            "imageId": "i2",
            "patientId": "p2",
            "label": 0,
            "imagePath": _save(tmp_path, "i2-image", _blob()),
            "roiPath": str(tmp_path / "absent-roi.npy"),
        }
    )
    store = ArtifactStore(tmp_path / "workspace")
    result = _crop(store, samples)
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report["croppedCount"] == 0
    assert report["blockedCount"] == 3
    assert report["blocked"] == [
        {"imageId": "i0", "reason": "empty_roi"},
        {"imageId": "i1", "reason": "missing_roi"},
        {"imageId": "i2", "reason": "missing_roi"},
    ]
    blocked = {sample["imageId"]: sample for sample in store.read_json(result.outputs["samples"])}
    for image_id in ("i0", "i1", "i2"):
        assert blocked[image_id]["qcStatus"] == "blocked"
        assert blocked[image_id]["cropStatus"] in {"empty_roi", "missing_roi"}
    # 阻断样本保留原路径，不写裁剪文件
    assert blocked["i1"]["imagePath"].endswith("i1-image.npy")
    assert "roiPath" not in blocked["i1"]
    assert not (store.root / "crops").exists()


def test_roi_crop_clips_box_inside_image(tmp_path):
    import radstudio.operators_prep  # noqa: F401

    roi = np.zeros((64, 64), dtype=np.uint8)
    roi[56:64, 56:64] = 255
    samples = _crop_samples(tmp_path, [("edge", roi)])
    store = ArtifactStore(tmp_path / "workspace")
    result = _crop(store, samples, {"margin": 0.5})
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    box = report["boxes"][0]["cropBox"]
    assert box == [52, 52, 64, 64]
    assert 0 <= box[0] < box[2] <= 64 and 0 <= box[1] < box[3] <= 64


def test_roi_crop_tiny_roi_expands_to_min_size(tmp_path):
    import radstudio.operators_prep  # noqa: F401

    roi = np.zeros((64, 64), dtype=np.uint8)
    roi[30:32, 30:32] = 255
    samples = _crop_samples(tmp_path, [("tiny", roi)])
    store = ArtifactStore(tmp_path / "workspace")
    result = _crop(store, samples, {"margin": 0.0, "minSize": 16})
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    x0, y0, x1, y1 = report["boxes"][0]["cropBox"]
    assert x1 - x0 >= min(16, 64)
    assert y1 - y0 >= min(16, 64)
    assert 0 <= x0 and x1 <= 64 and 0 <= y0 and y1 <= 64


def test_roi_crop_empty_input(tmp_path):
    import radstudio.operators_prep  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    result = _crop(store, [])
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report["inputCount"] == 0
    assert report["croppedCount"] == 0
    assert report["blockedCount"] == 0
    assert report["blocked"] == [] and report["boxes"] == []
    assert store.read_json(result.outputs["samples"]) == []
