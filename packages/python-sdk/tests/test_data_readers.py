"""数据资源工作台读取器验证（T5 验收）。

覆盖：二维图片映射、临床表解析（主键/缺失值/重复键）、10x MEX 三件套、
H5AD 层选择、manifest 哈希核验，以及错误声明与缺件的稳定诊断码。
"""

from __future__ import annotations

import gzip
import hashlib
import io

import numpy as np
import pytest
from PIL import Image

from radstudio import data_readers as dr
from radstudio.operator import (
    BundleArtifactStore,
    OperatorRequest,
    invoke,
    parse_ref,
    resolve_operator,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _png_bytes(width=8, height=8, fill=7) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.full((height, width), fill, dtype=np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


def _manifest(rows, files):
    return {
        "schemaVersion": "1",
        "reader": {"key": "read-images", "version": "1.0.0"},
        "collection": {"id": "col-1", "revision": 3},
        "config": {},
        "files": files,
        "rows": rows,
    }


def _file_entry(tmp_path, files, name, data) -> tuple[str, dict]:
    """把合成文件写到工作区并登记 manifest 条目；返回 (fileId, entry)。"""
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    entry = {"path": name, "sha256": _sha(data)}
    files[name] = entry
    return name, entry


def _resolve_local(root):
    return dr.make_resolver(root, verify_sha256=False)


# ---------------------------------------------------------------------------
# 二维图片
# ---------------------------------------------------------------------------


def test_read_images_maps_samples_and_patient_fallback(tmp_path):
    files = {}
    _, img1 = _file_entry(tmp_path, files, "case1.png", _png_bytes())
    _, img2 = _file_entry(tmp_path, files, "case2.png", _png_bytes())
    _, roi1 = _file_entry(tmp_path, files, "case1_roi.png", _png_bytes(fill=255))
    manifest = _manifest(
        [
            {"rowId": "r1", "sampleId": "case1", "fields": {"patientId": "p1"}, "assets": [{"role": "image", "fileId": img1["path"]}, {"role": "roi", "fileId": roi1["path"]}]},
            {"rowId": "r2", "sampleId": "case2", "fields": {}, "assets": [{"role": "image", "fileId": img2["path"]}]},
        ],
        files,
    )
    samples = dr.read_images(manifest, {"roiRole": "roi"}, resolve=_resolve_local(tmp_path))
    assert samples == [
        {"imageId": "case1", "patientId": "p1", "label": None, "imagePath": str(tmp_path / "case1.png"), "roiPath": str(tmp_path / "case1_roi.png")},
        {"imageId": "case2", "patientId": "case2", "label": None, "imagePath": str(tmp_path / "case2.png"), "roiPath": ""},
    ]


def test_read_images_requires_roi_and_rejects_multi_image(tmp_path):
    files = {}
    _, img = _file_entry(tmp_path, files, "case1.png", _png_bytes())
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "image", "fileId": img["path"]}]}],
        files,
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_images(manifest, {"requireRoi": True, "roiRole": "roi"}, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "ROW_MISSING_ROLE"
    assert excinfo.value.row_id == "r1"

    _, img2 = _file_entry(tmp_path, files, "case1b.png", _png_bytes(fill=9))
    manifest["rows"][0]["assets"].append({"role": "image", "fileId": img2["path"]})
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_images(manifest, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "ROW_ROLE_OVERFLOW"


def test_read_images_missing_role_and_unknown_config(tmp_path):
    files = {}
    _, img = _file_entry(tmp_path, files, "case1.png", _png_bytes())
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": []}],
        files,
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_images(manifest, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "ROW_MISSING_ROLE"

    manifest["rows"][0]["assets"].append({"role": "image", "fileId": img["path"]})
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_images(manifest, {"unknownParam": 1}, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "CONFIG_INVALID"


# ---------------------------------------------------------------------------
# 临床表
# ---------------------------------------------------------------------------


def _write_csv(tmp_path, files, name, text):
    return _file_entry(tmp_path, files, name, text.encode("utf-8"))


def test_read_table_parses_missing_values_and_primary_key(tmp_path):
    files = {}
    _, csv1 = _write_csv(tmp_path, files, "clinical.csv", "patientId,age,grade\np1,50,NA\np2,,II\np3,64,I\n")
    manifest = _manifest(
        [
            {"rowId": "r1", "sampleId": "s1", "fields": {}, "assets": [{"role": "table", "fileId": csv1["path"]}]},
        ],
        files,
    )
    table = dr.read_table(manifest, {"primaryKeyField": "patientId"}, resolve=_resolve_local(tmp_path))
    assert table["columns"] == ["patientId", "age", "grade"]
    assert table["records"] == [
        {"patientId": "p1", "age": "50", "grade": None},
        {"patientId": "p2", "age": None, "grade": "II"},
        {"patientId": "p3", "age": "64", "grade": "I"},
    ]


def test_read_table_duplicate_primary_key_policies(tmp_path):
    files = {}
    _, csv1 = _write_csv(tmp_path, files, "clinical.csv", "patientId,age\np1,50\np1,60\n")
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "s1", "fields": {}, "assets": [{"role": "table", "fileId": csv1["path"]}]}],
        files,
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_table(manifest, {"primaryKeyField": "patientId"}, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "CSV_DUPLICATE_PRIMARY_KEY"

    table = dr.read_table(
        manifest,
        {"primaryKeyField": "patientId", "onDuplicateKey": "keep_first"},
        resolve=_resolve_local(tmp_path),
    )
    assert len(table["records"]) == 1


def test_read_table_rejects_missing_primary_key_column_and_empty_key(tmp_path):
    files = {}
    _, csv1 = _write_csv(tmp_path, files, "clinical.csv", "subject,age\np1,50\n")
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "s1", "fields": {}, "assets": [{"role": "table", "fileId": csv1["path"]}]}],
        files,
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_table(manifest, {"primaryKeyField": "patientId"}, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "CSV_PRIMARY_KEY_MISSING"

    _, csv2 = _write_csv(tmp_path, files, "clinical2.csv", "patientId,age\nNA,50\n")
    manifest["rows"][0]["assets"][0]["fileId"] = csv2["path"]
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_table(manifest, {"primaryKeyField": "patientId"}, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "CSV_PRIMARY_KEY_EMPTY"


def test_read_table_rejects_wrong_extension(tmp_path):
    files = {}
    _, doc = _file_entry(tmp_path, files, "clinical.pdf", b"%PDF-1.4 fake")
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "s1", "fields": {}, "assets": [{"role": "table", "fileId": doc["path"]}]}],
        files,
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_table(manifest, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "FILE_EXTENSION_UNSUPPORTED"


# ---------------------------------------------------------------------------
# 10x MEX
# ---------------------------------------------------------------------------


def _write_10x_library(
    tmp_path, files, name, n_cells=4, n_genes=3, shuffle_dims=False, features_text=None
):
    """合成 10x MEX 三件套：mtx（gzip）+ barcodes/features.tsv.gz。"""
    from scipy.io import mmwrite

    rng = np.random.default_rng(hash(name) % (2**32))
    matrix = rng.poisson(1.0, size=(n_genes, n_cells)).astype(np.float32)
    mtx_path = tmp_path / f"{name}.mtx"
    mmwrite(mtx_path, matrix)
    mtx_data = mtx_path.read_bytes()
    mtx_path.unlink()
    _, matrix_entry = _file_entry(tmp_path, files, f"{name}/matrix.mtx.gz", gzip.compress(mtx_data))

    if shuffle_dims:
        # 形状既不是 (genes, cells) 也不是 (cells, genes)：维度错误必须定位文库。
        barcodes = [f"bc{i}" for i in range(n_cells + 1)]
    else:
        barcodes = [f"bc{i}" for i in range(n_cells)]
    barcodes_text = "\n".join(barcodes) + "\n"
    _, barcode_entry = _file_entry(tmp_path, files, f"{name}/barcodes.tsv.gz", gzip.compress(barcodes_text.encode()))

    if features_text is None:
        features_text = "".join(f"gene{i}\tGene-{i}\tGene Expression\n" for i in range(n_genes))
    _, feature_entry = _file_entry(tmp_path, files, f"{name}/features.tsv.gz", gzip.compress(features_text.encode()))
    return matrix_entry, barcode_entry, feature_entry


def _needs_scipy_anndata():
    pytest.importorskip("scipy")
    pytest.importorskip("anndata")


def test_read_10x_mex_concatenates_libraries(tmp_path):
    _needs_scipy_anndata()
    files = {}
    lib1 = _write_10x_library(tmp_path, files, "lib1")
    lib2 = _write_10x_library(tmp_path, files, "lib2", n_cells=5)
    manifest = _manifest(
        [
            {"rowId": "r1", "sampleId": "lib1", "fields": {}, "assets": [
                {"role": "matrix", "fileId": lib1[0]["path"]},
                {"role": "barcodes", "fileId": lib1[1]["path"]},
                {"role": "features", "fileId": lib1[2]["path"]},
            ]},
            {"rowId": "r2", "sampleId": "lib2", "fields": {}, "assets": [
                {"role": "matrix", "fileId": lib2[0]["path"]},
                {"role": "barcodes", "fileId": lib2[1]["path"]},
                {"role": "features", "fileId": lib2[2]["path"]},
            ]},
        ],
        files,
    )
    manifest["reader"] = {"key": "read-single-cell", "version": "1.0.0"}
    adata = dr.read_single_cell(manifest, {"sourceFormat": "10x_mex"}, resolve=_resolve_local(tmp_path))
    assert adata.n_obs == 9  # 4 + 5
    assert adata.n_vars == 3
    assert sorted(set(adata.obs["library"])) == ["lib1", "lib2"]
    # 基因名与方向：cells × genes。
    assert list(adata.var_names) == ["gene0", "gene1", "gene2"]


def test_read_single_cell_v11_uses_gene_symbols_and_keeps_gene_ids(tmp_path):
    """1.1.0：var_names 为 second-column 符号，Ensembl ID 存入 var.gene_ids。"""
    _needs_scipy_anndata()
    import scanpy

    import radstudio.operators_readers  # noqa: F401

    files: dict = {}
    features_text = (
        "ENSG1\tGENE-A\tGene Expression\n"
        "ENSG2\tMT-CO1\tGene Expression\n"
        "ENSG3\tDUP\tGene Expression\n"
        "ENSG4\tDUP\tGene Expression\n"
    )
    entries = _write_10x_library(
        tmp_path, files, "lib1", n_cells=2, n_genes=4, features_text=features_text
    )
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "lib1", "fields": {}, "assets": [
            {"role": "matrix", "fileId": entries[0]["path"]},
            {"role": "barcodes", "fileId": entries[1]["path"]},
            {"role": "features", "fileId": entries[2]["path"]},
        ]}],
        files,
    )
    manifest["reader"] = {"key": "read-single-cell", "version": "1.1.0"}

    # 1.0.0 旧行为：仍以第一列 Ensembl ID 作 var_names，且不写 gene_ids。
    old = dr.read_single_cell(manifest, {"sourceFormat": "10x_mex"}, resolve=_resolve_local(tmp_path))
    assert list(old.var_names) == ["ENSG1", "ENSG2", "ENSG3", "ENSG4"]
    assert "gene_ids" not in old.var

    store = BundleArtifactStore(tmp_path / "workspace")
    request = OperatorRequest(
        requestId="req-read-v11",
        runId="run-1",
        nodeId="n1",
        operatorType="ReadSingleCell",
        parameters={},
        inputs={},
        operatorVersion="1.1.0",
        environment={"inputManifest": manifest, "workspaceRoot": str(tmp_path)},
    )
    result = invoke(request, store)
    assert result.status == "completed", result.error
    adata = scanpy.read_h5ad(store.resolve_path(parse_ref(result.outputs["adata"])["path"]))
    assert list(adata.var_names) == ["GENE-A", "MT-CO1", "DUP", "DUP-1"]
    assert list(adata.var["gene_ids"]) == ["ENSG1", "ENSG2", "ENSG3", "ENSG4"]


