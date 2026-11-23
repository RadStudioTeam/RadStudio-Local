"""单细胞研究算子（Scanpy/Anndata，[sc] extra，独立 sc 算子镜像）。

ScDataInput 把数据集样本承载的单细胞文库（10x mtx zip 或 h5ad）解析并拼接为
Anndata 对象，样本标识写入 ``adata.obs["library"]``；ScQualityControl 计算
标准 QC 指标（每细胞基因数、计数总数、线粒体占比）并按阈值过滤低质量细胞
与低表达基因。后续归一化/降维/聚类/差异表达算子在阶段 2 接入同一
``adata_snapshot`` 产物流。

scanpy/anndata 为 [sc] extra 依赖——模块级不导入，调用时才检查，
不影响基础算子镜像与其余算子（与 operators_dl 的 torch 模式一致）。
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

import numpy as np

from .artifact_boundary import hash_file
from .operator import Operator, make_ref, operator, parse_ref

_SEED = 42
# Leiden 的 random_state 是 uint32；允许显式传入 0 表示「不额外扰动」。
_MAX_SEED = 2**32 - 1
_ADATA_KIND = "adata"
ScMaxCells = 50000
_MITO_GENE_RE = re.compile(r"^mt[-.]", re.IGNORECASE)


def _require_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    return matplotlib


def _write_plot(ctx, fig, name="plot.png") -> str:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=150, bbox_inches="tight")
    fig.canvas.figure  # noqa: B018 - keep figure alive until saved
    import matplotlib.pyplot as plt

    plt.close(fig)
    return ctx.output_bytes(name, buffer.getvalue(), "image")


def _csv_bytes(header, rows) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _obs_column(adata, name):
    if name in adata.obs and adata.obs[name] is not None:
        return [str(value) for value in adata.obs[name]]
    return None


def _top_hvg_matrix(adata, n_comps: int):
    """PCA 前收敛到高变基因子集（ScNormalize 已标注 highly_variable 时）。"""
    if "highly_variable" not in adata.var:
        return adata
    hvg_count = int(adata.var["highly_variable"].sum())
    if hvg_count and hvg_count > max(n_comps, 2):
        return adata[:, adata.var["highly_variable"]].copy()
    return adata


def _ensure_neighbors(scanpy, adata, n_neighbors: int) -> None:
    if "neighbors" in adata.uns:
        return
    n_comps = min(50, adata.n_vars - 1, adata.n_obs - 1)
    n_comps = max(n_comps, 2)
    work = _top_hvg_matrix(adata, n_comps) if "highly_variable" in adata.var else adata
    scanpy.pp.pca(work, n_comps=n_comps, random_state=_SEED)
    scanpy.pp.neighbors(
        work,
        n_neighbors=max(2, min(n_neighbors, adata.n_obs - 1)),
        n_pcs=n_comps,
        random_state=_SEED,
    )
    adata.obsm["X_pca"] = work.obsm["X_pca"]
    adata.uns["neighbors"] = work.uns["neighbors"]
    for key in ("distances", "connectivities"):
        if key in work.obsp:
            adata.obsp[key] = work.obsp[key]


def _embedding_frame(adata, coords) -> tuple[list, list]:
    header = ["cellId", "dim1", "dim2"]
    library = _obs_column(adata, "library")
    patient = _obs_column(adata, "patientId")
    if library:
        header.append("library")
    if patient:
        header.append("patientId")
    rows = []
    for i, cell_id in enumerate(adata.obs_names):
        row = [str(cell_id), f"{float(coords[i, 0]):.6g}", f"{float(coords[i, 1]):.6g}"]
        if library:
            row.append(library[i])
        if patient:
            row.append(patient[i])
        rows.append(row)
    return header, rows


def _require_scanpy():
    try:
        import scanpy

        return scanpy
    except ImportError as exc:
        raise ImportError(
            "Single-cell operators require the [sc] extra: "
            "uv sync --extra sc (or pip install 'radstudio-sdk[sc]')"
        ) from exc


def _require_anndata():
    try:
        import anndata

        return anndata
    except ImportError as exc:
        raise ImportError(
            "Single-cell operators require the [sc] extra: "
            "uv sync --extra sc (or pip install 'radstudio-sdk[sc]')"
        ) from exc


def _library_id(sample):
    for key in ("imageId", "sampleId"):
        value = sample.get(key)
        if value:
            return str(value)
    raise ValueError("单细胞样本缺少 imageId 标识，无法绑定文库批次")


def _extract_zip_safely(archive: zipfile.ZipFile, target: Path) -> None:
    """有界解包：拒绝绝对路径、回溯与符号链接条目（zipfile 无 filter 参数）。"""
    for member in archive.infolist():
        name = member.filename
        if name.endswith("/"):
            continue
        if (
            name.startswith("/")
            or "\\" in name
            or ":" in name
            or ".." in Path(name).parts
            or not Path(name).parts
        ):
            raise ValueError(f"压缩包内包含越界路径：{name}")
        mode = (member.external_attr >> 16) & 0o170000
        if mode not in (0, stat.S_IFREG):
            raise ValueError(f"压缩包条目 {name} 不是常规文件")
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def _read_tenx_zip(scanpy, zip_path: Path):
    """解压 10x zip 到临时目录并在其内读取；matrix.mtx(.gz) 所在目录必须唯一。"""
    with tempfile.TemporaryDirectory(prefix="sc-library-") as tmp:
        with zipfile.ZipFile(zip_path) as archive:
            _extract_zip_safely(archive, Path(tmp))
        root = Path(tmp)
        matrices = [
            entry
            for entry in root.rglob("*")
            if entry.is_file() and entry.name.lower() in ("matrix.mtx", "matrix.mtx.gz")
        ]
        if not matrices:
            raise ValueError(
                "压缩包内未找到 matrix.mtx（10x mtx 三件套：matrix.mtx + barcodes.tsv + features.tsv）"
            )
        if len(matrices) > 1:
            raise ValueError("压缩包内存在多个 matrix.mtx，请一次只上传一个文库")
        matrix_dir = matrices[0].parent
        # scanpy 对 v3 布局默认强制 .gz 后缀；未压缩 mtx（STARsolo 等）需显式 compressed=False
        compressed = (matrix_dir / "matrix.mtx.gz").is_file()
        return scanpy.read_10x_mtx(
            matrix_dir,
            var_names="gene_symbols",
            make_unique=True,
            cache=False,
            compressed=compressed,
        )


def _load_library(scanpy, path: Path):
    """按后缀解析单个文库：.zip → 10x mtx；.h5ad → Anndata。返回 (adata, format)。"""
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return _read_tenx_zip(scanpy, path), "tenx_mtx"
    if suffix == ".h5ad":
        return scanpy.read_h5ad(path), "h5ad"
    raise ValueError(f"单细胞文库仅支持 .zip（10x mtx）或 .h5ad 文件：{path.name}")


def _write_adata(ctx, adata, name="adata_snapshot.h5ad") -> str:
    """把 Anndata 写入工作区并登记为 adata 产物（大对象走磁盘而非内存字节）。"""
    target = ctx.store.resolve_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    adata.write_h5ad(target, compression="lzf")
    ctx.store.register(name, _ADATA_KIND)
    digest, _size = hash_file(target)
    ctx.emit(
        "artifact.created",
        {
            "nodeId": ctx.request.nodeId,
            "name": name,
            "storagePath": name,
            "artifactType": _ADATA_KIND,
        },
    )
    return make_ref(name, digest, _ADATA_KIND)


def _read_adata_input(ctx, port="adata"):
    """按引用路径直读 h5ad 输入（避免 read_bytes 全量进内存）。"""
    scanpy = _require_scanpy()
    if port not in ctx.inputs:
        raise ValueError(f"Missing required input: {port}")
    info = parse_ref(ctx.inputs[port])
    return scanpy.read_h5ad(ctx.store.resolve_path(info["path"]))


def _check_cell_cap(adata, library_id):
    if adata.n_obs > ScMaxCells:
        raise ValueError(
            f"文库 {library_id} 声明 {adata.n_obs} 个细胞，超过单文库上限 {ScMaxCells}；"
            "请按文库拆分后分次上传"
        )


def _run_quality_control(ctx, max_genes: int | None):
    """QC 实现：max_genes=None 表示旧版（1.0.3，无基因数上限）。"""
    scanpy = _require_scanpy()
    adata = _read_adata_input(ctx)
    p = ctx.parameters
    min_genes = int(p["minGenes"])
    min_cells = int(p["minCells"])
    max_mito = float(p["maxMitoPercent"])
    exclude_failed = bool(p["excludeFailed"])
    if min_genes < 0 or min_cells < 0 or not 0 <= max_mito <= 100:
        raise ValueError("QC 阈值不合法：minGenes/minCells ≥ 0，maxMitoPercent ∈ [0,100]")
    if max_genes is not None:
        if not 0 <= max_genes <= 100000:
            raise ValueError("maxGenes 必须在 [0,100000] 内（0 表示不启用上限）")
        if max_genes and max_genes < min_genes:
            raise ValueError("maxGenes 不能小于 minGenes（0 表示不启用上限）")

    ctx.progress(10, "计算 QC 指标")
    cells_before, genes_before = int(adata.n_obs), int(adata.n_vars)
    adata.var["mt"] = [bool(_MITO_GENE_RE.match(str(name))) for name in adata.var_names]
    mito_genes = int(sum(adata.var["mt"]))
    scanpy.pp.calculate_qc_metrics(
        adata, qc_vars=["mt"], percent_top=(), log1p=False, inplace=True
    )

    low_gene_cells = int((adata.obs["n_genes_by_counts"] < min_genes).sum())
    high_mito_cells = (
        int((adata.obs["pct_counts_mt"] > max_mito).sum()) if mito_genes else 0
    )
    high_gene_cells = (
        int((adata.obs["n_genes_by_counts"] > max_genes).sum()) if max_genes else 0
    )
    low_cell_genes = int((adata.var["n_cells_by_counts"] < min_cells).sum())

    ctx.progress(50, "过滤低质量细胞与基因")
    if exclude_failed:
        scanpy.pp.filter_cells(adata, min_genes=min_genes)
        if max_genes:
            adata = adata[adata.obs["n_genes_by_counts"] <= max_genes, :].copy()
        if mito_genes:
            adata = adata[adata.obs["pct_counts_mt"] <= max_mito, :].copy()
        scanpy.pp.filter_genes(adata, min_cells=min_cells)
    if adata.n_obs == 0:
        raise ValueError(
            f"QC 过滤后没有剩余细胞（阈值 minGenes={min_genes}，"
            f"maxGenes={max_genes or 'off'}，maxMitoPercent={max_mito}）；"
            "请放宽阈值或检查数据"
        )
    if not exclude_failed:
        passed = adata.obs["n_genes_by_counts"] >= min_genes
        if max_genes:
            passed = passed & (adata.obs["n_genes_by_counts"] <= max_genes)
        if mito_genes:
            passed = passed & (adata.obs["pct_counts_mt"] <= max_mito)
        adata.obs["sc_qc_pass"] = passed

    excluded = {
        "lowGenes": low_gene_cells,
        "highMito": high_mito_cells,
        "lowCellGenes": low_cell_genes,
    }
    thresholds = {
        "minGenes": min_genes,
        "minCells": min_cells,
        "maxMitoPercent": max_mito,
        "excludeFailed": exclude_failed,
    }
    if max_genes is not None:
        excluded["highGenes"] = high_gene_cells
        thresholds["maxGenes"] = max_genes
    report = {
        "cellsBefore": cells_before,
        "genesBefore": genes_before,
        "cellsAfter": int(adata.n_obs),
        "genesAfter": int(adata.n_vars),
        "excluded": excluded,
        "thresholds": thresholds,
        "mitoGenes": mito_genes,
        "medianGenesPerCell": float(np.median(adata.obs["n_genes_by_counts"])),
        "medianCountsPerCell": float(np.median(adata.obs["total_counts"])),
    }
    ctx.progress(95, "写出过滤后数据")
    return {
        "adata": _write_adata(ctx, adata),
        "report": ctx.output_json("qc_report.json", report, "report"),
    }


@operator("ScQualityControl", version="1.0.3")
class ScQualityControlOperator(Operator):
    """单细胞质量控制：细胞/基因级过滤 + 线粒体占比上限，输出 QC 摘要。

    excludeFailed=false 时只计算并标注指标（obs 保留 sc_qc_pass），不移除细胞。
    """

    def run(self, ctx):
        return _run_quality_control(ctx, None)


@operator("ScQualityControl", version="1.0.4")
class ScQualityControlOperatorV104(Operator):
    """单细胞质量控制（含基因数上限）：在 1.0.3 基础上增加 maxGenes。

    maxGenes 为 0 时不启用上限；excludeFailed=false 时 sc_qc_pass 同时考虑上下限。
    """

    def run(self, ctx):
        return _run_quality_control(ctx, int(ctx.parameters["maxGenes"]))


@operator("ScNormalize", version="1.0.3")
class ScNormalizeOperator(Operator):
    """单细胞归一化：文库深度归一 + log1p + 高变基因标注（不子集化基因）。"""

    def run(self, ctx):
        scanpy = _require_scanpy()
        adata = _read_adata_input(ctx)
        p = ctx.parameters
        target_sum = float(p["targetSum"])
        log1p = bool(p["log1p"])
        hvg_count = int(p["hvgCount"])
        flavor = str(p["flavor"])
        if target_sum <= 0:
            raise ValueError("targetSum 必须为正数")
        if hvg_count < 0:
            raise ValueError("hvgCount 不能为负（0 表示跳过高变基因标注）")
        if flavor not in ("seurat", "cell_ranger"):
            raise ValueError("flavor 仅支持 seurat 或 cell_ranger（seurat_v3 需原始计数，MVP 不支持）")

        cells_before, genes_before = int(adata.n_obs), int(adata.n_vars)
        ctx.progress(20, "文库深度归一")
        scanpy.pp.normalize_total(adata, target_sum=target_sum)
        if log1p:
            scanpy.pp.log1p(adata)

        hvg_selected = 0
        if hvg_count > 0:
            ctx.progress(60, "标注高变基因")
            n_top = min(hvg_count, adata.n_vars)
            scanpy.pp.highly_variable_genes(adata, n_top_genes=n_top, flavor=flavor)
            hvg_selected = int(adata.var["highly_variable"].sum())
            if hvg_selected == 0:
                raise ValueError("高变基因标注结果为空，请检查数据或增大 hvgCount")

        report = {
            "cells": cells_before,
            "genes": genes_before,
            "targetSum": target_sum,
            "log1p": log1p,
            "hvgCount": hvg_count,
            "hvgSelected": hvg_selected,
            "flavor": flavor,
        }
        ctx.progress(95, "写出归一化数据")
        return {
            "adata": _write_adata(ctx, adata),
            "report": ctx.output_json("normalize_report.json", report, "report"),
        }


@operator("ScDimReduction", version="1.0.3")
class ScDimReductionOperator(Operator):
    """单细胞降维：PCA（优先高变基因）→ 邻接图 → UMAP/t-SNE 二维嵌入。"""

    def run(self, ctx):
        scanpy = _require_matplotlib() and _require_scanpy()
        adata = _read_adata_input(ctx)
        p = ctx.parameters
        n_comps = int(p["nComps"])
        embedding = str(p["embedding"])
        neighbors_k = int(p["neighborsK"])
        if embedding not in ("umap", "tsne"):
            raise ValueError("embedding 仅支持 umap 或 tsne")
        if n_comps < 2 or neighbors_k < 2:
            raise ValueError("nComps 与 neighborsK 均须 ≥ 2")

        cells_before, genes_before = int(adata.n_obs), int(adata.n_vars)
        n_comps = min(n_comps, adata.n_vars - 1, adata.n_obs - 1)
        n_comps = max(n_comps, 2)
        ctx.progress(20, "PCA 主成分分析")
        work = _top_hvg_matrix(adata, n_comps)
        hvg_used = work is not adata
        scanpy.pp.pca(work, n_comps=n_comps, random_state=_SEED)
        adata.obsm["X_pca"] = work.obsm["X_pca"]

        ctx.progress(50, "构建邻接图")
        scanpy.pp.neighbors(
            adata,
            n_neighbors=min(neighbors_k, adata.n_obs - 1),
            n_pcs=n_comps,
            use_rep="X_pca",
            random_state=_SEED,
        )

        ctx.progress(70, f"计算 {embedding.upper()} 嵌入")
        if embedding == "umap":
            scanpy.tl.umap(adata, random_state=_SEED)
            coords = adata.obsm["X_umap"]
        else:
            # sklearn t-SNE 要求 perplexity < n_samples；默认 30 在小样本上直接失败
            perplexity = min(30.0, max(2.0, (adata.n_obs - 1) / 3.0))
            scanpy.tl.tsne(adata, random_state=_SEED, use_rep="X_pca", perplexity=perplexity)
            coords = adata.obsm["X_tsne"]

        header, rows = _embedding_frame(adata, coords)
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(6, 5))
        color_col = "cluster" if "cluster" in adata.obs else ("library" if "library" in adata.obs else None)
        if color_col:
            values = adata.obs[color_col].astype(str)
            for label in sorted(values.unique()):
                mask = (values == label).to_numpy()
                ax.scatter(coords[mask, 0], coords[mask, 1], s=6, label=str(label), alpha=0.8)
            ax.legend(markerscale=2, fontsize=8)
        else:
            ax.scatter(coords[:, 0], coords[:, 1], s=6, alpha=0.8)
        ax.set_xlabel(f"{embedding}1")
        ax.set_ylabel(f"{embedding}2")
        ax.set_title(f"{embedding.upper()} embedding")

        report = {
            "cells": cells_before,
            "genesBefore": genes_before,
            "genesUsed": int(work.n_vars),
            "highlyVariableSubset": hvg_used,
            "nComps": n_comps,
            "neighborsK": int(adata.uns["neighbors"]["params"]["n_neighbors"]),
            "embedding": embedding,
        }
        ctx.progress(95, "写出嵌入结果")
        return {
            "adata": _write_adata(ctx, adata),
            "embedding": ctx.output_bytes(
                "embedding.csv", _csv_bytes(header, rows), "table"
            ),
            "plot": _write_plot(ctx, fig, "embedding_plot.png"),
            "report": ctx.output_json("dim_reduction_report.json", report, "report"),
        }


@operator("ScClustering", version="1.0.3")
class ScClusteringOperator(Operator):
    """单细胞聚类：Leiden 社区检测（igraph flavor，固定随机种子），cluster 写入 obs。"""

    def run(self, ctx):
        return _run_clustering(ctx, _SEED)


@operator("ScClustering", version="1.0.4")
class ScClusteringOperatorV104(Operator):
    """单细胞聚类（可配置随机种子）：在 1.0.3 基础上增加 seed，供稳定性扫描使用。"""

    def run(self, ctx):
        seed = int(ctx.parameters["seed"])
        if not 0 <= seed <= _MAX_SEED:
            raise ValueError(f"seed 必须在 [0, {_MAX_SEED}] 之间")
        return _run_clustering(ctx, seed, include_seed=True)


def _run_clustering(ctx, seed, include_seed=False):
    scanpy = _require_scanpy()
    adata = _read_adata_input(ctx)
    p = ctx.parameters
    resolution = float(p["resolution"])
    flavor = str(p["flavor"])
    if resolution <= 0:
        raise ValueError("resolution 必须为正数")
    if flavor != "leiden":
        raise ValueError("flavor 仅支持 leiden（louvain 已弃用）")
    if adata.n_obs < 3:
        raise ValueError("细胞数过少，无法聚类")

    neighbors_reused = "neighbors" in adata.uns
    ctx.progress(20, "准备邻接图")
    _ensure_neighbors(scanpy, adata, n_neighbors=15)

    ctx.progress(50, "Leiden 聚类")
    scanpy.tl.leiden(
        adata,
        resolution=resolution,
        key_added="cluster",
        random_state=seed,
        flavor="igraph",
        n_iterations=2,
        directed=False,
    )
    clusters = adata.obs["cluster"].astype(str)
    sizes = {str(k): int(v) for k, v in clusters.value_counts().items()}

    header = ["cellId", "cluster"]
    library = _obs_column(adata, "library")
    patient = _obs_column(adata, "patientId")
    if library:
        header.append("library")
    if patient:
        header.append("patientId")
    rows = []
    for i, cell_id in enumerate(adata.obs_names):
        row = [str(cell_id), clusters.iloc[i]]
        if library:
            row.append(library[i])
        if patient:
            row.append(patient[i])
        rows.append(row)

    report = {
        "cells": int(adata.n_obs),
        "resolution": resolution,
        "clusterCount": len(sizes),
        "clusterSizes": dict(sorted(sizes.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)),
        "neighborsReused": neighbors_reused,
    }
    if include_seed:
        report["seed"] = seed
    ctx.progress(95, "写出聚类结果")
    return {
        "adata": _write_adata(ctx, adata),
        "cluster_assignments": ctx.output_bytes(
            "cluster_assignments.csv", _csv_bytes(header, rows), "table"
        ),
        "report": ctx.output_json("clustering_report.json", report, "report"),
    }


@operator("ScDifferentialExpression", version="1.0.3")
class ScDifferentialExpressionOperator(Operator):
    """单细胞差异表达：rank_genes_groups 逐组排名，输出 markers CSV 与点图。"""

    def run(self, ctx):
        scanpy = _require_matplotlib() and _require_scanpy()
        adata = _read_adata_input(ctx)
        p = ctx.parameters
        groupby = str(p["groupby"])
        top_n = int(p["topN"])
        method = str(p["method"])
        if method not in ("wilcoxon", "t-test"):
            raise ValueError("method 仅支持 wilcoxon 或 t-test")
        if top_n <= 0:
            raise ValueError("topN 必须为正数")
        if groupby not in adata.obs:
            raise ValueError(
                f"obs 缺少分组列 {groupby}；请先运行 ScClustering（cluster）或绑定自定义分组列"
            )
        groups = adata.obs[groupby].astype(str)
        if groups.nunique() < 2:
            raise ValueError("分组列只有一个取值，无法做差异表达")

        ctx.progress(30, "逐组基因排名")
        scanpy.tl.rank_genes_groups(adata, groupby, method=method, use_raw=False)
        ranked = adata.uns["rank_genes_groups"]

        ctx.progress(60, "汇总 markers")
        rows = []
        top_genes = {}
        names = ranked["names"]
        scores = ranked["scores"]
        lfc = ranked["logfoldchanges"]
        pvals = ranked["pvals"]
        padj = ranked["pvals_adj"]
        for group_str in names.dtype.names:
            take = min(top_n, len(names[group_str]))
            top_genes[group_str] = [str(names[group_str][i]) for i in range(take)]
            for i in range(take):
                rows.append(
                    [
                        group_str,
                        str(names[group_str][i]),
                        f"{float(scores[group_str][i]):.6g}",
                        f"{float(lfc[group_str][i]):.6g}",
                        f"{float(pvals[group_str][i]):.3g}",
                        f"{float(padj[group_str][i]):.3g}",
                    ]
                )
        header = ["group", "gene", "score", "logFoldChange", "pValue", "pAdj"]

        ctx.progress(80, "绘制 marker 点图")
        import matplotlib.pyplot as plt

        top_per_group = min(top_n, 5)
        plot_genes = sorted({gene for genes in top_genes.values() for gene in genes[:top_per_group]})
        group_labels = sorted(groups.unique())
        fig, ax = plt.subplots(figsize=(max(6, 0.6 * len(plot_genes) + 2), max(4, 0.5 * len(group_labels) + 2)))
        if plot_genes:
            x_positions = range(len(plot_genes))
            y_positions = range(len(group_labels))
            for yi, group_label in enumerate(group_labels):
                mask = (groups == group_label).to_numpy()
                sub = adata[mask][:, plot_genes]
                matrix = sub.X.toarray() if hasattr(sub.X, "toarray") else np.asarray(sub.X)
                means = matrix.mean(axis=0)
                fractions = (matrix > 0).mean(axis=0)
                sizes = 20 + 180 * np.asarray(fractions, dtype=float)
                scatter = ax.scatter(
                    x_positions,
                    [yi] * len(plot_genes),
                    s=sizes,
                    c=np.asarray(means, dtype=float),
                    cmap="YlGnBu",
                    edgecolors="grey",
                    linewidths=0.5,
                )
            ax.set_xticks(list(x_positions))
            ax.set_xticklabels(plot_genes, rotation=45, ha="right", fontsize=8)
            ax.set_yticks(list(y_positions))
            ax.set_yticklabels(group_labels)
            fig.colorbar(scatter, ax=ax, label="mean expression (log)", shrink=0.8)
            ax.set_title("Marker genes (dot size = fraction expressing)")
        else:
            ax.text(0.5, 0.5, "no marker genes", ha="center", va="center")

        group_sizes = {str(k): int(v) for k, v in groups.value_counts().items()}
        summary = {
            "groupby": groupby,
            "method": method,
            "topN": top_n,
            "groups": [
                {"group": key, "cells": group_sizes[key], "topGenes": top_genes[key]}
                for key in sorted(top_genes)
            ],
        }
        ctx.progress(95, "写出差异表达结果")
        return {
            "markers": ctx.output_bytes("markers.csv", _csv_bytes(header, rows), "table"),
            "dot_plot": _write_plot(ctx, fig, "dot_plot.png"),
            "summary": ctx.output_json("de_summary.json", summary, "metrics"),
        }
