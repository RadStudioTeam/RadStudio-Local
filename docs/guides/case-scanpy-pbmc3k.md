> **本地版说明**：本文改编自 RadStudio 云端版文档；涉及网页工作台、组织、计费、云函数等步骤在本地版不适用。本地操作以仓库 README、examples/ 与 docs/quickstart.md 为准。


# 案例：Scanpy PBMC3k 单细胞流程复现

本页记录「用 RadStudio 的单细胞链路复现 Scanpy 官方 PBMC3k 教程流程」的案例：从 10x 原始矩阵出发，经过 QC、归一化、特征选择、降维、邻居图、UMAP、Leiden 聚类，到 Wilcoxon 差异表达与 marker 表。

> **当前状态**
>
本案例**已完成一次真实运行**，运行 ID 为 `p4-pbmc3k-tutorial-qc-20261122T072828Z`；本页 QC、簇数、簇规模、marker 表与关键基因名次全部取自该次运行的产物文件。**复现包发布、独立干净环境重跑与博客整理仍在进行中**，因此本页不登记 DOI，也不宣称逐点复现教程的 UMAP 或簇边界。

## 复现对象

| 项目 | 内容 |
| --- | --- |
| 论文题目 | SCANPY: large-scale single-cell gene expression data analysis |
| 作者 | Wolf FA, Angerer P, Theis FJ |
| 期刊与年份 | Genome Biology, 2018;19:15 |
| DOI | 10.1186/s13059-017-1382-0 |
| 复现范围 | Scanpy 官方 PBMC3k 教程标准流程：QC → 归一化 / log1p / HVG → PCA → 邻居图 → UMAP → Leiden → Wilcoxon marker |
| 不做什么 | 不做细胞类型自动注释、不做批次整合、不做双细胞识别 |
| 对应模块 | 单细胞转录组分析 |

## 论文数据与结果

Scanpy 是一篇工具论文，核心是面向大规模单细胞表达数据的可扩展分析框架与配套流程。

- 数据：PBMC3k 是 Scanpy 官方教程长期使用的公开 10x 数据集。
- 流程：教程标准步骤为 QC → 归一化与 log1p → 高变基因选择 → PCA → 邻居图 → UMAP → Leiden 聚类 → Wilcoxon 差异表达。
- 公开口径结果：教程在 QC 后得到 **2638 cells**，resolution 1.0 下得到**约 8 簇**，并以 marker 基因与 UMAP 结构做定性展示。
- 以上为 Scanpy 官方教程的公开口径，**以官方教程为准**；本页不复述论文中的其他数值，也不把教程结果当作本次运行的对照基准。

单细胞结果对版本、参数与随机种子敏感，簇数与簇边界天然存在波动；本案例复现的是「官方教程的标准流程与参数口径能否在平台内跑通并得到可追溯结果」。

## 本次数据

| 项目 | 内容 |
| --- | --- |
| 数据集 | 10x Genomics PBMC3k（`pbmc3k_filtered_gene_bc_matrices`） |
| 来源 / 版本 | 10x Genomics 公开数据，v1.1.0，hg19 基因组注释 |
| 文件组织 | 10x MEX 三件套（`barcodes.tsv`、`genes.tsv`、`matrix.mtx`），基因符号取 `genes.tsv` 第二列 |
| 样本量 | 2700 cells × 32738 genes（QC 前） |
| 归档校验和 | SHA-256 `847d6ebd9a1ec9a768f2be7e40ca42cbfe75ebeb6d76a4c24167041699dc28b5`（归档 7621991 bytes） |
| 许可 | 10x Site Terms，仅限研究用途；仓库不再分发原始文件 |
| 状态 | 已入库并完成一次真实运行 |

矩阵读取后 `var_names` 为基因符号，`var.gene_ids` 保留 Ensembl ID，因此 `MT-` 前缀的线粒体基因可被正确识别（本次识别到 13 个）。

## 工作流与算子

```text
ReadSingleCell → ScQualityControl → ScNormalize → ScDimReduction
               → ScClustering → ScDifferentialExpression → Report
```