def test_read_10x_mex_missing_features_and_dim_mismatch(tmp_path):
    _needs_scipy_anndata()
    files = {}
    lib1 = _write_10x_library(tmp_path, files, "lib1")
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "lib1", "fields": {}, "assets": [
            {"role": "matrix", "fileId": lib1[0]["path"]},
            {"role": "barcodes", "fileId": lib1[1]["path"]},
        ]}],
        files,
    )
    manifest["reader"] = {"key": "read-single-cell", "version": "1.0.0"}
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_single_cell(manifest, {"sourceFormat": "10x_mex"}, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "ROW_MISSING_ROLE"
    assert excinfo.value.field == "features"
    assert excinfo.value.row_id == "r1"

    # 维度不符：barcodes 数与矩阵形状对不上。
    files2 = {}
    bad = _write_10x_library(tmp_path, files2, "libX", shuffle_dims=True)
    manifest_bad = _manifest(
        [{"rowId": "r9", "sampleId": "libX", "fields": {}, "assets": [
            {"role": "matrix", "fileId": bad[0]["path"]},
            {"role": "barcodes", "fileId": bad[1]["path"]},
            {"role": "features", "fileId": bad[2]["path"]},
        ]}],
        files2,
    )
    manifest_bad["reader"] = {"key": "read-single-cell", "version": "1.0.0"}
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_single_cell(manifest_bad, {"sourceFormat": "10x_mex"}, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "SC_DIMENSION_MISMATCH"


# ---------------------------------------------------------------------------
# H5AD
# ---------------------------------------------------------------------------


def test_read_h5ad_layer_selection(tmp_path):
    _needs_scipy_anndata()
    anndata = pytest.importorskip("anndata")
    files = {}
    adata = anndata.AnnData(X=np.array([[1.0, 2.0], [3.0, 4.0]]))
    adata.layers["counts"] = np.array([[10.0, 20.0], [30.0, 40.0]])
    adata.raw = adata.copy()
    buffer = tmp_path / "lib.h5ad"
    adata.write_h5ad(buffer)
    entry = {"path": "lib.h5ad", "sha256": _sha(buffer.read_bytes())}
    files[entry["path"]] = entry
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "h5", "fields": {}, "assets": [{"role": "matrix", "fileId": "lib.h5ad"}]}],
        files,
    )
    manifest["reader"] = {"key": "read-single-cell", "version": "1.0.0"}

    x_result = dr.read_single_cell(manifest, {"sourceFormat": "h5ad"}, resolve=_resolve_local(tmp_path))
    assert float(x_result.X[0, 0]) == 1.0

    counts = dr.read_single_cell(manifest, {"sourceFormat": "h5ad", "layerSource": "layer", "layerName": "counts"}, resolve=_resolve_local(tmp_path))
    assert float(counts.X[0, 0]) == 10.0

    raw = dr.read_single_cell(manifest, {"sourceFormat": "h5ad", "layerSource": "raw"}, resolve=_resolve_local(tmp_path))
    assert float(raw.X[1, 1]) == 4.0

    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_single_cell(manifest, {"sourceFormat": "h5ad", "layerSource": "layer", "layerName": "nope"}, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "SC_LAYER_MISSING"


# ---------------------------------------------------------------------------
# manifest 校验与哈希
# ---------------------------------------------------------------------------


def test_manifest_schema_version_rejected(tmp_path):
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.validate_manifest({"schemaVersion": "2", "files": {}, "rows": []})
    assert excinfo.value.code == "MANIFEST_INVALID"


def test_resolver_verifies_sha256(tmp_path):
    files = {}
    _, entry = _file_entry(tmp_path, files, "case1.png", _png_bytes())
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "image", "fileId": entry["path"]}]}],
        files,
    )
    # 内容被篡改：哈希核验失败必须显式报错，不静默使用被改文件。
    (tmp_path / "case1.png").write_bytes(_png_bytes(fill=99))
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_images(manifest, resolve=dr.make_resolver(tmp_path, verify_sha256=True))
    assert excinfo.value.code == "FILE_HASH_MISMATCH"


