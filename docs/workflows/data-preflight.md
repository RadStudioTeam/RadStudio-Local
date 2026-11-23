# 数据预检：同步快检与异步深度预检

工作流校验除了图与算子定义，还要回答「绑定数据版本现在能不能跑」。这里有两层能力：

- **同步快检**：`POST /api/v1/workflows/validate` 内联执行，只读数据版本与数据集的缓存计数，2 秒预算，**不打开任何影像文件**。
- **异步深度预检**（B33）：独立的持久化作业，由后台 worker 领取执行，逐个打开样本影像做格式/维度、ROI、重复患者、标签与覆盖率画像，结果绑定草稿修订、数据版本与算子快照，漂移即过期。

两层都不改变运行门禁：QC 告警与深度预检问题只作提示，创建运行、AI 预览与重试仍只调用原 `ValidateWorkflow`。

## 同步快检

### 响应形态

```json
{
  "valid": true,
  "issues": [],
  "dataPreflight": {
    "status": "checked",
    "datasets": [{
      "nodeId": "data_input", "datasetId": "…", "datasetVersionId": "…",
      "versionNumber": 2, "status": "frozen", "frozen": true,
      "sampleCount": 100, "usableCount": 98,
      "warningCount": 8, "blockedCount": 0, "excludedCount": 2
    }],
    "checks": [
      {"id": "binding", "status": "passed", "detail": "已解析数据版本 …"},
      {"id": "project", "status": "passed", "detail": "数据集归属当前项目"},
      {"id": "usable-samples", "status": "passed", "detail": "可用样本 98 / 100"}
    ],
    "notChecked": ["影像格式、维度与文件可读性（需深度预检）", "…"]
  }
}
```

`dataPreflight` 是新增字段，旧客户端忽略即可；`valid` 仍按问题严重级别重算（任一 `error` 即 false），因此 QC 告警不会改变既有运行门禁。

### 状态与检查语义

| status | 含义 | 何时出现 |
| --- | --- | --- |
| `checked` | 所有 DataInput 的绑定都解析成功 | 至少一个绑定解析完成，且没有绑定/归属失败 |
| `partial` | 部分绑定未能解析 | 版本不存在、读取失败或跨项目，此时只对已解析版本下结论 |
| `timeout` | 快检超时 | 请求上下文被取消或超出 2 秒预算；不返回已解析行，`notChecked` 追加超时说明 |
| `skipped` | 未检查 | 图里没有可解析的 DataInput 绑定 |

每份报告固定给三项检查：`binding`（数据版本绑定）、`project`（数据集归属当前项目）、`usable-samples`（可用样本数）。检查状态只有 `passed` / `failed` / `skipped`，**没有「未检查」被渲染成「通过」的路径**。

### 数据与分级

- 可用样本 = `accepted_count + warning_count`；`usableCount <= 0` 出 `error`：样本总数为 0 用 `dataset.empty`，否则 `dataset.no_usable_samples`。
- 告警 / 阻塞 / 排除任一非零只出 `warning`（`dataset.qc_warning`），提示用户确认排除样本，不阻断运行。
- 未绑定版本仍由既有图校验出 `dataset.unbound`；未冻结由 `dataset.unfrozen`；版本不存在与项目不符由快检分别记为 `binding` / `project` 失败，并各出对应 error。
- 数据相关问题都带 `nodeId` 与 `fieldPath: parameters.datasetVersionId`，编辑器可定位到参数行。

### 覆盖率与未知项

`notChecked` 是报告的一部分，固定列出同步快检不覆盖、需要深度预检或运行后校验的项：

1. 影像格式、维度与文件可读性（需深度预检）
2. 患者与检查重复、标签分布与缺失值（需深度预检）
3. ROI 对齐与检查覆盖率（需深度预检）
4. 资源与时长估算（需深度预检）
5. 特征质量后置校验（运行完成后由 [特征表后置质量评估](feature-quality.md) 记录，不属于预检）

超时会在末尾追加「数据版本样本计数（读取超时）」。界面必须同时显示状态、覆盖率（检查了几个版本、可用多少样本）与未检查项，不能只显示一个「通过」。

### 超时与副作用

- `handleWorkflowValidate` 用 `context.WithTimeout(r.Context(), 2s)` 调 `ValidateWorkflowData`，只做 `GetByID` 级读取；超时如实返回 `timeout`，不伪造问题。
- 快检是只读的，不写草稿、不创建版本、不扣积分；创建运行、AI 预览与重试仍只调用原 `ValidateWorkflow`，不经过快检。

## 异步深度预检

### 接口

| 方法与路径 | 权限 | 说明 |
| --- | --- | --- |
| `POST /api/v1/workflows/preflight` | 项目 editor | 请求体 `{projectId, workflowId, ir?}`；`ir` 缺省时用工作流已存 IR。先跑 `ValidateWorkflow`，图不合法直接 400（不占用队列配额）；成功后 202 返回作业视图 |
| `GET /api/v1/workflows/preflight/{id}` | 项目 viewer | 返回 `{job, resultExpired, expiryReasons}`；作业的 IR 只存库，不随响应下发 |
| `POST /api/v1/workflows/preflight/{id}/cancel` | 项目 editor | 仅 `queued` / `running` 可取消；已终态返回 409 |

