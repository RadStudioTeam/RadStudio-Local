# 运行环境

- 运行时：本地 Python SDK（非容器 worker）
- Python：3.12.13
- 操作系统：Windows-11-10.0.26300-SP0
- git：`ed6227e62d4d`（工作区含未提交改动：True）
- 镜像摘要：**无**。本轮未使用容器 worker，因此没有镜像 digest；这与复现验收模板中的 `image.txt` 字段口径不同，属于已知限制。

复跑时以 `requirements.lock` 与 `pyradiomics.txt` 为准；干净环境重跑记录见 `../reproduction/clean-rerun.json`。