# ---------------------------------------------------------------------------
# 算子注册与执行（serve 模式）
# ---------------------------------------------------------------------------


def test_reader_operators_registered():
    import radstudio.operators_readers  # noqa: F401

    for operator_type in ("ReadImages", "ReadTable", "ReadSingleCell"):
        operator_class = resolve_operator(operator_type, "1.0.0")
        assert operator_class.operator_type == operator_type
        assert operator_class.operator_version == "1.0.0"


def test_read_images_operator_serve_mode(tmp_path):
    import radstudio.operators_readers  # noqa: F401

    store = BundleArtifactStore(tmp_path / "workspace")
    image_bytes = _png_bytes()
    rel = store.write_local("case1.png", image_bytes)

    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {"patientId": "p1"}, "assets": [{"role": "image", "fileId": "case1.png"}]}],
        {"case1.png": {"path": rel, "sha256": _sha(image_bytes)}},
    )
    request = OperatorRequest(
        requestId="req-read",
        runId="run-1",
        nodeId="n1",
        operatorType="ReadImages",
        parameters={},
        inputs={},
        environment={"inputManifest": manifest, "workspaceRoot": str(tmp_path / "workspace")},
    )
    result = invoke(request, store)
    assert result.status == "completed"
    samples = store.read_json(result.outputs["samples"])
    # 服务端每节点独立工作区：样本路径必须是工作区相对路径，worker 才会把
    # 影像文件搬到下游节点（服务端 samplePathFields 约定）。
    assert samples == [
        {"imageId": "case1", "patientId": "p1", "label": None, "imagePath": rel, "roiPath": ""}
    ]


