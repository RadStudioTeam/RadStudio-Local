"""体数据处理节点验证（T9 验收）。

覆盖设计 §5 的处理节点契约：
  * 转换只改数值类型/缩放，并记录参数，不做隐式重采样；
  * 筛选、合并、掩膜配对、标签关联对被排除/重复/缺失/未命中项必须显式
    失败或写入诊断，禁止静默丢行；
  * 掩膜几何校验比较 shape/spacing/origin/direction，而不只是数组大小。
"""

from __future__ import annotations

import io
import json

import numpy as np

import radstudio.operators_volumes  # noqa: F401  注册算子
from radstudio.operator import BundleArtifactStore, OperatorRequest, invoke
from radstudio.operators_volumes import VolumeCollectionError, _require_collection

_GEOMETRY_DEFAULT = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def _npy(array) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, np.asarray(array, dtype=np.float32))
    return buffer.getvalue()


def _geometry(array, *, spacing=None, origin=None) -> dict:
    return {
        "shape": [int(value) for value in array.shape],
        "spacing": list(spacing or [1.0, 1.0, 1.0]),
        "origin": list(origin or [0.0, 0.0, 0.0]),
        "direction": list(_GEOMETRY_DEFAULT),
    }


def _entry(store, sample_id, array, *, prefix="input", patient_id=None, modality=None, geometry=None):
    ref = store.write_bytes(f"{prefix}/{sample_id}.npy", _npy(array), "volume")
    entry = {
        "sampleId": sample_id,
        "format": "dicom",
        "dataRef": ref,
        "geometry": geometry or _geometry(array),
    }
    if patient_id is not None:
        entry["patientId"] = patient_id
    if modality is not None:
        entry["modality"] = modality
    return entry


def _collection(store, entries, *, name="volumes.json", reader="read-dicom") -> str:
    payload = {
        "schemaVersion": "1",
        "reader": {"key": reader, "version": "1.0.0"},
        "volumes": entries,
        "diagnostics": [],
    }
    # 输入放在 inputs/ 下，避免与算子输出（工作区根目录 volumes.json）互相覆盖。
    return store.write_bytes(f"inputs/{name}", json.dumps(payload).encode("utf-8"), "volume_collection")


def _table(store, records, columns, *, name="table.json") -> str:
    payload = {"columns": list(columns), "records": records}
    return store.write_bytes(f"inputs/{name}", json.dumps(payload).encode("utf-8"), "table")


def _run(operator_type, store, *, inputs=None, input_sets=None, parameters=None):
    request = OperatorRequest(
        requestId="req-1",
        runId="run-1",
        nodeId="n1",
        operatorType=operator_type,
        parameters=parameters or {},
        inputs=inputs or {},
        inputSets=input_sets or {},
    )
    return invoke(request, store)


def _load(store, ref) -> np.ndarray:
    return np.load(io.BytesIO(store.read_bytes(ref)))


def test_volume_conversion_records_parameters_and_values(tmp_path):
    store = BundleArtifactStore(tmp_path / "ws")
    array = np.array([[[0.0, 1.0], [2.0, 3.0]]], dtype=np.float32)
    source = _collection(store, [_entry(store, "s1", array)])

    result = _run(
        "VolumeConversion",
        store,
        inputs={"volumes": source},
        parameters={"targetType": "int16", "scaling": "none"},
    )
    assert result.status == "completed", result.error
    converted = store.read_json(result.outputs["volumes"])
    entry = converted["volumes"][0]
    assert entry["conversion"] == {"targetType": "int16", "scaling": "none"}
    assert entry["geometry"] == _geometry(array)
    values = _load(store, entry["dataRef"])
    assert values.dtype == np.int16
    assert values.tolist() == [[[0, 1], [2, 3]]]

    scaled = _run(
        "VolumeConversion",
        store,
        inputs={"volumes": result.outputs["volumes"]},
        parameters={"targetType": "float32", "scaling": "minmax"},
    )
    assert scaled.status == "completed", scaled.error
    volume = store.read_json(scaled.outputs["volumes"])["volumes"][0]
    values = _load(store, volume["dataRef"])
    assert float(values.min()) == 0.0
    assert float(values.max()) == 1.0


def test_volume_conversion_rejects_unknown_parameters(tmp_path):
    store = BundleArtifactStore(tmp_path / "ws")
    source = _collection(store, [_entry(store, "s1", np.zeros((2, 2, 2), dtype=np.float32))])

    unknown = _run(
        "VolumeConversion",
        store,
        inputs={"volumes": source},
        parameters={"scaling": "histogram"},
    )
    assert unknown.status == "failed"
    assert "不支持" in str(unknown.error)

    bad_key = _run(
        "VolumeConversion",
        store,
        inputs={"volumes": source},
        parameters={"resample": True},
    )
    assert bad_key.status == "failed"
    assert "未知参数" in str(bad_key.error)


