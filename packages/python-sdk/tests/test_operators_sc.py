"""单细胞算子（ScQualityControl 等）小规模合成数据测试。

无 scanpy 环境（未装 [sc] extra）自动跳过。矩阵规模 ≤ 8 细胞 × 9 基因，
QC 断言全部对照手工计算值；50,000 细胞上限用 monkeypatch 缩小验证。
"""

import numpy as np
import pytest
from scipy import sparse

from radstudio.artifact_boundary import hash_file
from radstudio.operator import (
    ArtifactStore,
    OperatorRequest,
    invoke,
    make_ref,
    parse_ref,
)

pytest.importorskip("scanpy", reason="Single-cell operators require the [sc] extra")

QC_PARAMS = {"minGenes": 3, "minCells": 2, "maxMitoPercent": 50.0, "excludeFailed": True}


def _request(node_id, operator_type, parameters, inputs, environment=None, version="1.0.3"):
    return OperatorRequest(
        requestId=f"req-{node_id}",
        runId="run-1",
        nodeId=node_id,
        operatorType=operator_type,
        parameters=parameters,
        inputs=inputs,
        operatorVersion=version,
        environment=environment,
    )


def _qc_fixture_adata():
    """8 细胞 × 9 基因手工设计矩阵（见 test_sc_qc_filters 的逐格注释）。"""
    import anndata

    gene_names = ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "MT-A", "MT-B"]
    counts = np.array(
        [
            [10, 5, 2, 0, 0, 0, 0, 1, 0],  # c0: 4 genes, mito 1/18 → PASS
            [1, 2, 0, 0, 0, 0, 0, 0, 0],  # c1: 2 genes → lowGenes
            [3, 3, 3, 0, 0, 0, 0, 10, 5],  # c2: mito 15/24=62.5% → highMito
            [0, 0, 0, 10, 0, 0, 0, 0, 2],  # c3: 2 genes → lowGenes
            [5, 0, 0, 5, 5, 0, 0, 0, 0],  # c4: PASS
            [0, 4, 0, 4, 0, 4, 0, 0, 0],  # c5: PASS
            [2, 2, 2, 2, 2, 2, 0, 0, 0],  # c6: PASS
            [0, 0, 0, 0, 0, 0, 100, 50, 0],  # c7: 2 genes → lowGenes
        ],
        dtype=np.int64,
    )
    adata = anndata.AnnData(
        X=sparse.csr_matrix(counts),
        obs={"patientId": [f"p{i}" for i in range(counts.shape[0])]},
        var={"geneName": gene_names},
    )
    adata.var_names = gene_names  # QC 按 var_names 识别线粒体基因
    return adata


def _adata_ref(store, adata, name="input.h5ad"):
    target = store.root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    adata.write_h5ad(target, compression="lzf")
    store.register(name, "adata")
    digest, _size = hash_file(target)
    return make_ref(name, digest, "adata")


def test_sc_operators_registered():
    import radstudio.operators_sc  # noqa: F401
    from radstudio.operator import resolve_operator

    cls = resolve_operator("ScQualityControl", "1.0.3")
    assert cls.operator_type == "ScQualityControl"
    v104 = resolve_operator("ScQualityControl", "1.0.4")
    assert v104.operator_type == "ScQualityControl"
    assert v104.operator_version == "1.0.4"
    clustering = resolve_operator("ScClustering", "1.0.4")
    assert clustering.operator_type == "ScClustering"


def test_sc_qc_filters(tmp_path):
    import scanpy

    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    result = invoke(
        _request("qc", "ScQualityControl", QC_PARAMS, {"adata": ref}),
        store,
    )
    assert result.status == "completed", result.error
    adata = scanpy.read_h5ad(store.resolve_path(parse_ref(result.outputs["adata"])["path"]))
    # c0/c4/c5/c6 存活；G7/MT-A/MT-B 被基因过滤移除
    assert adata.n_obs == 4 and adata.n_vars == 6
    report = store.read_json(result.outputs["report"])
    assert report["cellsBefore"] == 8 and report["genesBefore"] == 9
    assert report["cellsAfter"] == 4 and report["genesAfter"] == 6
    # excluded 全部按过滤前原始数据统计：lowGenes=c1/c3/c7，highMito=c2（62.5%），
    # lowCellGenes=G7（仅 c7 一个细胞表达，minCells=2）
    assert report["excluded"] == {"lowGenes": 3, "highMito": 1, "lowCellGenes": 1}
    assert report["mitoGenes"] == 2
    # 存活细胞 c0=4, c4=3, c5=3, c6=6 基因；计数 18/15/12/12
    assert report["medianGenesPerCell"] == pytest.approx(3.5)
    assert report["medianCountsPerCell"] == pytest.approx(13.5)


