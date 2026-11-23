# 快速开始

目标：在一台干净的机器上，跑通 **IBSI 2D 数字体模自检**，看到逐特征偏差表，并理解产物含义。

## 1. 前置条件

- Python 3.12
- [uv](https://docs.astral.sh/uv/)（依赖与虚拟环境管理）
- 网络可访问 GitHub（PyRadiomics 以钉死 commit 的源码包安装）；国内网络可配置 `UV_DEFAULT_INDEX`

```bash
# 可选：配置 PyPI 镜像
export UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/
```

## 2. 获取代码

```bash
git clone https://github.com/RadStudioTeam/RadStudio-Local.git
cd radstudio-local
```

## 3. 跑自检

```bash
bash reproductions/p1-ibsi-2d/reproduction/run.sh
```

首次运行会：

1. 在 `packages/python-sdk/.venv` 创建虚拟环境；
2. 按 `uv.lock` 安装依赖（**PyRadiomics 需要本地编译，约 3–10 分钟**）；
3. 执行 7 节点链路：读取数字体模 → 2D 预处理 → IBSI 特征 → 读取参考值 → 逐特征比对。

## 4. 预期结果

默认输出到 `runs/p1-ibsi-rerun/`（可用第二个参数改目录）：

| 文件 | 内容 | 预期 |
| --- | --- | --- |
| `07-comparison.csv` | 逐特征偏差 | 54 行（no_tolerance 35 + not_comparable 19） |
| `07-comparison_by-family.csv` | 按特征族汇总 | GLSZM 16 / NGTDM 5 / NGLDM 14 可比，GLDZM 16 不可比 |
| `run-summary.json` | 节点、参数、输入哈希、环境 | 应记录 Python/PyRadiomics 版本 |

脚本内置断言：**不应出现 pass/fail**（数字体模参考值未公布容差）。

## 5. 这是什么、不是什么

- 是：同一套算子实现与云端一致的可复现链路，输入哈希与参数可核对。
- 不是：IBSI 认证、临床验证或论文结论复现。偏差原因与口径见 `reproductions/p1-ibsi-2d/README.md`。

## 6. 常见问题

| 现象 | 处理 |
| --- | --- |
| PyRadiomics 编译失败 | 安装 `build-essential`/`cmake`/`ninja`，或改用预构建 Docker 镜像（v0.1 后续提供） |
| `uv sync` 卡在 GitHub | 配置代理或 `UV_DEFAULT_INDEX`；PyRadiomics 直链需可达 |
| 输出目录已有旧结果 | 换一个 `--run-dir`，或删除旧目录后重跑 |
| 数值与上次不同 | 核对 `uv.lock`、Python 版本与 `environment/` 登记；跨环境不保证逐位一致 |

## 7. 下一步

- 读 [执行契约](workflows/README.md) 理解 Workflow IR 与产物边界；
- 看 [自定义算子示例](../examples/custom-operators/brier-score/README.md)；
- 本地 Web UI 与 `radstudio` CLI 在 v0.2 路线图中（见 [ROADMAP](../ROADMAP.md)）。
