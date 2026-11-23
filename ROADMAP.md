# 路线图

> 只列方向与顺序，不承诺日期；以 Release Notes 为准。

## v0.1.0-beta（2026-12-15 技术预览）

- Python SDK + 单机 runner + CPU 影像组学算子
- Workflow IR / Operator / Artifact / Event Schema
- IBSI 2D 复现包与自检脚本
- 最小 CLI：`radstudio run` / `replay` / `doctor`
- Docker runner 镜像（linux/amd64 + arm64）
- 文档：quickstart、执行契约、算子开发、复现指南

## v0.2.0（2027-01-15 正式发布）

- **本地 Web UI（单用户）**：数据选择 / 模板 / 参数表单 / 运行 / 结果 / 设置
- `radstudio ui` 一键启动，本地静态报告
- Windows 通过 Docker Desktop 支持
- 模板库 3–5 个（IBSI 2D、BUSI 分割+组学、基础建模）

## v0.2.x

- CLI 完善（init/doctor/cloud 链接）
- Windows 便携包评估
- 单细胞可选依赖的独立镜像评估（GPL 合规）

## v1.0 之前

- IR/算子契约兼容承诺
- 算子贡献与模板共享机制
- 论文复现案例持续补充

## 非目标

- 多用户/权限/计费/云端协作
- 拖拽式工作流编辑器（编辑留在云端）
- 临床诊断、医疗器械注册
- 患者数据托管
