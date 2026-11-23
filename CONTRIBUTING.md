# 贡献指南

感谢你愿意改进 RadStudio Local。提交前请阅读本页，尤其是**数据合规**部分。

## 可以贡献什么

| 类型 | 说明 | 入口 |
| --- | --- | --- |
| Bug / 文档修正 | 复现步骤、环境、日志、期望行为 | Issue: Bug report |
| 算子 | 按统一算子契约实现新的影像组学/统计算子，附合成数据测试 | Issue: Operator proposal → PR |
| 复现包 | 公开论文 + 公开数据的可重跑包（输入哈希、产物、已知差异） | Issue: Reproduction package |
| 本地 UI/CLI | v0.2 路线图内的功能 | 先开 Issue 讨论范围 |

## 开发环境

```bash
git clone https://github.com/RadStudioTeam/RadStudio-Local.git
cd radstudio-local/packages/python-sdk
uv sync --dev
uv run pytest -q
uv run ruff check .
```

跑 IBSI 自检：

```bash
cd ../..
bash reproductions/p1-ibsi-2d/reproduction/run.sh
```

可选重依赖（DL/单细胞）**不需要安装**：相关测试通过 `pytest.importorskip` 自动跳过。

## 提交规范

- 使用 DCO：每个 commit 加 `Signed-off-by: Your Name <you@example.com>`（`git commit -s`）。本项目不要求 CLA。
- 一个 PR 只做一件事；附测试或说明为何无法测试。
- 破坏 Workflow IR/算子契约的改动必须写迁移说明。
- 不提交生成物（`out/`、`runs/`、`.venv/`）与模型权重。

## 数据合规（硬性）

- **禁止**提交任何患者影像、DICOM/NIfTI 原始数据、姓名/ID/检查号、手机号、邮箱、院内路径或真实病例截图。
- 示例与测试数据必须为合成数据或**明确许可可再分发**的公开数据，并在 `NOTICE`/README 写明来源与许可。
- 发现疑似 PHI 时请立即停止 PR，并按 [SECURITY.md](SECURITY.md) 私下联系维护者。

## 代码风格

- Python 3.11+，`ruff` 行宽 100。
- 新增算子必须：有参数 schema、输入/输出契约、合成数据测试、错误码清晰。
- 不引入 GPL/AGPL 依赖到默认发行版；可选依赖需在 NOTICE 说明。