def test_sc_qc_v104_max_genes(tmp_path):
    """1.0.4：maxGenes 上限生效（6 基因的 c6 被剔除），旧 1.0.3 不产生上限。"""
    import scanpy

    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    params = dict(QC_PARAMS, maxGenes=4)
    result = invoke(
        _request("qc", "ScQualityControl", params, {"adata": ref}, version="1.0.4"),
        store,
    )
    assert result.status == "completed", result.error
    adata = scanpy.read_h5ad(store.resolve_path(parse_ref(result.outputs["adata"])["path"]))
    # c0/c4/c5 存活；c6 因 6 个基因 > maxGenes=4 被剔除。
    assert adata.n_obs == 3 and adata.n_vars == 3
    report = store.read_json(result.outputs["report"])
    assert report["cellsAfter"] == 3 and report["genesAfter"] == 3
    assert report["excluded"] == {
        "lowGenes": 3,
        "highMito": 1,
        "lowCellGenes": 1,
        "highGenes": 2,
    }
    assert report["thresholds"]["maxGenes"] == 4

    # maxGenes=0 表示不启用上限，结果与 1.0.3 一致。
    store_zero = ArtifactStore(tmp_path / "workspace-zero")
    ref_zero = _adata_ref(store_zero, _qc_fixture_adata())
    zero = invoke(
        _request(
            "qc",
            "ScQualityControl",
            dict(QC_PARAMS, maxGenes=0),
            {"adata": ref_zero},
            version="1.0.4",
        ),
        store_zero,
    )
    assert zero.status == "completed", zero.error
    zero_report = store_zero.read_json(zero.outputs["report"])
    assert zero_report["cellsAfter"] == 4
    assert zero_report["excluded"]["highGenes"] == 0
    assert zero_report["thresholds"]["maxGenes"] == 0

    # 1.0.3 旧版报告不包含 maxGenes/highGenes 字段。
    old = invoke(_request("qc", "ScQualityControl", QC_PARAMS, {"adata": ref}, version="1.0.3"), store)
    assert old.status == "completed", old.error
    old_report = store.read_json(old.outputs["report"])
    assert "maxGenes" not in old_report["thresholds"]
    assert "highGenes" not in old_report["excluded"]


