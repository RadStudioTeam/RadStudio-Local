# RadStudio Local

**面向医学影像组学研究的本地可复现工具。数据留在本机，工作流是纯 JSON，产物可独立复核。**

> 状态：`v0.1.0-beta`（CLI / Docker / 复现包；本地 Web UI 在路线图中，计划 2027-01-15）
> 定位：**仅供科学研究与教学，不用于临床诊断，不构成医疗器械。**
> 上游：RadStudio（云端低代码科研平台）的本地执行与复现能力开源版。

## 这是什么

- **本地执行**：用同一套算子实现跑影像组学链路（质控 → 预处理 → IBSI 2D 特征 → 建模 → 评估 → 图表），不依赖云端。
- **可复现**：输入哈希、算子快照、参数、产物与已知波动随复现包保存；同一环境可重跑对照。
- **可移植**：工作流是带版本的 JSON（Workflow IR），可在本地与云端之间导入/导出。
- **可扩展**：按统一算子契约写 Python 自定义算子，本地调试后既可在本地用，也可提交到云端。

## 这不是什么

- 不是临床诊断软件；输出不能作为诊断依据。
- 不是云端平台的替代：**没有多用户、协作、GPU 集群、AI 助手与数据广场**。这些在云端版提供。
- 不是「一键论文」工具：它把流程变得可追溯、可复核，但不替你选题、统计或写作。

## 快速开始（v0.1.0-beta）

### 前置

- Python 3.12 + [uv](https://docs.astral.sh/uv/)
- 或 Docker（本地 Web UI / 预构建镜像在后续版本提供）

### 跑通 IBSI 2D 数字体模自检

```bash
git clone https://github.com/RadStudioTeam/RadStudio-Local.git
cd radstudio-local
bash reproductions/p1-ibsi-2d/reproduction/run.sh
```

首次会按 `uv.lock` 创建虚拟环境（PyRadiomics 需本地编译，耗时数分钟）。成功后应看到：

- `07-comparison.csv`：54 行逐特征偏差
- `07-comparison_by-family.csv`：按特征族汇总
- `run-summary.json`：节点、参数、输入哈希与环境登记

> 口径：数字体模的 54 行参考值**没有公布容差**，因此本自检只产出偏差，不产出 pass/fail，也不代表「通过 IBSI 认证」。详见 `reproductions/p1-ibsi-2d/README.md`。

### 跑自己的数据（v0.1 CLI，随 beta 逐步完善）

```bash
cd packages/python-sdk && uv sync
# 规划中的命令（v0.1 交付）：
# radstudio init  ./my-study
# radstudio run   ./my-study/workflow.json --data ./my-study/data --out ./my-study/out
```

v0.1 的 CLI 与本地 Web UI 正在开发；当前版本请以复现包与 `python -m radstudio.runner --request <request.json> --output <out>` 为准。

## 仓库结构

```text
packages/python-sdk/     SDK、runner、算子、执行契约（含 tests）
packages/*-schema/       Workflow IR / Operator / Event / Artifact / Analysis JSON Schema
packages/compliance/     IBSI 2D 数字体模与参考值（CC BY 4.0）
reproductions/           P1 IBSI 2D 复现包（输入哈希、产物、已知波动）
examples/                自定义算子示例（Brier Score）
docs/                    quickstart、执行契约（docs/workflows/）
```

## 与云端的关系

| 能力 | RadStudio Local | 云端 RadStudio |
| --- | --- | --- |
| 影像组学 CPU 链路、复现、导出 | ✅ 免费 | ✅ |
| 自定义算子本地开发 | ✅ | ✅（网页 IDE） |
| 数据管理/协作/权限/审计 | — | ✅ |
| GPU / DL / 大规模并行 | — | ✅ |
| AI 助手 / 自动编排 / 数据广场 | — | ✅ |
| 本地 Web UI（数据/模板/运行/结果） | 🚧 计划 v0.2 | ✅ 完整工作台 |

**开源版不限制功能，只限制「规模与协作」。** 需要云端时，可从本地导出的工作流无缝带入。

## 版本与兼容

- `0.x` 期间不承诺 Workflow IR 向后兼容；每次破坏性变更会在 Release Notes 给出迁移说明。
- 复现包以 `uv.lock` 与算子快照为准；跨环境不保证逐位一致，差异原因见各复现包 `known-variation.md`。

## 引用

如果本项目对你的研究有帮助，请按 `CITATION.cff` 引用；复现包与数据请同时引用 IBSI 原始论文（见 `packages/compliance/ibsi-2d/README.md`）。

## 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。**禁止提交任何患者数据、PHI 或未脱敏影像**；示例仅接受合成/公开数据。

## 许可

- 代码：Apache-2.0（见 [LICENSE](LICENSE)）
- 文档：CC BY 4.0
- IBSI 体模/参考值：CC BY 4.0（署名见 [NOTICE](NOTICE)）
- 名称与 Logo：商标保留（见 [TRADEMARK.md](TRADEMARK.md)）

## 安全与合规

- 仅用于科研与教学；不用于临床诊断。
- 本地执行，默认不上传数据；不含遥测。
- 漏洞报告见 [SECURITY.md](SECURITY.md)。
