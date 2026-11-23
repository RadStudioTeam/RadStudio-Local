# P1 · IBSI 2D 数字体模复现包

本包是论文复现计划 P1（IBSI 2020 Radiology 影像组学标准化）的 2D 数字体模自检产物。目标是核对 RadStudio 影像组学链路与 IBSI 官方参考值的偏差，**不是**复刻论文的临床结论，也**不是**独立第三方验证（PyRadiomics 本身是 IBSI 参与软件之一）。

## 范围与结论边界

- 只跑 IBSI 1 数字体模（5×4×4 体素，2 mm，ROI 74 体素），只覆盖 `aggregation=2D` 的 54 行参考值。
- 54 行 = `no_tolerance` 35 + `not_comparable` 19；`pass`/`fail` 均为 0。
- 数字体模参考值没有公布容差，因此**没有一致率**；有容差的 350 行来自 CT 配置 A/B，未包含在本包。
- 按族相对偏差见 `figures/` 与 `metrics/by-family.csv`；数值不得转写为「通过 IBSI」。

## 目录

| 目录 | 内容 |
| --- | --- |
| `manifests/` | 4 个切片行的 image/roi 清单、输入 SHA-256、聚合哈希 |
| `predictions/` | 逐样本特征表（4 × 35） |
| `metrics/` | 逐特征比对表、汇总、按族偏差、不可比原因、运行摘要 |
| `figures/` | 图 1 / 图 2（SVG）与源数据 CSV |
| `workflows/` | 7 节点工作流与算子版本 |
| `environment/` | 运行时、依赖锁、PyRadiomics commit、种子 |
| `reproduction/` | 重跑脚本、预期产物、已知波动、干净环境重跑记录 |
| `splits/` | 不适用说明（数字体模无划分） |

## 运行

```powershell
reproductions/p1-ibsi-2d/reproduction/run.ps1
```

## 数据与许可

- 数字体模、掩膜与切片 PNG：IBSI `theibsi/data_sets`，CC BY 4.0。
- 2D 参考值：IBSI 参考手册 v10 Chapter 3 表格提取，CC BY 4.0。
- 引用：Zwanenburg A, et al. The Image Biomarker Standardization Initiative. Radiology. 2020;295(2):328-338. doi:10.1148/radiol.2020191145。

运行 ID：`p1-ibsi-phantom-selfcheck`；生成时间：2026-11-22T16:40:17.907251+00:00。
复现包尚未分配 DOI；发布后在此登记。