def test_sc_qc_v104_invalid_max_genes(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    for max_genes, message in ((-1, "maxGenes"), (100001, "maxGenes"), (2, "maxGenes")):
        result = invoke(
            _request(
                "qc",
                "ScQualityControl",
                dict(QC_PARAMS, maxGenes=max_genes),
                {"adata": ref},
                version="1.0.4",
            ),
            store,
        )
        assert result.status == "failed"
        assert message in result.error.message


def test_sc_qc_annotate_only(tmp_path):
    import scanpy

    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    result = invoke(
        _request("qc", "ScQualityControl", dict(QC_PARAMS, excludeFailed=False), {"adata": ref}),
        store,
    )
    assert result.status == "completed", result.error
    adata = scanpy.read_h5ad(store.resolve_path(parse_ref(result.outputs["adata"])["path"]))
    assert adata.n_obs == 8 and adata.n_vars == 9
    assert "sc_qc_pass" in adata.obs.columns
    assert int(adata.obs["sc_qc_pass"].sum()) == 4
    report = store.read_json(result.outputs["report"])
    assert report["thresholds"]["excludeFailed"] is False


def test_sc_qc_rejects_empty_result(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    result = invoke(
        _request(
            "qc",
            "ScQualityControl",
            dict(QC_PARAMS, minGenes=100, maxMitoPercent=0.0),
            {"adata": ref},
        ),
        store,
    )
    assert result.status == "failed"
    assert "没有剩余细胞" in result.error.message


def test_sc_qc_invalid_threshold(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    result = invoke(
        _request("qc", "ScQualityControl", dict(QC_PARAMS, maxMitoPercent=150.0), {"adata": ref}),
        store,
    )
    assert result.status == "failed"
    assert "阈值" in result.error.message


def test_sc_qc_without_mito_genes(tmp_path):

    import radstudio.operators_sc  # noqa: F401

    adata = _qc_fixture_adata()
    adata.var_names = [f"G{i + 1}" for i in range(adata.n_vars)]  # 无 MT- 基因
    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, adata)
    result = invoke(_request("qc", "ScQualityControl", QC_PARAMS, {"adata": ref}), store)
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report["mitoGenes"] == 0
    assert report["excluded"]["highMito"] == 0
    # c2 仅因无 mito 指标而存活 → cellsAfter = 5
    assert report["cellsAfter"] == 5


# ---------------------------------------------------------------------------
# 阶段 2：ScNormalize / ScDimReduction / ScClustering / ScDifferentialExpression
# ---------------------------------------------------------------------------


def _read_csv_bytes(store, ref):
    rows = store.read_bytes(ref).decode("utf-8").strip().splitlines()
    header = rows[0].split(",")
    return header, [line.split(",") for line in rows[1:]]


def _is_png(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def test_sc_normalize_hand_computed(tmp_path):
    import anndata
    import scanpy

    import radstudio.operators_sc  # noqa: F401

    # 单细胞手算：counts [10,5,5] 总量 20，targetSum=100 → [50,25,25]，log1p → ln(51)/ln(26)
    adata = anndata.AnnData(X=np.array([[10.0, 5.0, 5.0]]), var={"geneName": ["A", "B", "C"]})
    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, adata)
    result = invoke(
        _request(
            "norm",
            "ScNormalize",
            {"targetSum": 100.0, "log1p": True, "hvgCount": 0, "flavor": "seurat"},
            {"adata": ref},
        ),
        store,
    )
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report["log1p"] is True and report["hvgSelected"] == 0
    normalized = scanpy.read_h5ad(
        store.resolve_path(parse_ref(result.outputs["adata"])["path"])
    )
    assert normalized.X[0, 0] == pytest.approx(np.log1p(50.0))
    assert normalized.X[0, 1] == pytest.approx(np.log1p(25.0))
    assert normalized.X[0, 2] == pytest.approx(np.log1p(25.0))


def test_sc_normalize_hvg_marked_not_subset(tmp_path):
    import scanpy

    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    result = invoke(
        _request(
            "norm",
            "ScNormalize",
            {"targetSum": 10000.0, "log1p": True, "hvgCount": 3, "flavor": "seurat"},
            {"adata": ref},
        ),
        store,
    )
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report["hvgCount"] == 3 and report["hvgSelected"] == 3
    assert report["flavor"] == "seurat"
    adata = scanpy.read_h5ad(store.resolve_path(parse_ref(result.outputs["adata"])["path"]))
    # 不子集化：基因矩阵保留 9 列，仅标注 highly_variable
    assert adata.n_vars == 9
    assert int(adata.var["highly_variable"].sum()) == 3


def test_sc_normalize_invalid_params(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    for params, message in [
        ({"targetSum": 0.0, "log1p": True, "hvgCount": 0, "flavor": "seurat"}, "targetSum"),
        ({"targetSum": 1e4, "log1p": True, "hvgCount": 0, "flavor": "seurat_v3"}, "flavor"),
    ]:
        result = invoke(_request("norm", "ScNormalize", params, {"adata": ref}), store)
        assert result.status == "failed"
        assert message in result.error.message


def _run_chain(tmp_path, store, hvg_count=0):
    """QC → Normalize → DimReduction(umap) → Clustering，返回各步结果。"""
    import radstudio.operators_sc  # noqa: F401

    ref = _adata_ref(store, _qc_fixture_adata())
    qc = invoke(_request("qc", "ScQualityControl", QC_PARAMS, {"adata": ref}), store)
    assert qc.status == "completed", qc.error
    norm = invoke(
        _request(
            "norm",
            "ScNormalize",
            {"targetSum": 10000.0, "log1p": True, "hvgCount": hvg_count, "flavor": "seurat"},
            {"adata": qc.outputs["adata"]},
        ),
        store,
    )
    assert norm.status == "completed", norm.error
    dim = invoke(
        _request(
            "dim",
            "ScDimReduction",
            {"nComps": 3, "embedding": "umap", "neighborsK": 3},
            {"adata": norm.outputs["adata"]},
        ),
        store,
    )
    assert dim.status == "completed", dim.error
    cluster = invoke(
        _request(
            "cluster",
            "ScClustering",
            {"resolution": 1.0, "flavor": "leiden"},
            {"adata": dim.outputs["adata"]},
        ),
        store,
    )
    assert cluster.status == "completed", cluster.error
    return ref, qc, norm, dim, cluster


def test_sc_chain_normalize_dim_clustering(tmp_path):
    store = ArtifactStore(tmp_path / "workspace")
    _ref, _qc, _norm, dim, cluster = _run_chain(tmp_path, store)

    dim_report = store.read_json(dim.outputs["report"])
    assert dim_report["embedding"] == "umap" and dim_report["nComps"] == 3
    header, rows = _read_csv_bytes(store, dim.outputs["embedding"])
    assert header[:3] == ["cellId", "dim1", "dim2"]
    assert len(rows) == 4  # QC 后存活 4 细胞
    assert "library" not in header  # 单文库无 library 列
    assert _is_png(store.read_bytes(dim.outputs["plot"]))

    cluster_report = store.read_json(cluster.outputs["report"])
    assert cluster_report["neighborsReused"] is True
    assert cluster_report["clusterCount"] >= 1
    assert cluster_report["cells"] == 4
    header, rows = _read_csv_bytes(store, cluster.outputs["cluster_assignments"])
    assert header[:2] == ["cellId", "cluster"]
    assert len(rows) == 4
    assert all(row[1] for row in rows)  # 每个细胞都有簇号


def test_sc_clustering_fallback_without_neighbors(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    qc = invoke(_request("qc", "ScQualityControl", QC_PARAMS, {"adata": ref}), store)
    assert qc.status == "completed", qc.error
    # 跳过 DimReduction，直接聚类 → 自动回退 PCA + 邻接图
    result = invoke(
        _request(
            "cluster",
            "ScClustering",
            {"resolution": 1.0, "flavor": "leiden"},
            {"adata": qc.outputs["adata"]},
        ),
        store,
    )
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report["neighborsReused"] is False
    assert report["clusterCount"] >= 1


def test_sc_clustering_configurable_seed(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    qc = invoke(_request("qc", "ScQualityControl", QC_PARAMS, {"adata": ref}), store)
    assert qc.status == "completed", qc.error
    result = invoke(
        _request(
            "cluster",
            "ScClustering",
            {"resolution": 1.0, "flavor": "leiden", "seed": 43},
            {"adata": qc.outputs["adata"]},
            version="1.0.4",
        ),
        store,
    )
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report["seed"] == 43
    assert report["clusterCount"] >= 1


def test_sc_clustering_rejects_out_of_range_seed(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    result = invoke(
        _request(
            "cluster",
            "ScClustering",
            {"resolution": 1.0, "flavor": "leiden", "seed": -1},
            {"adata": ref},
            version="1.0.4",
        ),
        store,
    )
    assert result.status == "failed"
    assert "seed" in result.error.message


def test_sc_dim_reduction_tsne(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    qc = invoke(_request("qc", "ScQualityControl", QC_PARAMS, {"adata": ref}), store)
    norm = invoke(
        _request(
            "norm",
            "ScNormalize",
            {"targetSum": 10000.0, "log1p": True, "hvgCount": 0, "flavor": "seurat"},
            {"adata": qc.outputs["adata"]},
        ),
        store,
    )
    assert norm.status == "completed", norm.error
    result = invoke(
        _request(
            "dim",
            "ScDimReduction",
            {"nComps": 3, "embedding": "tsne", "neighborsK": 3},
            {"adata": norm.outputs["adata"]},
        ),
        store,
    )
    assert result.status == "completed", result.error
    report = store.read_json(result.outputs["report"])
    assert report["embedding"] == "tsne"
    assert _is_png(store.read_bytes(result.outputs["plot"]))


def test_sc_dim_reduction_deterministic(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    coords_by_run = []
    for run in ("a", "b"):
        store = ArtifactStore(tmp_path / f"workspace-{run}")
        _ref, _qc, _norm, dim, _cluster = _run_chain(tmp_path, store)
        _header, rows = _read_csv_bytes(store, dim.outputs["embedding"])
        coords_by_run.append([(float(row[1]), float(row[2])) for row in rows])
    assert coords_by_run[0] == coords_by_run[1]


def test_sc_dim_reduction_invalid_params(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _qc_fixture_adata())
    for params, message in [
        ({"nComps": 3, "embedding": "pca", "neighborsK": 3}, "embedding"),
        ({"nComps": 1, "embedding": "umap", "neighborsK": 3}, "nComps"),
    ]:
        result = invoke(_request("dim", "ScDimReduction", params, {"adata": ref}), store)
        assert result.status == "failed"
        assert message in result.error.message


def _de_fixture_adata():
    """6 细胞 × 4 基因（已 log1p 数值）：A 组 G1 高表达，B 组 G2 高表达。"""
    import anndata

    values = np.array(
        [
            [5.0, 0.0, 1.0, 1.0],  # A
            [4.0, 0.0, 1.0, 1.0],  # A
            [5.0, 1.0, 1.0, 0.0],  # A
            [0.0, 5.0, 1.0, 1.0],  # B
            [0.0, 4.0, 1.0, 1.0],  # B
            [1.0, 5.0, 0.0, 1.0],  # B
        ]
    )
    adata = anndata.AnnData(
        X=values,
        obs={"group": ["A", "A", "A", "B", "B", "B"]},
        var={"geneName": ["G1", "G2", "G3", "G4"]},
    )
    adata.var_names = ["G1", "G2", "G3", "G4"]
    return adata


def test_sc_de_markers_wilcoxon(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _de_fixture_adata())
    result = invoke(
        _request(
            "de",
            "ScDifferentialExpression",
            {"groupby": "group", "topN": 2, "method": "wilcoxon"},
            {"adata": ref},
        ),
        store,
    )
    assert result.status == "completed", result.error
    header, rows = _read_csv_bytes(store, result.outputs["markers"])
    assert header == ["group", "gene", "score", "logFoldChange", "pValue", "pAdj"]
    group_a = [row for row in rows if row[0] == "A"]
    group_b = [row for row in rows if row[0] == "B"]
    assert len(group_a) == 2 and len(group_b) == 2
    # A 组 top1 = G1，B 组 top1 = G2（手算组间差异最大的基因）
    assert group_a[0][1] == "G1"
    assert group_b[0][1] == "G2"
    summary = store.read_json(result.outputs["summary"])
    assert summary["method"] == "wilcoxon" and summary["topN"] == 2
    by_group = {item["group"]: item for item in summary["groups"]}
    assert by_group["A"]["topGenes"][0] == "G1"
    assert by_group["B"]["topGenes"][0] == "G2"
    assert by_group["A"]["cells"] == 3
    assert _is_png(store.read_bytes(result.outputs["dot_plot"]))


def test_sc_de_ttest_and_single_group_errors(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _de_fixture_adata())
    result = invoke(
        _request(
            "de",
            "ScDifferentialExpression",
            {"groupby": "group", "topN": 1, "method": "t-test"},
            {"adata": ref},
        ),
        store,
    )
    assert result.status == "completed", result.error
    assert store.read_json(result.outputs["summary"])["method"] == "t-test"

    # 分组列缺失
    missing = invoke(
        _request(
            "de",
            "ScDifferentialExpression",
            {"groupby": "cluster", "topN": 2, "method": "wilcoxon"},
            {"adata": ref},
        ),
        store,
    )
    assert missing.status == "failed"
    assert "缺少分组列" in missing.error.message

    # 单一取值分组
    single = _de_fixture_adata()
    single.obs["group"] = ["A"] * 6
    single_ref = _adata_ref(store, single, name="single.h5ad")
    only_one = invoke(
        _request(
            "de",
            "ScDifferentialExpression",
            {"groupby": "group", "topN": 2, "method": "wilcoxon"},
            {"adata": single_ref},
        ),
        store,
    )
    assert only_one.status == "failed"
    assert "只有一个取值" in only_one.error.message


def test_sc_de_invalid_method(tmp_path):
    import radstudio.operators_sc  # noqa: F401

    store = ArtifactStore(tmp_path / "workspace")
    ref = _adata_ref(store, _de_fixture_adata())
    result = invoke(
        _request(
            "de",
            "ScDifferentialExpression",
            {"groupby": "group", "topN": 2, "method": "binomial"},
            {"adata": ref},
        ),
        store,
    )
    assert result.status == "failed"
    assert "method" in result.error.message
