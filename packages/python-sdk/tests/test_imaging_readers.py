"""DICOM / NIfTI 读取器验证（T9，验收场景 10）。

用合成夹具覆盖：序列排序、混序列、缺片、几何不一致、四维选取与
体积序号越界，以及读取节点在 serve 模式下的产物登记。
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import struct
from pathlib import Path

import numpy as np
import pytest

from radstudio import data_readers as dr
from radstudio.operator import BundleArtifactStore, OperatorRequest, invoke

_VR_32 = {"OB", "OW", "OF", "OD", "OL", "OV", "SQ", "UT", "UN", "UC", "UR", "SV", "UV"}


def _elem(group: int, elem: int, vr: str, value: bytes) -> bytes:
    if len(value) % 2:
        value += b"\x00"
    if vr in _VR_32:
        header = struct.pack("<HH", group, elem) + vr.encode("ascii") + b"\x00\x00" + struct.pack("<I", len(value))
    else:
        header = struct.pack("<HH", group, elem) + vr.encode("ascii") + struct.pack("<H", len(value))
    return header + value


def _text(value: str) -> bytes:
    return value.encode("ascii")


def dicom_slice(
    *,
    series_uid: str = "1.2.3.4.1",
    study_uid: str = "1.2.3.4",
    instance: int = 1,
    position: float = 0.0,
    rows: int = 4,
    columns: int = 4,
    spacing: float = 0.5,
    fill: int = 1,
) -> bytes:
    out = bytearray(128)
    out += b"DICM"
    out += _elem(0x0002, 0x0010, "UI", _text("1.2.840.10008.1.2.1"))
    out += _elem(0x0008, 0x0016, "UI", _text("1.2.840.10008.5.1.4.1.1.2"))
    out += _elem(0x0008, 0x0060, "CS", _text("CT"))
    out += _elem(0x0020, 0x000D, "UI", _text(study_uid))
    out += _elem(0x0020, 0x000E, "UI", _text(series_uid))
    out += _elem(0x0020, 0x0013, "IS", _text(str(instance)))
    out += _elem(0x0020, 0x0032, "DS", _text(f"0\\0\\{position:g}"))
    out += _elem(0x0020, 0x0037, "DS", _text("1\\0\\0\\0\\1\\0"))
    out += _elem(0x0028, 0x0010, "US", struct.pack("<H", rows))
    out += _elem(0x0028, 0x0011, "US", struct.pack("<H", columns))
    out += _elem(0x0028, 0x0030, "DS", _text(f"{spacing:g}\\{spacing:g}"))
    out += _elem(0x0028, 0x0100, "US", struct.pack("<H", 16))
    out += _elem(0x0028, 0x0103, "US", struct.pack("<H", 0))
    out += _elem(0x0028, 0x0002, "US", struct.pack("<H", 1))
    pixels = np.full((rows, columns), fill, dtype="<u2").tobytes()
    out += _elem(0x7FE0, 0x0010, "OW", pixels)
    return bytes(out)


def nifti_bytes(
    dims: tuple[int, ...],
    *,
    pixdim: tuple[float, ...] = (1.0, 1.0, 1.0),
    data: bytes | None = None,
    srow: tuple[float, ...] | None = None,
) -> bytes:
    header = bytearray(348)
    struct.pack_into("<i", header, 0, 348)
    dim_values = [dims[0], *dims[1:], *([1] * (8 - len(dims)))]
    struct.pack_into("<8h", header, 40, *dim_values)
    struct.pack_into("<8f", header, 76, 1.0, *pixdim, *([1.0] * (8 - 1 - len(pixdim))))
    struct.pack_into("<h", header, 70, 2)  # DT_UINT8
    struct.pack_into("<h", header, 72, 8)
    struct.pack_into("<f", header, 108, 352.0)
    struct.pack_into("<f", header, 112, 1.0)
    struct.pack_into("<f", header, 116, 0.0)
    if srow is not None:
        struct.pack_into("<h", header, 254, 1)
        struct.pack_into("<12f", header, 280, *srow)
    header[344:348] = b"n+1\x00"
    count = int(np.prod(dims[1:]))
    payload = data if data is not None else bytes(range(1, 1 + count % 250)) * (count // 250 + 1)
    return bytes(header) + b"\x00\x00\x00\x00" + payload[:count]


def _manifest(rows, files, reader_key="read-dicom"):
    return {
        "schemaVersion": "1",
        "reader": {"key": reader_key, "version": "1.0.0"},
        "collection": {"id": "col-1", "revision": 1},
        "config": {},
        "files": files,
        "rows": rows,
    }


def _write(tmp_path, files, name, data):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    files[name] = {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
    return name


def _resolve(tmp_path):
    return dr.make_resolver(tmp_path, verify_sha256=True)


# ---------------------------------------------------------------------------
# DICOM
# ---------------------------------------------------------------------------


def test_read_dicom_sorts_by_position_and_builds_volume(tmp_path):
    files: dict = {}
    ids = [
        _write(tmp_path, files, "z2.dcm", dicom_slice(instance=2, position=1.0, fill=2)),
        _write(tmp_path, files, "z0.dcm", dicom_slice(instance=1, position=0.0, fill=1)),
        _write(tmp_path, files, "z1.dcm", dicom_slice(instance=3, position=2.0, fill=3)),
    ]
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {"patientId": "p1"}, "assets": [{"role": "dicom", "fileId": fid} for fid in ids]}],
        files,
    )
    result = dr.read_dicom(manifest, {}, resolve=_resolve(tmp_path))
    volume = result["volumes"][0]
    assert volume["sampleId"] == "case1"
    assert volume["patientId"] == "p1"
    assert volume["geometry"]["shape"] == [3, 4, 4]
    assert volume["geometry"]["spacing"] == [1.0, 0.5, 0.5]
    assert result["diagnostics"] == []
    array = volume["array"]
    assert array.shape == (3, 4, 4)
    assert [float(array[z, 0, 0]) for z in range(3)] == [1.0, 2.0, 3.0]


def test_read_dicom_uses_original_file_name_from_snapshot(tmp_path):
    """快照对象键是内容寻址的：扩展名只能来自 manifest.fileName。"""
    files: dict = {}
    ids = []
    for index, position in enumerate((0.0, 1.0, 2.0)):
        payload = dicom_slice(instance=index + 1, position=position, fill=index + 1)
        key = hashlib.sha256(payload).hexdigest()
        (tmp_path / key).write_bytes(payload)
        files[key] = {"path": key, "fileName": f"slice-{index}.dcm", "sha256": key}
        ids.append(key)
    manifest = _manifest(
        [
            {
                "rowId": "r1",
                "sampleId": "case1",
                "fields": {"patientId": "p1"},
                "assets": [{"role": "dicom", "fileId": fid} for fid in ids],
            }
        ],
        files,
    )
    result = dr.read_dicom(manifest, {}, resolve=_resolve(tmp_path))
    volume = result["volumes"][0]
    assert volume["geometry"]["shape"] == [3, 4, 4]
    assert volume["geometry"]["sliceCount"] == 3


def test_read_dicom_rejects_key_without_original_file_name(tmp_path):
    files: dict = {}
    payload = dicom_slice()
    key = hashlib.sha256(payload).hexdigest()
    (tmp_path / key).write_bytes(payload)
    files[key] = {"path": key, "sha256": key}
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "dicom", "fileId": key}]}],
        files,
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_dicom(manifest, {}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "FILE_EXTENSION_UNSUPPORTED"


def test_readers_reject_mismatched_snapshot_reader(tmp_path):
    files: dict = {}
    fid = _write(tmp_path, files, "case.nii", nifti_bytes((4, 4, 3)))
    rows = [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "nifti", "fileId": fid}]}]
    images_manifest = _manifest(rows, files, reader_key="read-images")
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_nifti(images_manifest, {}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "READER_KEY_MISMATCH"
    assert "read-images" in str(excinfo.value)

    dicom_manifest = _manifest(rows, files, reader_key="read-nifti")
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_dicom(dicom_manifest, {}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "READER_KEY_MISMATCH"


def test_read_dicom_detects_mixed_series(tmp_path):
    files: dict = {}
    ids = [
        _write(tmp_path, files, "a.dcm", dicom_slice(series_uid="1.2.3.4.1", instance=1, position=0.0)),
        _write(tmp_path, files, "b.dcm", dicom_slice(series_uid="1.2.3.4.9", instance=2, position=1.0)),
    ]
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "dicom", "fileId": fid} for fid in ids]}],
        files,
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_dicom(manifest, {}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "DICOM_MIXED_SERIES"

    # 显式关闭单序列约束后允许读取，但仍记录序列 UID。
    result = dr.read_dicom(manifest, {"requireSingleSeries": False}, resolve=_resolve(tmp_path))
    assert result["volumes"][0]["geometry"]["sliceCount"] == 2


def test_read_dicom_missing_slices_error_and_report(tmp_path):
    files: dict = {}
    ids = [
        _write(tmp_path, files, "i1.dcm", dicom_slice(instance=1, position=0.0)),
        _write(tmp_path, files, "i3.dcm", dicom_slice(instance=3, position=2.0)),
    ]
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "dicom", "fileId": fid} for fid in ids]}],
        files,
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_dicom(manifest, {"sortBy": "instance_number"}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "DICOM_MISSING_SLICES"
    assert "2" in str(excinfo.value)

    result = dr.read_dicom(
        manifest,
        {"sortBy": "instance_number", "onMissingSlices": "report"},
        resolve=_resolve(tmp_path),
    )
    assert [d["code"] for d in result["diagnostics"]] == ["DICOM_MISSING_SLICES"]
    assert result["diagnostics"][0]["severity"] == "warning"


def test_read_dicom_geometry_mismatch_is_rejected(tmp_path):
    files: dict = {}
    ids = [
        _write(tmp_path, files, "g1.dcm", dicom_slice(instance=1, rows=4, columns=4)),
        _write(tmp_path, files, "g2.dcm", dicom_slice(instance=2, rows=5, columns=4, position=1.0)),
    ]
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "dicom", "fileId": fid} for fid in ids]}],
        files,
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_dicom(manifest, {}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "DICOM_GEOMETRY_INCONSISTENT"


def test_read_dicom_requires_role_and_rejects_bad_extension(tmp_path):
    files: dict = {}
    bad = _write(tmp_path, files, "slice.txt", b"not dicom")
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "dicom", "fileId": bad}]}],
        files,
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_dicom(manifest, {}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "FILE_EXTENSION_UNSUPPORTED"

    manifest = _manifest([{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": []}], files)
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_dicom(manifest, {}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "ROW_MISSING_ROLE"


# ---------------------------------------------------------------------------
# NIfTI
# ---------------------------------------------------------------------------


def test_read_nifti_3d_and_spatial_metadata(tmp_path):
    files: dict = {}
    fid = _write(
        tmp_path,
        files,
        "case.nii",
        nifti_bytes((3, 4, 5, 6), pixdim=(2.0, 1.5, 1.5), srow=(1.5, 0, 0, 10, 0, 1.5, 0, 20, 0, 0, 2, 30)),
    )
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {"patientId": "p1"}, "assets": [{"role": "nifti", "fileId": fid}]}],
        files,
        reader_key="read-nifti",
    )
    result = dr.read_nifti(manifest, {}, resolve=_resolve(tmp_path))
    volume = result["volumes"][0]
    assert volume["geometry"]["shape"] == [6, 5, 4]  # numpy (z,y,x)
    assert volume["geometry"]["spacing"] == [1.5, 1.5, 2.0]
    assert volume["geometry"]["origin"] == [10.0, 20.0, 30.0]
    assert volume["array"].shape == (6, 5, 4)


def test_read_nifti_4d_requires_explicit_selection(tmp_path):
    files: dict = {}
    fid = _write(tmp_path, files, "case4d.nii", nifti_bytes((4, 2, 2, 2, 3)))
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "nifti", "fileId": fid}]}],
        files,
        reader_key="read-nifti",
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_nifti(manifest, {}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "NIFTI_4D_NOT_ALLOWED"

    result = dr.read_nifti(manifest, {"allow4D": True, "volumeIndex": 2}, resolve=_resolve(tmp_path))
    assert result["volumes"][0]["geometry"]["shape"] == [2, 2, 2]

    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_nifti(manifest, {"allow4D": True, "volumeIndex": 3}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "NIFTI_VOLUME_INDEX_OUT_OF_RANGE"


def test_read_nifti_gzip_and_magic_mismatch(tmp_path):
    files: dict = {}
    gz = gzip.compress(nifti_bytes((3, 2, 2, 2)))
    fid = _write(tmp_path, files, "case.nii.gz", gz)
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "nifti", "fileId": fid}]}],
        files,
        reader_key="read-nifti",
    )
    assert dr.read_nifti(manifest, {}, resolve=_resolve(tmp_path))["volumes"][0]["geometry"]["shape"] == [2, 2, 2]

    files = {}
    bad = _write(tmp_path, files, "bad.nii", b"\x00" * 400)
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "nifti", "fileId": bad}]}],
        files,
        reader_key="read-nifti",
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_nifti(manifest, {}, resolve=_resolve(tmp_path))
    assert excinfo.value.code == "NIFTI_MAGIC_MISMATCH"


def test_read_nifti_gzip_from_content_addressed_workspace(tmp_path):
    """执行端对象键是 sha256（无扩展名）：gzip 必须按魔数识别。"""
    files: dict = {}
    payload = gzip.compress(nifti_bytes((3, 2, 2, 2)))
    key = hashlib.sha256(payload).hexdigest()
    (tmp_path / key).write_bytes(payload)
    files[key] = {"path": key, "fileName": "case.nii.gz", "sha256": key}
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "nifti", "fileId": key}]}],
        files,
        reader_key="read-nifti",
    )
    volume = dr.read_nifti(manifest, {}, resolve=dr.make_resolver(tmp_path, verify_sha256=True))["volumes"][0]
    assert volume["geometry"]["shape"] == [2, 2, 2]


# ---------------------------------------------------------------------------
# 读取节点（serve 模式）
# ---------------------------------------------------------------------------


def test_read_dicom_operator_writes_volume_artifacts(tmp_path):
    import radstudio.operators_readers  # noqa: F401

    workspace = tmp_path / "workspace"
    store = BundleArtifactStore(workspace)
    files = {}
    assets = []
    for index, fill in enumerate((1, 2, 3)):
        data = dicom_slice(instance=index + 1, position=float(index), fill=fill)
        rel = store.write_local(f"dicom/{index}.dcm", data)
        files[rel] = {"path": rel, "sha256": hashlib.sha256(data).hexdigest()}
        assets.append({"role": "dicom", "fileId": rel})
    manifest = _manifest([{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": assets}], files)
    request = OperatorRequest(
        requestId="req-dicom",
        runId="run-1",
        nodeId="n1",
        operatorType="ReadDicom",
        parameters={},
        inputs={},
        environment={"inputManifest": manifest, "workspaceRoot": str(workspace)},
    )
    result = invoke(request, store)
    assert result.status == "completed", result.error
    collection = store.read_json(result.outputs["volumes"])
    assert collection["schemaVersion"] == "1"
    entry = collection["volumes"][0]
    assert entry["geometry"]["shape"] == [3, 4, 4]
    array = np.load(io.BytesIO(store.read_bytes(entry["dataRef"])))
    assert array.shape == (3, 4, 4)
    assert float(array[1, 0, 0]) == 2.0


def test_runner_runs_read_dicom_node_from_snapshot_manifest(tmp_path):
    """部署执行端同一入口（单体 runner）执行 ReadDicom：输入快照 manifest 经
    snapshotInputs 解析、参数取 manifest.config 覆盖 IR 绑定字段，产物登记为
    volume_collection。"""
    from radstudio import runner

    workspace = tmp_path / "input"
    files: dict = {}
    assets = []
    for index, fill in enumerate((1, 2, 3)):
        data = dicom_slice(instance=index + 1, position=float(index), fill=fill)
        target = workspace / "objects" / "dicom" / f"{index}.dcm"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        name = f"objects/dicom/{index}.dcm"
        # 生产环境快照文件挂在 /input 下；测试用绝对路径绕开挂载点，
        # 解析分支（相对路径拼 workspaceRoot）之外的逻辑完全一致。
        files[name] = {"path": str(target), "sha256": hashlib.sha256(data).hexdigest(), "byteSize": len(data)}
        assets.append({"role": "dicom", "fileId": name})
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": assets}], files
    )
    manifest["config"] = {
        "dicomRole": "dicom",
        "seriesGrouping": "series_instance_uid",
        "requireSingleSeries": True,
        "sortBy": "image_position",
        "onMissingSlices": "error",
        "maxSlicesPerSeries": 500,
        "patientField": "patientId",
    }
    manifest_path = workspace / "snapshot" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    manifest_path.write_bytes(raw)

    request = {
        "runId": "run-read-dicom",
        "ir": {
            "schemaVersion": "2",
            "nodes": [
                {
                    "id": "read1",
                    "type": "ReadDicom",
                    "operatorVersion": "1.0.0",
                    "parameters": {"dataCollectionId": "col-1", "readerConfigId": "cfg-1"},
                }
            ],
            "edges": [],
        },
        "snapshotInputs": {
            "read1": {
                "manifestPath": str(manifest_path),
                "manifestSha256": hashlib.sha256(raw).hexdigest(),
            }
        },
    }
    output = tmp_path / "output"
    events = []
    results = runner.run(request, output, lambda kind, payload: events.append(kind))

    assert "read1" in results
    entries = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    volume = next(entry for entry in entries if entry["artifactType"] == "volume_collection")
    assert volume["nodeId"] == "read1"
    payload = json.loads((output / volume["path"]).read_text(encoding="utf-8"))
    assert payload["volumes"][0]["geometry"]["shape"] == [3, 4, 4]
    assert [float(value) for value in payload["volumes"][0]["geometry"]["spacing"]] == [1.0, 0.5, 0.5]
    assert "node.completed" in events


def test_runner_rejects_tampered_snapshot_manifest(tmp_path):
    """运行端必须核对快照 manifest 哈希：内容被改动时拒绝执行而不是静默读取。"""
    from radstudio import runner

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(b'{"schemaVersion":"1"}')
    request = {
        "runId": "run-tampered",
        "ir": {
            "schemaVersion": "2",
            "nodes": [
                {
                    "id": "read1",
                    "type": "ReadDicom",
                    "operatorVersion": "1.0.0",
                    "parameters": {"dataCollectionId": "col-1", "readerConfigId": "cfg-1"},
                }
            ],
            "edges": [],
        },
        "snapshotInputs": {
            "read1": {"manifestPath": str(manifest_path), "manifestSha256": "0" * 64}
        },
    }
    with pytest.raises(dr.ReaderError) as excinfo:
        runner.run(request, tmp_path / "output", lambda *_: None)
    assert excinfo.value.code == "MANIFEST_HASH_MISMATCH"


def test_reproduction_exports_snapshot_inputs_portably(tmp_path):
    """复现包只导出快照 manifest 声明的文件，使用相对路径与哈希，并保留读取器配置；
    replay 按清单校验后把 manifest 重写为本地绝对路径。"""
    from radstudio import replay, reproduction

    workspace = tmp_path / "input"
    files: dict = {}
    assets = []
    for index in range(2):
        data = b"slice-" + bytes([index])
        name = f"objects/dicom/{index}.dcm"
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[name] = {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
        assets.append({"role": "dicom", "fileId": name})
    # 未在 manifest 中登记的文件不得进入复现包输入清单。
    (workspace / "objects" / "dicom" / "hidden.dcm").write_bytes(b"hidden")
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": assets}], files
    )
    manifest["config"] = {"seriesGrouping": "series_instance_uid"}
    raw = json.dumps(manifest).encode("utf-8")
    manifest_path = workspace / "snapshot" / "input-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(raw)

    request = {
        "snapshotInputs": {
            "read1": {
                "manifestPath": str(manifest_path),
                "manifestSha256": hashlib.sha256(raw).hexdigest(),
                "readerConfigId": "cfg-1",
                "readerImplVersion": "1.0.0",
            }
        }
    }
    payloads: dict = {}
    hashes: list = []
    reproduction._export_snapshot_inputs(request, payloads, hashes)
    exported = {item["path"]: item["sha256"] for item in hashes}
    assert len(exported) == 2
    assert all(path.startswith("inputs/read1-") and path.endswith(".dcm") for path in exported)
    entry = request["snapshotInputs"]["read1"]
    assert entry["readerConfigId"] == "cfg-1"
    assert entry["readerImplVersion"] == "1.0.0"
    assert all(not path.startswith("/") for path in exported)

    bundle_manifest = json.loads(payloads[entry["manifestPath"]])
    assert bundle_manifest["config"] == manifest["config"]
    assert sorted(item["path"] for item in bundle_manifest["files"].values()) == sorted(exported)
    assert (
        hashlib.sha256(payloads[entry["manifestPath"]]).hexdigest() == entry["manifestSha256"]
    )
    target = tmp_path / entry["manifestPath"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payloads[entry["manifestPath"]])

    dataset_dir = tmp_path / "restore"
    for relative in exported:
        restored = dataset_dir / relative
        restored.parent.mkdir(parents=True, exist_ok=True)
        restored.write_bytes((workspace / "objects" / "dicom" / Path(relative).name).read_bytes())
    verified = {relative: dataset_dir / relative for relative in exported}
    consumed: set = set()
    replay._materialize_snapshot_inputs(
        tmp_path, request, verified, consumed, tmp_path / "materialized"
    )
    assert consumed == set(exported)
    materialized = json.loads(Path(entry["manifestPath"]).read_text(encoding="utf-8"))
    assert all(Path(item["path"]).is_absolute() for item in materialized["files"].values())
    assert (
        hashlib.sha256(
            json.dumps(
                materialized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        == entry["manifestSha256"]
    )
