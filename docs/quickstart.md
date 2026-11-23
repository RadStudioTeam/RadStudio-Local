# 快速开始

RadStudio Local 有两条路径：先用官方数字体模验证链路，再用 CLI 跑自己的数据。

## 1. 前置

- Python 3.12 + [uv](https://docs.astral.sh/uv/)
- 网络可访问 GitHub（PyRadiomics 需按钉死 commit 源码构建）
- 可选：Docker（不想在宿主机装依赖时）

```bash
git clone https://github.com/RadStudioTeam/RadStudio-Local.git
cd RadStudio-Local
```

## 2. 跑通 IBSI 2D 自检（不需要真实数据）

```bash
bash reproductions/p1-ibsi-2d/reproduction/run.sh
```

首次会按 `uv.lock` 建环境（PyRadiomics 本地编译约 3–10 分钟）。预期产物在 `runs/p1-ibsi-rerun/`：

| 文件 | 内容 |
| --- | --- |
| `07-comparison.csv` | 54 行逐特征偏差（no_tolerance 35 + not_comparable 19） |
| `07-comparison_by-family.csv` | 按特征族汇总 |
| `run-summary.json` | 节点、参数、输入哈希、环境 |

**口径**：数字体模参考值未公布容差，因此只产出偏差，不产出 pass/fail，也不代表“通过 IBSI”。

## 3. 跑自己的数据（CLI）

### 3.1 准备目录

```text
my-study/
├── data/
│   ├── images/       # case1.png, case2.png, ...
│   ├── masks/        # 与图片同名；也支持同目录 _mask 后缀
│   └── labels.csv    # sample_id,label[,patient_id]
└── workflow.json     # Workflow IR，或云端导出的 workflow.json
```

支持的影像格式：`png/jpg/jpeg/bmp/tif/tiff/npy/nii/nii.gz`。标签需恰好两个类别；默认 `0/1` 为负/正，其他取值用 `--positive/--negative`。

从模板复制一份工作流：

```bash
mkdir -p my-study
cp packages/workflow-schema/examples/default-workflow.json my-study/workflow.json
```

### 3.2 安装 CLI 并检查环境

```bash
cd packages/python-sdk
uv sync
uv run radstudio doctor
```

### 3.3 运行

```bash
uv run radstudio run ../../my-study/workflow.json \
  --data ../../my-study/data \
  --out ../../my-study/out
```

产物：

```text
my-study/out/
├── manifest.json
├── events.jsonl
├── _inputs/request.json               # 本次 runner 请求（可审计）
├── _inputs/read-images-manifest.json  # 输入文件与 SHA-256
└── ...                                # 特征表/指标/图/报告
```

### 3.4 生成静态报告

```bash
uv run radstudio report ../../my-study/out --open
```

无服务端、双击 `report.html` 即可查看指标、图表与产物清单。

## 4. Docker（可选）

```bash
# 在仓库根目录构建
docker build -t radstudio-local -f packages/python-sdk/Dockerfile.runner .

docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/work" radstudio-local \
  run /work/my-study/workflow.json --data /work/my-study/data --out /work/my-study/out

docker run --rm radstudio-local doctor
```

## 5. 复现包重跑

```bash
uv run radstudio replay ../../bundle --dataset-dir ../../my-study/data --out ../../my-study/out
```

复现包需包含 `request.json` 与 `input-hashes.json`（由云端运行或 `out/_inputs/` 生成）。

## 6. 当前限制（v0.1.1）

- 只支持以 `ReadImages` 开头的影像工作流；`ReadTable`、DICOM 序列、NIfTI 序列、单细胞读取器在后续版本。
- 单机 CPU；不做 GPU、云函数派发、多用户协作。
- 本地 Web UI 计划 v0.2（2027-01）；当前用 CLI + 静态报告。
- 仅供科研与教学，不构成医疗器械，不用于临床诊断。

## 7. 常见问题

| 现象 | 处理 |
| --- | --- |
| PyRadiomics 编译失败 | 安装 `build-essential`/`cmake`/`ninja`，或改用 Docker |
| `uv sync` 卡在 GitHub | 配置代理或 `UV_DEFAULT_INDEX`；PyRadiomics 直链需可达 |
| 提示没有配到掩膜 | 按同名 / `_mask` 后缀配对，或加 `--allow-missing-roi` |
| 标签不是 0/1 | 用 `--positive` / `--negative` 指定两个类别 |
| 输出目录已有旧结果 | 换一个 `--out`，或删除旧目录后重跑 |

更多命令见 [CLI 文档](cli.md)。
