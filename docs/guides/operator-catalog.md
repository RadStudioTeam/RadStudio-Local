> **本地版说明**：本文改编自 RadStudio 云端版文档；涉及网页工作台、组织、计费、云函数等步骤在本地版不适用。本地操作以仓库 README、examples/ 与 docs/quickstart.md 为准。


# 算子目录

算子目录是工作流编辑器的调色板，也是运行校验的依据。按当前算子注册表（2026-11 核对），公开目录包含 **49 种算子类型、79 个版本**，按 `data` / `features` / `model` / `results` / `single-cell` 五类组织。实际可用性以工作台算子面板为准。

:::info 版本与运行时
- 下表版本为注册表中的本地容器（Docker）版本。
- 部分算子可能另有 `x.y.z-fc` 云函数版本；是否存在取决于平台配置，执行环境与本地容器不同，复现时应记录实际版本号。
- 单细胞算子已正式可用，仅有本地容器版本（1.0.3 / 1.0.4），不再按内测范围限制。
- 旧的 `DataInput` 与 `ScDataInput` 已下线；数据统一通过读取节点进入工作流。

## 1. 数据类 `data`

### 1.1 读取节点

读取节点读取数据集合或项目资源目录，输出后续节点可用的数据结构。读取参数与校验规则详见数据资源与数据集合。

| 算子 | 可用版本 | 用途 |
| --- | --- | --- |
| `ReadImages` | 1.0.0 | 读取二维图片（PNG/JPEG/TIFF/BMP/NPY），可选 ROI/掩膜配对与标签映射 |
| `ReadTable` | 1.0.0 | 读取 CSV/TSV 表格，处理编码、分隔符、主键与重复键策略 |
| `ReadSingleCell` | 1.0.0 / 1.1.0 | 读取 10x MEX 三件套或 H5AD，按文库拼接为 AnnData；1.1.0 以基因符号为 var_names 并保留 gene_ids |
| `ReadDicom` | 1.0.0 | 读取 DICOM 序列，按序列分组、排序并检查缺失层 |
| `ReadNifti` | 1.0.0 | 读取 NIfTI-1 三维/四维体数据 |

### 1.2 数据准备与校验

| 算子 | 可用版本 | 用途 |
| --- | --- | --- |
| `ImageFormatConversion` | 1.0.0 / 1.1.0 | 影像格式转换与灰度/掩膜模式处理 |
| `QualityControl` | 1.0.0 / 1.1.0 | 样本质量检查，输出 accepted / warning / blocked / excluded |
| `ROIValidation` | 1.0.0 / 1.1.0 | ROI 与影像的几何、标签一致性校验 |
| `FixedSplit` | 1.0.0 | 按数据集自带的划分列切训练/验证/测试，沿用论文原始评估协议 |
| `SegMaskApply` | 1.0.0 | 把预测掩膜写回样本 `roiPath`，原真值保留到 `groundTruthRoiPath`；空掩膜标记 blocked |
| `VolumeConversion` | 1.0.0 | 体数据与二维切片的形态转换 |
| `VolumeFilter` | 1.0.0 | 体数据滤波 |
| `VolumeMerge` | 1.0.0 | 多体数据合并 |
| `PairMasks` | 1.0.0 | 影像与掩膜配对 |
| `LabelAssociation` | 1.0.0 | 标签关联与映射 |
| `PerceptualHashDedup` | 1.0.0 | 感知哈希检测库内/跨库近重复影像，输出去重样本集与重复簇报告 |
| `ROICrop` | 1.0.0 | 按 ROI 外接框加外扩比例裁剪影像与掩膜，供全图 vs ROI 对照 |

## 2. 特征类 `features`