def test_read_images_operator_uses_manifest_reference(tmp_path):
    import json

    import radstudio.operators_readers  # noqa: F401

    workspace = tmp_path / "workspace"
    store = BundleArtifactStore(workspace)
    image_bytes = _png_bytes()
    rel = store.write_local("case1.png", image_bytes)
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {"patientId": "p1"}, "assets": [{"role": "image", "fileId": "case1.png"}]}],
        {"case1.png": {"path": rel, "sha256": _sha(image_bytes)}},
    )
    manifest_dir = workspace / "snapshot"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(manifest).encode("utf-8")
    (manifest_dir / "input-manifest.json").write_bytes(raw)
    request = OperatorRequest(
        requestId="req-read",
        runId="run-1",
        nodeId="n1",
        operatorType="ReadImages",
        parameters={},
        inputs={},
        environment={
            "inputManifestRef": "snapshot/input-manifest.json",
            "snapshot": {"snapshotId": "snap-1", "manifestSha256": _sha(raw)},
            "workspaceRoot": str(workspace),
        },
    )
    result = invoke(request, store)
    assert result.status == "completed"
    samples = store.read_json(result.outputs["samples"])
    assert samples == [
        {"imageId": "case1", "patientId": "p1", "label": None, "imagePath": rel, "roiPath": ""}
    ]


