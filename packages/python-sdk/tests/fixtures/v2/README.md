# v2 合成夹具

> 生成：`node scripts/v2/generate-fixtures.mjs`；日期：2026-11-23。  
> 全部为合成数据，不含真实患者/受限数据。

| 文件 | 用途 |
| --- | --- |
| `geo/rnaseq_counts.csv` | 12 基因 × 8 样本 counts（B vs A：g1/g2 上调、g3/g4 下调） |
| `geo/rnaseq_tpm.csv` | 同数据 TPM（按样本归一化） |
| `geo/rnaseq_samples.csv` | 样本/受试者/分组/批次/年龄/性别/生存 |
| `geo/microarray_matrix.csv` | 10 探针 × 8 样本 log2 强度 |
| `geo/gpl_subset.csv` | 探针 → 基因符号 |
| `bulk/expected_de.json` | 预期差异方向（仅方向性测试） |
| `notebook/input.csv` | notebook/代码节点最小输入 |

## 约束

- 夹具只用于读取/归一化/DEG/富集/生存的单元与集成测试，不用于性能/真实效应结论。
- 修改生成器后必须重跑并提交 `manifest.json`；测试按哈希校验。
- 影像 2D/3D 夹具为最小合成体模，只验证格式/读取/几何，不用于分割精度或临床结论。