| 算子 | 可用版本 | 用途 |
| --- | --- | --- |
| `Preprocessing2D` | 1.0.0 / 1.1.0 | 2D 预处理（重采样、归一化、裁剪等） |
| `ImageFilterBank` | 1.0.0 / 1.1.0 | 滤波器组（如 LoG、Wavelet） |
| `IBSI2DFeatureExtraction` | 1.0.0 | 基于 PyRadiomics 的 IBSI 2D 特征提取 |
| `FeatureTableJoin` | 1.0.0 | 多特征表按样本对齐与合并 |
| `CNNFeatureExtraction` | 1.0.0 | 提取 CNN 分类头之前的 penultimate 特征，供传统分类器与融合模型使用 |

## 3. 模型类 `model`

| 算子 | 可用版本 | 用途 |
| --- | --- | --- |
| `DataSplit` | 1.0.0 / 1.1.0 / 1.2.0 | 分层交叉验证划分；高版本支持更多可配置项 |
| `HoldoutSplit` | 1.0.0 / 1.2.0 | 留出独立训练/验证集 |
| `FeatureFilter` | 1.0.0 / 1.1.0 | 方差、相关性、缺失率等折内筛选 |
| `ValidateFeatureTable` | 1.0.0 | 特征表完整性、缺失与类型校验 |
| `LogisticRegression` | 1.0.0 / 1.1.0 / 1.2.0 | 逻辑回归 |
| `RandomForestClassifier` | 1.0.0 / 1.1.0 / 1.2.0 | 随机森林 |
| `SVMClassifier` | 1.0.0 / 1.1.0 / 1.2.0 | 支持向量机 |
| `GradientBoostingClassifier` | 1.0.0 / 1.1.0 / 1.2.0 | 梯度提升分类 |
| `DLCNNTraining` | 1.0.0 / 1.1.0 / 1.2.0 | CNN 训练（torchvision / MONAI 骨干）；1.2.0 支持固定划分早停与多分类 |
| `SegUNetTraining` | 1.0.0 | MONAI UNet 逐折分割训练，输出检查点与逐折 Dice/IoU |
| `MedSAMInference` | 1.0.0 | MedSAM 零样本 box-prompt 推理，输出原尺寸掩膜与置信度（需自备权重） |

## 4. 结果类 `results`

| 算子 | 可用版本 | 用途 |
| --- | --- | --- |
| `DLCNNInference` | 1.0.0 / 1.1.0 / 1.2.0 / 1.3.0 | CNN 推理；高版本支持外部验证与多分类概率输出 |
| `GradCAM` | 1.0.0 | 对 CNN 集成逐折计算 Grad-CAM 并平均，输出叠加热力图 |
| `SegUNetInference` | 1.0.0 | 逐折概率平均后阈值化，掩膜还原到原图尺寸输出 PNG 与索引 |
| `SegmentationEvaluation` | 1.0.0 / 1.1.0 | 预测掩膜对真值掩膜的 Dice / IoU / 精确率 / 召回率 / HD95；1.1.0 增加 NSD |
| `TemperatureScaling` | 1.0.0 | 在 OOF 预测上拟合温度，输出校准后概率与 ECE / Brier / NLL 对照 |
| `EnsembleFusion` | 1.0.0 | 两模型预测按样本对齐，支持概率均值、概率乘积与多数投票 |
| `BenchmarkSummary` | 1.0.0 | 两模型判别力、校准与 95% CI 汇总，含 DeLong 与 McNemar 配对检验 |
| `IBSIReferenceComparison` | 1.0.0 | 特征表对照 IBSI 2D 参考值逐行判定：有容差判 pass/fail，无容差只报告偏差 |
| `ModelApply` | 1.0.0 / 1.1.0 | 已训练模型在新数据上的应用 |
| `Evaluation` | 1.0.0 / 1.1.0 | OOF / 外部验证评估，输出 AUC、ROC、混淆矩阵等 |
| `PaperFigures` | 1.0.0 | 出版级图表（PNG / PDF / SVG / TIFF） |
| `Report` | 1.0.0 / 1.1.0 / 1.2.0 | HTML 报告与复现包 |