def test_read_images_operator_rejects_manifest_hash_mismatch(tmp_path):
    import json

    import radstudio.operators_readers  # noqa: F401

    workspace = tmp_path / "workspace"
    store = BundleArtifactStore(workspace)
    image_bytes = _png_bytes()
    rel = store.write_local("case1.png", image_bytes)
    manifest = _manifest(
        [{"rowId": "r1", "sampleId": "case1", "fields": {}, "assets": [{"role": "image", "fileId": "case1.png"}]}],
        {"case1.png": {"path": rel, "sha256": _sha(image_bytes)}},
    )
    manifest_dir = workspace / "snapshot"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "input-manifest.json").write_bytes(json.dumps(manifest).encode("utf-8"))
    request = OperatorRequest(
        requestId="req-read",
        runId="run-1",
        nodeId="n1",
        operatorType="ReadImages",
        parameters={},
        inputs={},
        environment={
            "inputManifestRef": "snapshot/input-manifest.json",
            "snapshot": {"snapshotId": "snap-1", "manifestSha256": "0" * 64},
            "workspaceRoot": str(workspace),
        },
    )
    result = invoke(request, store)
    assert result.status == "failed"
    assert "哈希不符" in str(result.error)


# ---------------------------------------------------------------------------
# 执行端内容寻址工作区：文件落在 sha256 键上，格式只能靠 manifest.fileName
# ---------------------------------------------------------------------------