| 顺序 | 算子 | 版本 | 本次参数 | 作用 |
| --- | --- | --- | --- | --- |
| 1 | `ReadSingleCell` | 1.1.0 | `sourceFormat=10x_mex`，`maxCellsPerLibrary=50000` | 读取 10x MEX 三件套，以基因符号建立表达矩阵 |
| 2 | `ScQualityControl` | 1.0.4 | `minGenes=200`、`maxGenes=2500`、`minCells=3`、`maxMitoPercent=5`、`excludeFailed=true` | 按细胞与基因层面阈值过滤，输出 QC 前后计数与过滤原因 |
| 3 | `ScNormalize` | 1.0.3 | `targetSum=10000`、`log1p=true`、`hvgCount=2000`、`flavor=seurat` | 文库大小归一化、log1p 转换与 HVG 选择 |
| 4 | `ScDimReduction` | 1.0.3 | `nComps=50`、`neighborsK=10`、`embedding=umap` | PCA 降维、邻居图构建与 UMAP 嵌入 |
| 5 | `ScClustering` | 1.0.3 | `flavor=leiden`、`resolution=1.0` | Leiden 聚类，输出簇标签与簇规模 |
| 6 | `ScDifferentialExpression` | 1.0.3 | `groupby=cluster`、`method=wilcoxon`、`topN=25` | Wilcoxon 差异表达，输出逐簇 marker 表 |
| 7 | `Report` | 本次运行记录未登记 | — | 汇总参数、簇表、marker 表与图；本次 `run-summary.json` 未包含 Report 步骤与版本 |

聚类使用的邻居图复用降维阶段的结果（`neighborsReused=true`），Leiden 固定 `igraph` 后端、`n_iterations=2`、`random_state=42`。

## 方法差异

| 维度 | 官方教程 | 本次运行 | 影响 |
| --- | --- | --- | --- |
| 输入通道 | 10x MEX 原始矩阵（hg19） | 同一份 v1.1.0 归档，以基因符号作 `var_names` | 基因命名一致，不影响 HVG 与 QC 口径 |
| QC 阈值 | `n_genes` 在 200–2500，`pct_mt` ≤ 5 | `minGenes=200`、`maxGenes=2500`、`maxMitoPercent=5` | QC 后 2638 cells，与教程公开值一致 |
| 归一化 / HVG | `normalize_total(1e4)` + `log1p`；HVG 用 `seurat_v3` 或 `seurat` 规则 | `targetSum=10000`、`log1p`、`hvgCount=2000`、`flavor=seurat` | HVG 集合与教程可能不同，传导到 PCA 与聚类 |
| 降维 | PCA `n_pcs=40` 后建邻居图 | `nComps=50`、`neighborsK=10` | UMAP 坐标与教程不可逐点比较 |
| 聚类算法 | Scanpy 默认 Leiden 参数 | Leiden + `igraph`、`n_iterations=2`、`random_state=42`、`resolution=1.0` | 簇数与簇边界可能不同 |
| 预处理 | 教程历史上做过 `regress_out` | 未做 `regress_out` | 部分结构差异可能来自此处 |
| 差异表达 | `rank_genes_groups`，Wilcoxon | `method=wilcoxon`、`topN=25`、`use_raw=False` | 方法口径一致，marker 排名可直接对照 |
| 指标口径 | 簇数、marker 与 UMAP 结构的定性核对 | 同左，额外记录关键基因名次 | 无统一数值标准；本次未计算置信区间 |

## 复现步骤

1. 下载 10x PBMC3k 归档（v1.1.0，hg19），登记许可、字节数与 SHA-256。
2. 以 `ReadSingleCell` 1.1.0 读取 `matrix.mtx`、`barcodes.tsv`、`genes.tsv`，建立 2700 cells × 32738 genes 的表达矩阵。
3. 以 `ScQualityControl` 1.0.4 按 200–2500 基因、线粒体比例 ≤ 5% 过滤细胞，并按 `minCells=3` 过滤基因。
4. 以 `ScNormalize` 1.0.3 做 `targetSum=10000` 归一化、`log1p` 转换，并用 `seurat` flavor 选 2000 个高变基因。
5. 以 `ScDimReduction` 1.0.3 在 2000 个 HVG 上做 50 维 PCA、10 邻居图与 UMAP。
6. 以 `ScClustering` 1.0.3 做 `resolution=1.0` 的 Leiden 聚类，落盘簇标签与簇规模。
7. 以 `ScDifferentialExpression` 1.0.3 做 Wilcoxon 检验，`topN=25` 输出逐簇 marker 表与 dot plot。
8. 汇总运行产物（簇表、marker 表、UMAP 图、报告 JSON），登记运行 ID、算子版本与参数快照。

仍待完成的三项：复现包发布、独立干净环境重跑、博客整理。跨 resolution 与跨 seed 的 ARI 稳定性扫描见下文「聚类稳定性」小节（seed 扫描使用新增的 `ScClustering` 1.0.4）。

## 复现结果

运行登记：

| 字段 | 值 |
| --- | --- |
| runId | `p4-pbmc3k-tutorial-qc-20261122T072828Z` |
| pipelineProfile | `scanpy-pbmc3k-tutorial-qc` |
| 运行时间 | 2026-11-22T07:28:28Z 至 2026-11-22T07:28:59Z，总耗时 30.7 s |
| 环境 | Python 3.12.13、Scanpy 1.12.5、anndata 0.13.4 |
| 状态 | completed |

QC 结果：