## 5. 单细胞类 `single-cell`

单细胞读取使用数据类中的 `ReadSingleCell`；以下算子消费其输出的 AnnData。

| 算子 | 可用版本 | 用途 |
| --- | --- | --- |
| `ScQualityControl` | 1.0.3 / 1.0.4 | 细胞/基因过滤与线粒体比例控制；1.0.4 增加 maxGenes 上限 |
| `ScNormalize` | 1.0.3 | 归一化、log1p 与高变基因 |
| `ScDimReduction` | 1.0.3 | PCA + UMAP / t-SNE |
| `ScClustering` | 1.0.3 / 1.0.4 | Leiden 聚类；1.0.4 增加 seed 参数用于稳定性扫描 |
| `ScDifferentialExpression` | 1.0.3 | Wilcoxon / t-test 差异表达与 marker |

参数、格式与限制见单细胞分析。

## 6. 典型组合

### 影像组学基线

```text
ReadImages → ImageFormatConversion → QualityControl → ROIValidation
           → Preprocessing2D → IBSI2DFeatureExtraction → FeatureFilter
           → DataSplit → LogisticRegression → Evaluation
```

### 表格数据

```text
ReadTable → ValidateFeatureTable → FeatureFilter
          → DataSplit → LogisticRegression → Evaluation
```

### 深度学习

```text
ReadImages → Preprocessing2D → DataSplit → DLCNNTraining
           → DLCNNInference → Evaluation
```

### 体数据与分割

```text
ReadDicom / ReadNifti → VolumeConversion → VolumeFilter → VolumeMerge
                      → SegUNetTraining → SegUNetInference → SegmentationEvaluation
```

### 出版交付

```text
... → Evaluation → PaperFigures → Report
```

### 外部验证

```text
ReadImages(外部集合) → Preprocessing2D → ModelApply / DLCNNInference(1.1+)
                     → Evaluation(1.1, external) → Report(1.1+)
```

### 单细胞基础分析

```text
ReadSingleCell → ScQualityControl → ScNormalize → ScDimReduction
               → ScClustering → ScDifferentialExpression → Report
```

## 7. 执行方式

算子由平台统一调度，你不需要手动选择机器：

| 场景 | 说明 |
| --- | --- |
| 常规算子 | 使用平台默认计算资源；单细胞算子默认 2 vCPU / 4 GiB / 3600 秒 |
| 深度学习算子 | 自动分配到适合的计算资源 |
| 大规模任务 | 自动拆分并行执行并合并结果 |
| `-fc` 版本 | 若部署提供，则调用平台托管的云函数端点，长任务受函数超时约束 |

节点的实际执行方式与耗时在运行详情中可见，参数与产物不受影响。

## 8. 版本策略

- 已发布版本不可变；参数或实现变更会发布新版本。
- 工作流节点固定算子版本，升级需要显式修改并重新校验。
- 标记为「已弃用」的算子仍可执行既有工作流，但不建议用于新工作流；已下线的算子不会出现在新工作流中。
- 目录中的算子会在平台升级时新增；已发布的工作流不会因目录变化而失效。
- 云函数版本（`-fc`）与本地容器版本不是同一执行环境，复现时应记录具体版本号。

## 9. 需要新算子？

如果目录中没有合适的算法，可以在网页工作台自助开发自定义算子：写代码、定义契约、用你的数据调试，通过发布门禁后发布给本组织使用。流程与限制见自定义算子开发，可直接复制的模板见自定义算子示例，端口类型的完整清单见算子契约与错误码参考。

自定义算子默认只在你自己的组织空间内可见可用；上架到数据广场或供其他组织使用需要平台审核。GPU 运行时需要单独申请。

## 相关文档

- 数据资源与数据集合
- 工作流编辑器
- 核心概念
- 功能状态与可用范围
- 常见问题