def _serve_manifest(rows, files, reader_key):
    manifest = _manifest(rows, files)
    manifest["reader"] = {"key": reader_key, "version": "1.0.0"}
    return manifest


def test_read_table_csv_gz_from_content_addressed_workspace(tmp_path):
    files: dict = {}
    data = gzip.compress(b"patientId,age\np1,50\n")
    key = _sha(data)
    (tmp_path / key).write_bytes(data)
    files[key] = {"path": key, "fileName": "clinical.csv.gz", "sha256": key}
    manifest = _serve_manifest(
        [{"rowId": "r1", "sampleId": "s1", "fields": {}, "assets": [{"role": "table", "fileId": key}]}],
        files,
        "read-table",
    )
    table = dr.read_table(manifest, {"primaryKeyField": "patientId"}, resolve=_resolve_local(tmp_path))
    assert table["records"] == [{"patientId": "p1", "age": "50"}]


def test_read_single_cell_10x_from_content_addressed_workspace(tmp_path):
    """10x 三件套：gzip 按魔数识别，barcodes/features 扩展名来自 fileName。"""
    _needs_scipy_anndata()
    files: dict = {}
    rows = []
    for index, n_cells in enumerate((4, 5), start=1):
        entries = _write_10x_library(tmp_path, files, f"lib{index}", n_cells=n_cells)
        roles = ("matrix", "barcodes", "features")
        assets = []
        for entry, role in zip(entries, roles):
            data = (tmp_path / entry["path"]).read_bytes()
            key = _sha(data)
            (tmp_path / key).write_bytes(data)
            files[key] = {"path": key, "fileName": entry["path"].rsplit("/", 1)[-1], "sha256": key}
            assets.append({"role": role, "fileId": key})
        rows.append({"rowId": f"r{index}", "sampleId": f"lib{index}", "fields": {}, "assets": assets})

    manifest = _serve_manifest(rows, files, "read-single-cell")
    adata = dr.read_single_cell(manifest, {"sourceFormat": "10x_mex"}, resolve=_resolve_local(tmp_path))
    assert adata.n_obs == 9
    assert adata.n_vars == 3
    assert sorted(set(adata.obs["library"])) == ["lib1", "lib2"]