创建时冻结三样东西：草稿修订号 `draftRevision`、数据绑定快照（版本号、版本哈希、状态）、算子快照摘要（类型/版本/状态/运行态/资源）。结果只对这些快照成立。

配额：同一创建者最多 3 个活跃作业，同一项目最多 8 个；超限返回 429 `preflight_quota_exceeded`。

### 作业生命周期

`queued → running → succeeded | failed | canceled`，`progress` 0–100。

- API 与 worker 进程每 2 秒各跑一轮循环：回收过期租约、清理过期结果、用 `FOR UPDATE SKIP LOCKED` 领取最早的一个 `queued` 作业。多进程只会有一个领到，不需要额外的调度服务。
- 领取即写 `lease_until = now + 5min`；单作业执行预算 4 分钟。进程崩溃时租约到期由 `ReapExpired` 记为失败（提示重试），作业不会永远停在 `running`。
- 完成写库用独立 30 秒上下文：构建阶段即使已超时，结论/失败原因也必须落库。
- 成功结果保留 24 小时（`expires_at`），到期由 `DeleteExpired` 删除；`failed` / `canceled` 同样按 TTL 清理。

### 样本画像与缓存

深度预检按数据版本构建样本画像，写入 `dataset_profiles`：

- 缓存键是 `(dataset_version_id, rules_version)`，当前规则版本为 `profile-v1`；缓存有效期 24 小时。规则调整必须递增版本号，旧画像自动失效。命中时报告里 `cacheHit=true`。
- 单次画像最多 2000 个样本（按 `sampleIndex` 稳定排序），证据最多 40 条，单个文件最多读 512 KiB 头部。超出时 `truncated=true`，「样本」检查降为 warning，覆盖率期望按已画像数收敛。
- 解析 JPEG SOF、NIfTI 头、DICOM 头取得格式与尺寸；统计可读/缺失/无法解析影像、格式与尺寸是否混用、ROI 可读性与尺寸一致性、重复患者/检查分组、标签缺失与分布、输入字节总量。
- 证据只保留脱敏引用（哈希后的患者/检查/文件引用）与内部样本 ID，**不写入患者标识或真实文件路径**。

### 报告与检查项

报告固定 7 项检查，顺序为 `profile`、`samples`、`images`、`roi`、`duplicates`、`labels`、`coverage`。检查状态为 `passed` / `warning` / `failed` / `skipped`；同一项被多次赋值时取最坏（`failed > warning > skipped > passed`），未赋值的初始状态在收尾时补 `skipped`。计数为 0 且状态 `skipped` 表示**没查**，不能读作通过。

| 检查 | passed | warning | failed |
| --- | --- | --- | --- |
| `profile` | 已生成样本画像 | — | 版本不可读、跨项目、画像构建失败（报告 `partial`） |
| `samples` | 已画像 N 个样本 | 样本量超出 2000，仅画像前 N 个 | 数据集没有样本 |
| `images` | N 个影像可读 | 格式或尺寸混用 | 存在缺失或无法解析的影像 |
| `roi` | N 个 ROI 可读且尺寸一致 | ROI 与影像尺寸不一致 | 存在无法解析的 ROI；无 ROI 时为 `skipped` |
| `duplicates` | 未发现重复患者或检查 | 存在重复患者或检查 | — |
| `labels` | 标签覆盖 N 个类别 | 存在缺标签样本；或只有一个类别 | — |
| `coverage` | 可读影像 N / 应有 M | — | 可读影像少于可用样本，运行会取数中断 |

问题码：`dataset.profile_unavailable`、`dataset.empty`、`dataset.images_missing`、`dataset.images_unreadable`、`dataset.mixed_formats`、`dataset.mixed_dimensions`、`dataset.roi_unreadable`、`dataset.roi_mismatch`、`dataset.duplicate_patients`、`dataset.duplicate_studies`、`dataset.labels_missing`、`dataset.labels_single_class`、`dataset.coverage`。除 `profile_unavailable`、`empty`、`images_missing`、`images_unreadable`、`roi_unreadable`、`coverage` 为 error 外，其余为 warning。问题均带 `nodeId` 与 `fieldPath: parameters.datasetVersionId`。

报告状态：无数据绑定时 `skipped`；任一绑定无法解析时 `partial`（其余绑定照常给结论）；构建超时 `timeout` 并在 `notChecked` 追加超时说明；否则 `checked`。

资源估算是按 IR 与算子资源声明做的**估算而非实测**：节点数、输入字节（画像累计）、磁盘 `输入×2 + 512MiB`、最大 CPU、峰值内存、GPU 数、各节点超时之和与最长单节点超时。

### 结果有效期（漂移即过期）

读取作业时重新计算 `resultExpired` / `expiryReasons`，任一原因命中即视为过期，前端不得沿用旧结论：