| 项目 | 结果 |
| --- | --- |
| QC 前 | 2700 cells × 32738 genes |
| QC 后 | 2638 cells × 13656 genes |
| 细胞级过滤 | highGenes 5、highMito 57、lowGenes 0 |
| 基因级过滤 | lowCellGenes 19024（`minCells=3`） |
| 线粒体基因 | 识别 13 个 |
| HVG | 选出 2000 个（`flavor=seurat`） |

聚类结果：Leiden `resolution=1.0` 共 **9 簇**，合计 2638 细胞。

| 簇 | 细胞数 | top5 marker |
| --- | --- | --- |
| 0 | 330 | CCL5、NKG7、B2M、CST7、GZMA |
| 1 | 517 | RPS27、RPL32、RPS12、RPS6、RPS3A |
| 2 | 601 | LTB、IL32、LDHB、CD3D、TPT1 |
| 3 | 342 | CD74、CD79A、HLA-DRA、CD79B、HLA-DPB1 |
| 4 | 464 | LYZ、S100A9、S100A8、TYROBP、FTL |
| 5 | 154 | GNLY、NKG7、GZMB、CTSW、PRF1 |
| 6 | 187 | LST1、COTL1、AIF1、FCER1G、FTH1 |
| 7 | 31 | HLA-DQA1、CD74、HLA-DRB5、HLA-DRB1、HLA-DPB1 |
| 8 | 12 | SDPR、GNG11、HIST1H2AC、PF4、PPBP |

关键基因在 `topN=25` 内的名次（1-based）：

| 基因 | 所在簇 | 名次 |
| --- | --- | --- |
| CD3D | 簇 2 | 4 |
| CD3E | 簇 2 | 8 |
| LST1 | 簇 6 | 1 |
| FCGR3A | 簇 6 | 7 |
| MS4A1 | 簇 3 | 6 |
| NKG7 | 簇 0 / 簇 5 | 2 / 2 |

与教程对照：QC 后细胞数同为 2638；本次得到 9 个 Leiden 标签，教程约 8 簇。多出的第 9 簇仅 12 个细胞，top marker 为 PPBP、PF4、SDPR、GNG11，呈血小板特征。在本次 QC / HVG 数据上改用教程的 `n_pcs=40` 与默认 Leiden 参数仍得到 9 簇，说明差异主要来自 HVG flavor（`seurat` vs `seurat_v3`）、`n_pcs` 50 vs 40、Leiden 参数（`igraph`、`n_iterations=2`、seed 42）以及未做 `regress_out`，而非 QC 阈值本身。本次不逐点比对 UMAP 坐标，也不比对簇编号顺序。以上数值未计算置信区间，也不做「与论文一致 / 超越」的结论。

### 聚类稳定性

在同一份 QC / 归一化 / 降维产物（2638 cells、同一邻居图，`neighborsReused=true`，未重做 HVG / PCA / 邻居图）上只重跑 `ScClustering`，以 `resolution=1.0`、`seed=42` 的 1.0.3 结果为 baseline，比较各组合相对 baseline 的 ARI 与簇数（运行 ID `p4-pbmc3k-stability-20261122T075054Z`，产物为同目录下 `07-stability.json`）。resolution 扫描用 `ScClustering` 1.0.3，seed 扫描用新增的 `ScClustering` 1.0.4：

| 组合 | 算子版本 | resolution | seed | 簇数 | ARI（相对 baseline） |
| --- | --- | --- | --- | --- | --- |
| baseline | 1.0.3 | 1.0 | 42 | 9 | 1.0000（参照） |
| res 0.8 | 1.0.3 | 0.8 | 42 | 8 | 0.7075 |
| res 1.0 | 1.0.3 | 1.0 | 42 | 9 | 1.0000 |
| res 1.2 | 1.0.3 | 1.2 | 42 | 10 | 0.9412 |
| seed 42 | 1.0.4 | 1.0 | 42 | 9 | 1.0000 |
| seed 43 | 1.0.4 | 1.0 | 43 | 9 | 0.9146 |
| seed 44 | 1.0.4 | 1.0 | 44 | 9 | 0.9106 |

口径说明：ARI 用 `sklearn.metrics.adjusted_rand_score` 计算，各组合与 baseline 的标签按 `cellId` 逐细胞对齐（同一 2638 个细胞，顺序校验一致）；ARI 只衡量标签一致性（对簇编号置换不敏感），不代表生物学正确性。seed 扫描前先用 1.0.4 跑 `seed=42`，其聚类归属 CSV 的 SHA-256 与 1.0.3 baseline 完全一致（`af7c2b573ddd6958e9d57410c5832a7030b141766e744573c7b8be3d75803592`），说明 1.0.4 未改变既有默认行为，跨 seed 差异可归因于随机种子本身。resolution 扫描覆盖 0.8 / 1.0 / 1.2，seed 扫描覆盖 42 / 43 / 44，共 6 次独立聚类，均来自同一邻接图。