def test_read_single_cell_h5ad_from_content_addressed_workspace(tmp_path):
    _needs_scipy_anndata()
    import anndata as ad

    source = ad.AnnData(X=np.eye(3, dtype=np.float32))
    source.obs_names = ["c1", "c2", "c3"]
    source.var_names = ["g1", "g2", "g3"]
    staging = tmp_path / "source.h5ad"
    source.write_h5ad(staging)
    data = staging.read_bytes()
    key = _sha(data)
    (tmp_path / key).write_bytes(data)
    files = {key: {"path": key, "fileName": "lib.h5ad", "sha256": key}}
    manifest = _serve_manifest(
        [{"rowId": "r1", "sampleId": "h5", "fields": {}, "assets": [{"role": "matrix", "fileId": key}]}],
        files,
        "read-single-cell",
    )
    adata = dr.read_single_cell(manifest, {"sourceFormat": "h5ad"}, resolve=_resolve_local(tmp_path))
    assert adata.n_obs == 3
    assert list(adata.var_names) == ["g1", "g2", "g3"]


def test_read_single_cell_h5ad_rejects_wrong_original_name(tmp_path):
    """声明的原始文件名不是 .h5ad 时必须报错，而不是按内容猜测格式。"""
    _needs_scipy_anndata()
    key = _sha(b"fake")
    (tmp_path / key).write_bytes(b"fake")
    files = {key: {"path": key, "fileName": "lib.rds", "sha256": key}}
    manifest = _serve_manifest(
        [{"rowId": "r1", "sampleId": "h5", "fields": {}, "assets": [{"role": "matrix", "fileId": key}]}],
        files,
        "read-single-cell",
    )
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_single_cell(manifest, {"sourceFormat": "h5ad"}, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "FILE_EXTENSION_UNSUPPORTED"
    assert "lib.rds" in str(excinfo.value)


def test_read_images_operator_requires_manifest(tmp_path):
    import radstudio.operators_readers  # noqa: F401

    store = BundleArtifactStore(tmp_path / "workspace")
    request = OperatorRequest(
        requestId="req-read",
        runId="run-1",
        nodeId="n1",
        operatorType="ReadImages",
        parameters={},
        inputs={},
    )
    result = invoke(request, store)
    assert result.status == "failed"
    assert "inputManifest" in str(result.error)


# ---------------------------------------------------------------------------
# 资源快照（R2/R3）：没有样本表，读取器按文件清单合成行
# ---------------------------------------------------------------------------


def _resource_manifest(tmp_path, reader_key, mapping):
    """mapping: relativePath -> bytes；files 以 assetVersionId 为键。"""
    files: dict = {}
    entries = []
    for index, (relative, data) in enumerate(mapping.items(), start=1):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        version_id = f"ver-{index}"
        files[version_id] = {"path": relative, "sha256": _sha(data), "fileName": relative.rsplit("/", 1)[-1]}
        entries.append(
            {
                "assetId": f"asset-{index}",
                "assetRevision": 1,
                "assetVersionId": version_id,
                "relativePath": relative,
                "sha256": _sha(data),
                "byteSize": len(data),
            }
        )
    return {
        "schemaVersion": "1",
        "reader": {"key": reader_key, "version": "1.0.0"},
        "config": {},
        "source": {
            "kind": "resources",
            "mode": "directory",
            "directoryId": "dir-1",
            "directoryRevision": 2,
            "recursive": True,
            "selectionHash": "hash",
        },
        "files": files,
        "rows": [],
        "entries": entries,
    }


def test_read_images_resource_directory_synthesizes_samples(tmp_path):
    manifest = _resource_manifest(
        tmp_path,
        "read-images",
        {
            "caseA/001.png": _png_bytes(),
            "caseA/001_mask.png": _png_bytes(fill=255),
            "caseA/002.png": _png_bytes(fill=9),
            "caseA/002_mask.png": _png_bytes(fill=255),
            "clinical.csv": b"patientId,age\np1,50\n",
        },
    )
    samples = dr.read_images(
        manifest,
        {"roiRole": "roi", "requireRoi": True, "roiNameContains": "_mask"},
        resolve=_resolve_local(tmp_path),
    )
    assert [s["imageId"] for s in samples] == ["caseA/001", "caseA/002"]
    assert samples[0]["patientId"] == "caseA"
    assert samples[0]["label"] is None
    assert samples[0]["roiPath"] == str(tmp_path / "caseA" / "001_mask.png")
    assert samples[1]["roiPath"] == str(tmp_path / "caseA" / "002_mask.png")


def test_read_images_resource_label_from_directory(tmp_path):
    manifest = _resource_manifest(
        tmp_path,
        "read-images",
        {"benign/001.png": _png_bytes(), "malignant/001.png": _png_bytes()},
    )
    samples = dr.read_images(
        manifest,
        {"labelField": "labelValue", "positiveValue": "malignant", "negativeValue": "benign"},
        resolve=_resolve_local(tmp_path),
    )
    assert [(s["imageId"], s["label"]) for s in samples] == [("benign/001", 0), ("malignant/001", 1)]

    classes = dr.read_images(
        manifest,
        {"labelField": "labelValue", "classLabels": {"benign": 0, "malignant": 1}},
        resolve=_resolve_local(tmp_path),
    )
    assert [s["label"] for s in classes] == [0, 1]


def test_read_images_resource_requires_roi_mask(tmp_path):
    manifest = _resource_manifest(tmp_path, "read-images", {"caseA/001.png": _png_bytes()})
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_images(
            manifest,
            {"roiRole": "roi", "requireRoi": True, "roiNameContains": "_mask"},
            resolve=_resolve_local(tmp_path),
        )
    assert excinfo.value.code == "ROW_MISSING_ROLE"

    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_images(manifest, {"roiRole": "roi"}, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "CONFIG_INVALID"


def test_read_images_resource_without_images_is_explicit(tmp_path):
    manifest = _resource_manifest(tmp_path, "read-images", {"notes.csv": b"a,b\n1,2\n"})
    with pytest.raises(dr.ReaderError) as excinfo:
        dr.read_images(manifest, resolve=_resolve_local(tmp_path))
    assert excinfo.value.code == "RESOURCE_NO_IMAGES"


def test_read_table_resource_files(tmp_path):
    manifest = _resource_manifest(
        tmp_path,
        "read-table",
        {"site_a.csv": b"patientId,age\np1,50\n", "site_b.csv": b"patientId,age\np2,60\n"},
    )
    table = dr.read_table(manifest, {"primaryKeyField": "patientId"}, resolve=_resolve_local(tmp_path))
    assert table["records"] == [{"patientId": "p1", "age": "50"}, {"patientId": "p2", "age": "60"}]


def test_manifest_rows_single_cell_groups_libraries_from_entries(tmp_path):
    manifest = _resource_manifest(
        tmp_path,
        "read-single-cell",
        {
            "lib1/matrix.mtx": b"%%MatrixMarket matrix coordinate integer general\n",
            "lib1/barcodes.tsv": b"c1\n",
            "lib1/features.tsv": b"g1\n",
            "lib2/matrix.mtx": b"%%MatrixMarket matrix coordinate integer general\n",
            "lib2/barcodes.tsv": b"c2\n",
            "lib2/features.tsv": b"g1\n",
        },
    )
    rows = dr.manifest_rows(manifest, "read-single-cell", {"sourceFormat": "10x_mex"})
    assert [row["sampleId"] for row in rows] == ["lib1", "lib2"]
    assert [sorted(a["role"] for a in row["assets"]) for row in rows] == [
        ["barcodes", "features", "matrix"],
        ["barcodes", "features", "matrix"],
    ]


def test_manifest_rows_dicom_treats_selection_as_one_series(tmp_path):
    manifest = _resource_manifest(
        tmp_path,
        "read-dicom",
        {"series/0001.dcm": b"a", "series/0002.dcm": b"b"},
    )
    rows = dr.manifest_rows(manifest, "read-dicom", {})
    assert len(rows) == 1
    assert rows[0]["sampleId"] == "series"
    assert len(rows[0]["assets"]) == 2