| 原因 | 触发条件 |
| --- | --- |
| `result_missing` | 作业尚无可读结果 |
| `result_expired` | 超过 24 小时 TTL |
| `profile_rules_changed` | 画像规则版本与作业记录不一致 |
| `workflow_missing` | 工作流已删除或不可读 |
| `draft_revision_changed` | 工作流当前修订号与作业冻结的 `draftRevision` 不同 |
| `dataset_version_changed` | 数据版本被替换、重命名、状态变化，或数据集不再属于该项目 |
| `operator_snapshot_changed` | 图内任一算子行的状态/运行态/资源变化，或行已删除 |
| `freshness_unavailable` | 计算有效期时读库失败（如实标过期，不假定有效） |

### Web 展示

编辑器工具栏的「深度预检」按钮先跑一次同步校验：图不合法直接展示问题，不创建作业；校验通过才调用 `POST /workflows/preflight` 并自动展开右侧检查器。面板只在该作业处于 `queued` / `running` 时每 1.5 秒轮询一次，进入终态即停止；连续 3 次轮询失败在面板内给出错误并停止，恢复后重新点按钮即可。

- 状态与进度：`排队中`、`检查中 N%`、`深度预检已完成`、`预检失败：原因`、`已取消`；活动作业显示「取消预检」，终态不再显示。
- 过期结论不隐藏报告，但必须同时展示「结论已过期，需要重新预检」横幅与逐条中文原因，不允许把过期结果当作当前结论。
- 报告摘要写「已检查 N 个数据版本 · 可用样本 X / Y」，检查汇总写「检查：a 通过 · b 告警 · c 失败 · d 未检查」；7 项检查逐项给出状态与说明。
- 每个数据版本一行：行内按钮可定位到节点；明细包含冻结/样本/已画像/可用以及缺失、无法解析、ROI 尺寸、重复患者、缺标签等计数，并给出画像规则与截断提示；证据默认折叠，展开后只显示脱敏引用（`sha256:` 前 12 位），不出现内部样本 ID 或真实路径。
- 问题列表逐条展示中文问题标签、`fieldPath` 与说明，可点击定位节点；没有关联节点时提示需要在数据版本上处理。
- 资源估算、未覆盖项与结果生成时间一并展示；特征质量不在预检范围内，运行结束后由 [特征表后置质量评估](feature-quality.md) 在运行详情展示。
- 切换或重新载入工作流会清空面板，避免把上一个工作流的结论留在界面上。

### 验收

- Go：`dataset_profile_test.go` 5 项（统计与分桶、读失败、重复与脱敏、缓存键与 TTL、截断上限）、`workflow_data_preflight_job_test.go` 4 项（报告分级与最坏合并、作业生命周期与租约/配额/取消、过期原因、HTTP 全流程与权限）、`workflow_data_preflight_job_integration_test.go`（PostgreSQL 仓储：领取/完成/取消/租约回收/到期清理/画像缓存）。全量回归在内存与 PG/Redis 两套存储下通过。
- 迁移 `051_data_preflight_jobs.sql`：门禁库应用至 051 且重复执行幂等；全新库 001→051 重放 49 条迁移通过。
- Web：`tests/workflow-deep-preflight.test.ts` 9 项（状态/进度文案、检查最坏合并汇总、覆盖率摘要、过期原因、证据脱敏、资源估算、数据集行描述、问题标签兜底、活动态判定）；`node --test "tests/*.test.ts"` 103 项、`tsc --noEmit`、`eslint .` 通过。
- 浏览器（真实登录 + 门禁 API，合成数据集/版本/6 样本）：40 项断言全绿——校验通过后自动展开检查器、终态摘要 `已检查 1 个数据版本 · 可用样本 6 / 6`、汇总 `4 通过 · 3 告警 · 0 失败 · 0 未检查`、7 项检查明细、ROI 尺寸不一致与重复患者 2/缺标签 1 计数、证据仅 `sha256:` 前 12 位、问题可定位到 `parameters.datasetVersionId`；改 `dataset_versions.version_hash` 后出现「结论已过期，需要重新预检」且报告仍展示；创建后立即取消显示「已取消」且不展示报告；390×844 无横向溢出，控制台仅登录前预期 401。

### 尚未覆盖

- 创建运行、AI 预览与重试都不跑深度预检，问题不阻断运行；也没有「运行前二次确认」。
- 画像只读文件头部（512 KiB）判断格式、尺寸与可读性，不解码整幅像素，不校验窗宽窗位、像素统计或分割掩膜内容；超过 2000 个样本只画像前 2000 个。
- ROI 只按存储的尺寸信息比对，不做空间配准；「ROI 对齐」不等于解剖对齐。
- 重复患者/检查与标签统计依赖导入时的样本元数据，元数据错误无法被发现。
- 资源与时长是估算；没有历史运行实测数据参与。
- 对象存储里的文件是否仍可读，只在画像阶段实际打开时验证；未画像的样本不验证。
- 作业由 API/worker 进程内的 2 秒轮询执行，没有独立队列服务、优先级、进度百分比细粒度上报或跨项目调度公平性。
