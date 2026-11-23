# 本地 Web UI（v0.2）

单用户、离线、本机运行的科研工作台。浏览器完成数据选择、模板/参数、运行、结果与导出；不需要命令行。

## 启动

```bash
cd packages/python-sdk && uv sync
uv run radstudio ui --workspace ~/RadStudioLocal
```

默认打开 <http://127.0.0.1:8765>；`--no-browser` 不自动开浏览器，`--port` 换端口。

Docker：

```bash
docker build -t radstudio-local -f packages/python-sdk/Dockerfile.runner .
docker run --rm -p 127.0.0.1:8765:8765 \
  -v "$HOME/RadStudioLocal:/workspace" \
  radstudio-local ui --no-browser --host 0.0.0.0 --workspace /workspace
```

> 容器内必须 `--host 0.0.0.0` 才能被端口映射访问；宿主机只映射到 `127.0.0.1`，不要对外网开放。

## 五个页面

| 页面 | 能做什么 |
| --- | --- |
| **数据** | 输入本机数据目录，扫描图片/掩膜/标签，显示样本数、文件数、类别与角色 |
| **工作流** | 选择模板、编辑 Workflow IR JSON、保存/复制默认模板、修改 ReadImages 参数、查看只读 DAG |
| **运行** | 选择工作流与数据目录，后台启动；进度、事件流、CLI 日志、取消 |
| **结果** | 指标表、图表、产物清单；打开完整静态 HTML 报告 |
| **设置** | 工作区路径、默认数据目录、版本；无账号/计费/权限设置 |

数据目录约定与 CLI 相同：`data/images/`、`data/masks/`、`data/labels.csv`。

## 工作流

- 首次启动会把内置模板复制为 `workspace/workflows/default-radiomics.json`。
- 「编辑」接受原始 Workflow IR 或云端导出的 `{ir: ...}`；保存时校验 JSON 与 `nodes`。
- 「读取节点参数」可改 `imageRole`/`roiRole`/`requireRoi`/`positiveValue`/`negativeValue`。
- 只读 DAG 用服务端生成 SVG；不提供拖拽编辑（编辑在云端 Web，或直接改 JSON）。

## 运行与结果

- 运行在后台子进程执行 `radstudio run`；页面每 3 秒刷新，事件与日志实时追加。
- 运行目录：`workspace/runs/<runId>/`，输出在其 `out/`；`_inputs/request.json`、`events.jsonl`、`manifest.json` 都会保留。
- 结果页按需生成 `out/report.html`；也可从 `/report/<runId>` 直接打开。

## 安全模型

- 只监听 `127.0.0.1`；**无登录、单用户**，不要暴露到内网/公网。
- 不读取数据内容上传；页面只展示本机路径与运行产物。
- 无遥测；工作区目录由你指定。
- 需要多人协作/权限/审计时使用云端团队空间，而不是共享本机 UI。

## 当前限制（v0.2）

- 只支持以 `ReadImages` 开头的影像工作流；`ReadTable`/DICOM 序列/NIfTI 序列/单细胞读取器在后续版本。
- 单机 CPU；不做 GPU/云函数/队列。
- 不做拖拽编辑、账号、组织、计费。
- Windows 建议 Docker Desktop；原生便携包在后续版本评估。
