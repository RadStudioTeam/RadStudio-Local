# 命令行（CLI）

`radstudio` 是本地版的主入口之一（另一个是 v0.2 的本地 Web UI）。它把「工作流 JSON + 本地数据目录」变成一次可追溯的运行。

## 安装

```bash
git clone https://github.com/RadStudioTeam/RadStudio-Local.git
cd RadStudio-Local/packages/python-sdk
uv sync
uv run radstudio doctor
```

`uv sync` 会安装 `radstudio` 命令；也可用 `uv run python -m radstudio.cli ...`。预构建镜像/Windows 说明见 [README](../README.md#快速开始)。

## 数据目录约定

```text
my-study/
├── data/
│   ├── images/          # case1.png, case2.png, ...
│   ├── masks/           # case1.png, case2.png, ...（与图片同名）
│   └── labels.csv       # sample_id,label[,patient_id]
└── workflow.json        # Workflow IR，或云端导出的 workflow.json
```

也支持图片与掩膜混在同一目录（按 `_mask` / `_roi` / `_seg` 后缀识别）。支持的影像格式：`png/jpg/jpeg/bmp/tif/tiff/npy/nii/nii.gz`。

`labels.csv` 至少两列：样本 id（默认 `sample_id`，也认 `id`/`image`/`case_id`）与标签（默认 `label`）。标签需恰好两个类别；默认把 `0/1` 映射为负/正，其他取值请用 `--positive` / `--negative` 指定。

## run

```bash
uv run radstudio run workflow.json --data ./data --out ./out
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--data` | 数据目录（默认 `./data`） |
| `--out` | 输出目录（默认 `./out`） |
| `--images` / `--masks` / `--labels` | 覆盖默认子目录/文件名 |
| `--id-column` / `--label-column` / `--patient-column` | 标签表列名 |
| `--positive` / `--negative` | 非 0/1 标签时指定正负类 |
| `--allow-missing-roi` | 允许没有掩膜（无 ROI 工作流） |
| `--reader-node` | 多个 ReadImages 节点时指定一个 |
| `--target-node` | 只跑到指定节点（调试/冒烟） |
| `--quiet` | 不打印逐节点进度 |

产物：

```text
out/
├── manifest.json                 # 产物清单
├── events.jsonl                  # 运行事件（逐节点）
├── _inputs/request.json          # 本次 runner 请求（可审计）
├── _inputs/read-images-manifest.json
├── _inputs/run-meta.json
└── ...                           # 特征表/指标/图/报告等节点产物
```

## report

把输出目录渲染成单文件静态 HTML（无服务端、双击即看）：

```bash
uv run radstudio report ./out --open
```

报告包含运行信息、指标表、图表和产物清单；不构成临床诊断依据。

## replay

用云端/本地导出的复现包（含 `request.json` 与 `input-hashes.json`）在本地重跑：

```bash
uv run radstudio replay ./bundle --dataset-dir ./data --out ./out
```

## doctor

```bash
uv run radstudio doctor
```

检查 Python、numpy/scipy/scikit-learn/Pillow/PyRadiomics 是否就绪。

## Docker

```bash
docker build -t radstudio-local -f packages/python-sdk/Dockerfile.runner .
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/work" radstudio-local \
  run /work/workflow.json --data /work/data --out /work/out
docker run --rm radstudio-local doctor
```

## 当前限制（v0.1.1）

- 只支持以 `ReadImages` 开头的影像工作流；`ReadTable`/DICOM/NIfTI 序列/单细胞读取器在后续版本。
- 单机 CPU；不做 GPU、云函数派发、多用户协作。
- 本地 UI 尚未提供（计划 v0.2）；当前用 CLI + 静态报告。