def test_volume_collection_invalid_schema(tmp_path):
    try:
        _require_collection({"schemaVersion": "2", "volumes": []}, "volumes")
    except VolumeCollectionError as exc:
        assert exc.code == "VOLUME_COLLECTION_INVALID"
    else:  # pragma: no cover - 结构错误必须拒绝
        raise AssertionError("未知 schemaVersion 必须报错")


def test_volume_filter_requires_explicit_exclusion_handling(tmp_path):
    store = BundleArtifactStore(tmp_path / "ws")
    source = _collection(
        store,
        [
            _entry(store, "keep", np.zeros((5, 2, 2), dtype=np.float32)),
            _entry(store, "short", np.zeros((2, 2, 2), dtype=np.float32)),
        ],
    )

    failed = _run("VolumeFilter", store, inputs={"volumes": source}, parameters={"minSlices": 3})
    assert failed.status == "failed"
    assert "short" in str(failed.error)

    reported = _run(
        "VolumeFilter",
        store,
        inputs={"volumes": source},
        parameters={"minSlices": 3, "onExcluded": "report"},
    )
    assert reported.status == "completed", reported.error
    output = store.read_json(reported.outputs["volumes"])
    assert [item["sampleId"] for item in output["volumes"]] == ["keep"]
    diagnostics = output["diagnostics"]
    assert any(
        item["code"] == "VOLUME_EXCLUDED" and item.get("sampleId") == "short"
        for item in diagnostics
    ), "被排除样本必须写入诊断，不能静默丢行"


def test_volume_filter_modality_and_mask_requirement(tmp_path):
    store = BundleArtifactStore(tmp_path / "ws")
    source = _collection(
        store,
        [
            _entry(store, "ct", np.zeros((4, 2, 2), dtype=np.float32), modality="CT"),
            _entry(store, "mr", np.zeros((4, 2, 2), dtype=np.float32), modality="MR"),
        ],
    )
    reported = _run(
        "VolumeFilter",
        store,
        inputs={"volumes": source},
        parameters={"modalities": ["CT"], "requireMask": True, "onExcluded": "report"},
    )
    assert reported.status == "completed", reported.error
    output = store.read_json(reported.outputs["volumes"])
    # CT 缺少掩膜、MR 模态不符，两者都不能留下
    assert output["volumes"] == []
    messages = "；".join(item["message"] for item in output["diagnostics"])
    assert "CT" in messages and "掩膜" in messages
    assert "MR" in messages


def test_volume_merge_duplicate_and_geometry_policies(tmp_path):
    store = BundleArtifactStore(tmp_path / "ws")
    first = _collection(store, [_entry(store, "s1", np.zeros((3, 2, 2), dtype=np.float32))], name="a.json")
    duplicate = _collection(
        store, [_entry(store, "s1", np.ones((3, 2, 2), dtype=np.float32), prefix="b")], name="b.json"
    )

    failed = _run("VolumeMerge", store, input_sets={"volumes": [first, duplicate]})
    assert failed.status == "failed"
    assert "重复 sampleId" in str(failed.error)

    merged = _run(
        "VolumeMerge",
        store,
        input_sets={"volumes": [first, duplicate]},
        parameters={"onDuplicateSampleId": "keep_first"},
    )
    assert merged.status == "completed", merged.error
    output = store.read_json(merged.outputs["volumes"])
    assert [item["sampleId"] for item in output["volumes"]] == ["s1"]
    assert any(item["code"] == "VOLUME_DUPLICATE_SAMPLE_ID" for item in output["diagnostics"])

    spaced = _collection(
        store,
        [
            _entry(
                store,
                "s2",
                np.zeros((3, 2, 2), dtype=np.float32),
                prefix="c",
                geometry=_geometry(np.zeros((3, 2, 2)), spacing=[2.0, 1.0, 1.0]),
            )
        ],
        name="c.json",
    )
    mismatch = _run(
        "VolumeMerge",
        store,
        input_sets={"volumes": [first, spaced]},
        parameters={"requireSameGeometry": True},
    )
    assert mismatch.status == "failed"
    assert "几何" in str(mismatch.error)


