# 重跑说明

## 前置条件

- Python 3.12；[uv](https://docs.astral.sh/uv/)；Windows（`run.ps1`）或 Linux/macOS（`run.sh`）。
- 仓库包含 `packages/compliance/ibsi-2d/`（体模、掩膜、切片 PNG、参考值与映射表，均为 CC BY 4.0）与 `packages/python-sdk/`（含钉死 PyRadiomics commit 的 `uv.lock`）。

## 命令（仓库根）

```powershell
reproductions/p1-ibsi-2d/reproduction/run.ps1
```

```bash
bash reproductions/p1-ibsi-2d/reproduction/run.sh
```

脚本会新建/复用 `packages/python-sdk/.venv`，按 `uv.lock` 安装依赖，再把 7 节点链路跑到独立运行目录。

## 预期产物

- `07-comparison.csv`：54 行，`no_tolerance` 35 + `not_comparable` 19；`pass`/`fail` 必须为 0（脚本内置断言）。
- `07-comparison_by-family.csv`：GLSZM 16 / NGTDM 5 / NGLDM 14 可比，GLDZM 16 不可比。
- `run-summary.json`：节点、参数、输入哈希与环境登记。

哈希见 `expected-artifacts.json`；本次干净环境重跑结果见 `clean-rerun.json`。

## 已知波动

- 特征值对 PyRadiomics 版本、SimpleITK 版本与离散化参数敏感；锁文件之外的环境不保证逐位一致。
- 本轮无随机性，同一环境重复运行应得到相同数值；若不同，优先核对 `requirements.lock` 与 `pyradiomics.json`。