### 运行产物

以下文件位于本地运行产物目录（未随仓库分发）：

| 文件 | 内容 |
| --- | --- |
| `run-summary.json` | 运行元数据、每步参数与版本、QC / 聚类 / DE 结果汇总 |
| `results-summary.json` | 与上表同源的扁平化结果摘要 |
| `05-clustering_report.json` | 簇数 9 与逐簇细胞数 |
| `06-markers.csv` | 逐簇 Wilcoxon marker 表（`group`、`gene`、`score`、`logFoldChange`、`pValue`、`pAdj`） |
| `05-umap_clusters.png` | 按簇着色的 UMAP 图 |
| `06-dot_plot.png` | 逐簇 marker dot plot |
| `07-stability.json` | resolution 0.8 / 1.0 / 1.2 与 seed 42 / 43 / 44 的聚类稳定性扫描（ARI、簇数、产物哈希） |
| `07-stability/*.csv` | 各组合的聚类归属（cellId → cluster），SHA-256 与 `07-stability.json` 一致 |

## 局限性

- **版本敏感**：Scanpy、igraph、leidenalg 的版本变化会改变默认行为与聚类结果；本次固定为 Scanpy 1.12.5，其他版本结果不可直接比较。
- **簇数差异**：本次 9 簇，教程约 8 簇；第 9 簇仅 12 个细胞，属于小簇，簇号与簇边界不承诺与教程逐一对应。
- **HVG flavor 差异**：本次只能用 `seurat`，教程可用 `seurat_v3`；HVG 集合不同会传导到 PCA、邻居图与聚类。
- **未做 `regress_out`**：教程历史版本做过回归去除技术协变量，本次未做，部分结构差异可能来源于此。
- **种子与 resolution 敏感**：聚类稳定性只覆盖目前测过的范围——三个 resolution（0.8 / 1.0 / 1.2）相对 baseline 的 ARI 为 0.71 / 1.00 / 0.94，三个 seed（42 / 43 / 44）的 ARI 为 1.00 / 0.91 / 0.91；采样范围仍很窄，不能据此对聚类稳定性下一般性结论。
- **不做细胞类型注释**：平台只输出簇与 marker，簇标签不等于细胞类型结论；例如簇 3 的 MS4A1 / CD79A 只是 B 细胞相关 marker 富集，不是自动注释结果。
- **不做批次整合**：不提供去批次能力；多样本或跨批次数据不适用本流程。
- **不做双细胞识别**：不检测 doublet，簇中可能混入双细胞。
- **marker 是描述性证据**：差异表达只说明簇间表达差异，不等于功能验证或临床发现；单细胞结果不构成临床结论。
- **数据规模有限**：PBMC3k 是小规模公开数据，流程可扩展性不能由本案例推断。

## 常见问题

**为什么簇数是 9，而教程是 8？**
差异来自 HVG flavor、PCA 维数、Leiden 参数与未做 `regress_out`，本次第 9 簇只有 12 个细胞。教程的「约 8 簇」本身就是版本与参数相关的定性结果，不应通过反复调参去「凑」某个簇数；判断应看簇结构与 marker 是否可解释。

**为什么结果里没有置信区间或「与论文一致」的结论？**
本次主结果只完成一次运行；稳定性部分做了 resolution 与 seed 扫描，但没有重复采样或不同上游参数的重跑，仍无法计算有意义的置信区间。细胞数一致只说明 QC 口径对齐，不代表聚类结果与教程逐点一致。

**UMAP 看起来和教程不一样，是失败了吗？**
不一定。UMAP 是随机算法，`n_pcs`、邻居数与种子都会改变布局；本次 `nComps=50`、教程 `n_pcs=40`，坐标本来就不可逐点比较。判断应看簇的分离结构与 marker 富集。

**平台能做细胞类型注释吗？**
不能。平台输出簇与 marker 表，细胞类型判断需要人工依据 marker 与领域知识完成，本案例不提供自动注释能力。

**能处理多个样本并做批次整合吗？**
不能。平台当前不提供批次整合，也不做双细胞识别；多样本场景需在平台外处理或等待能力扩展。

**可以把聚类结果写成细胞类型组成结论吗？**
不可以。簇不是细胞类型，marker 表只是描述性差异表达；任何细胞类型或临床解读都超出本案例范围。

---

**状态：已完成一次真实运行（runId `p4-pbmc3k-tutorial-qc-20261122T072828Z`）；复现包发布、独立环境重跑与博客整理进行中**

**数据与代码可用性**：原始数据为 10x Genomics PBMC3k（10x Site Terms，仅研究用途，仓库不再分发原始文件）。复现包与 DOI 待发布，本页不填占位 DOI。