def test_pair_masks_pairs_geometry_and_reports_gaps(tmp_path):
    store = BundleArtifactStore(tmp_path / "ws")
    image_array = np.zeros((3, 2, 2), dtype=np.float32)
    volumes = _collection(
        store,
        [
            _entry(store, "s1", image_array, patient_id="p1"),
            _entry(store, "s2", np.zeros((3, 2, 2), dtype=np.float32), prefix="v", patient_id="p2"),
        ],
        name="volumes.json",
    )
    complete_masks = _collection(
        store,
        [
            _entry(store, "s1", np.ones((3, 2, 2), dtype=np.float32), prefix="m"),
            _entry(store, "s2", np.ones((3, 2, 2), dtype=np.float32), prefix="m"),
        ],
        name="masks-ok.json",
    )
    paired = _run(
        "PairMasks",
        store,
        inputs={"volumes": volumes, "masks": complete_masks},
        parameters={"pairBy": "sampleId"},
    )
    assert paired.status == "completed", paired.error
    output = store.read_json(paired.outputs["volumes"])
    assert [item["sampleId"] for item in output["volumes"]] == ["s1", "s2"]
    assert output["volumes"][0]["maskSampleId"] == "s1"
    assert output["volumes"][0]["maskRef"]

    partial_masks = _collection(
        store,
        [
            _entry(store, "s1", np.ones((3, 2, 2), dtype=np.float32), prefix="m"),
            _entry(store, "mask-extra", np.ones((3, 2, 2), dtype=np.float32), prefix="m"),
        ],
        name="masks-partial.json",
    )
    missing = _run("PairMasks", store, inputs={"volumes": volumes, "masks": partial_masks})
    assert missing.status == "failed"
    assert "缺少掩膜" in str(missing.error)

    reported = _run(
        "PairMasks",
        store,
        inputs={"volumes": volumes, "masks": partial_masks},
        parameters={"onMissingMask": "report", "onExtraMask": "ignore_report"},
    )
    assert reported.status == "completed", reported.error
    output = store.read_json(reported.outputs["volumes"])
    assert [item["sampleId"] for item in output["volumes"]] == ["s1"]
    codes = {item["code"] for item in output["diagnostics"]}
    assert {"MASK_MISSING", "MASK_UNMATCHED"} <= codes

    mismatched_shape = np.ones((4, 2, 2), dtype=np.float32)
    bad_geometry = _collection(
        store,
        [
            _entry(
                store,
                "s1",
                mismatched_shape,
                prefix="bad",
                geometry=_geometry(mismatched_shape, spacing=[9.0, 9.0, 9.0]),
            )
        ],
        name="masks-bad.json",
    )
    mismatch = _run(
        "PairMasks",
        store,
        inputs={"volumes": volumes, "masks": bad_geometry},
        parameters={"onMissingMask": "report"},
    )
    assert mismatch.status == "failed"
    assert "几何不符" in str(mismatch.error)

    duplicate_masks = _collection(
        store,
        [
            _entry(store, "mask-a", np.ones((3, 2, 2), dtype=np.float32), prefix="d", patient_id="p1"),
            _entry(store, "mask-b", np.ones((3, 2, 2), dtype=np.float32), prefix="d", patient_id="p1"),
        ],
        name="masks-dup.json",
    )
    duplicate = _run(
        "PairMasks",
        store,
        inputs={"volumes": volumes, "masks": duplicate_masks},
        parameters={"pairBy": "patientId"},
    )
    assert duplicate.status == "failed"
    assert "重复" in str(duplicate.error)


def test_label_association_explicit_policies(tmp_path):
    store = BundleArtifactStore(tmp_path / "ws")
    volumes = _collection(
        store,
        [
            _entry(store, "v1", np.zeros((2, 2, 2), dtype=np.float32), patient_id="p1"),
            _entry(store, "v2", np.zeros((2, 2, 2), dtype=np.float32), prefix="v", patient_id="p2"),
        ],
        name="volumes.json",
    )
    table = _table(
        store,
        [
            {"patientId": "p1", "label": "tumor"},
            {"patientId": "p2", "label": None},
            {"patientId": "p3", "label": "normal"},
        ],
        ["patientId", "label"],
    )

    strict = _run("LabelAssociation", store, inputs={"volumes": volumes, "table": table})
    assert strict.status == "failed"
    assert "空" in str(strict.error)

    reported = _run(
        "LabelAssociation",
        store,
        inputs={"volumes": volumes, "table": table},
        parameters={"onMissingLabel": "report", "onUnmatchedRow": "report"},
    )
    assert reported.status == "completed", reported.error
    output = store.read_json(reported.outputs["volumes"])
    labels = {item["sampleId"]: item.get("label") for item in output["volumes"]}
    assert labels == {"v1": "tumor", "v2": None}
    codes = {item["code"] for item in output["diagnostics"]}
    assert {"LABEL_EMPTY", "LABEL_UNMATCHED_ROW"} <= codes

    duplicate_table = _table(
        store,
        [{"patientId": "p1", "label": "a"}, {"patientId": "p1", "label": "b"}],
        ["patientId", "label"],
        name="table-dup.json",
    )
    duplicated = _run(
        "LabelAssociation",
        store,
        inputs={"volumes": volumes, "table": duplicate_table},
        parameters={"onUnmatchedRow": "report", "onMissingLabel": "report"},
    )
    assert duplicated.status == "failed"
    assert "重复" in str(duplicated.error)

    missing_column = _table(store, [{"patientId": "p1"}], ["patientId"], name="table-nolabel.json")
    missing = _run("LabelAssociation", store, inputs={"volumes": volumes, "table": missing_column})
    assert missing.status == "failed"
    assert "标签列" in str(missing.error)
