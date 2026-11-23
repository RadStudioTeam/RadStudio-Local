# 多数据源运行：绑定派生、执行隔离与受限能力

状态：B36 实施检查点，2026-10-30。面向内部开发与测试，不进入对外文档站。

## 口径

- 工作流的 DataInput 是数据绑定的唯一来源；运行不再从请求体 `datasetVersionId` 取权威值。
- 每个 DataInput 的 `parameters.datasetVersionId` 必须非空并指向本项目已冻结版本。允许不同 DataInput 绑定不同数据集，也允许两个节点绑定同一版本（各自独立暂存）。
- 单 DataInput 工作流保持原行为：`datasetVersionId` 回填，旧客户端与历史运行不需要迁移。

## 创建与持久化

`validateRunBinding` 在幂等查找、计费和入库之前按 IR 节点顺序派生绑定：

- 请求携带 `datasetVersionId` 时必须是派生集合之一，否则返回 422 `dataset.mismatch`（字段 `datasetVersionId`）；该字段只做兼容校验，不再作为权威值。
- 派生结果写入 `CreateRunRequest.DatasetBindings`（JSON 标记为 `json:"-"`，客户端不能直接提交）；单绑定时同时回填 `DatasetVersionID`。
- 迁移 052 为 `experiment_runs` 增加 `dataset_bindings`（JSONB，默认 `[]`）与 GIN 索引；PostgreSQL 与内存仓储读写同一字段，创建事务参数为第 14 个。
- `RunResponse.datasetBindings` 回显；历史运行没有该字段时前端回落到 `datasetVersionId`。
- 幂等重放、提交确认与重试校验都用绑定集合比较（节点 ID + 版本 ID；历史运行缺节点 ID 时只比版本）。绑定增删改都视为新请求。

## 执行

- `loadRunDatasets` 按绑定装载样本，同一版本被多个节点引用时只查询一次；node 模式下发 `RunnerRequest.Datasets`，单绑定仍填 `Dataset`。
- 每个 DataInput 节点用 `datasetForNode` 取自己的数据集，并以节点 ID 作为工作区子目录：多绑定时文件落在 `data/<nodeID>/`，单绑定时保持 `data/`。挂载映射键、数据集文件清单与复现包同步带前缀，避免同名样本互相覆盖。
- DataInput 输出的 `environment.dataset.id` 是该节点自己的版本 ID，Python 侧 `datasetVersionId == environment.dataset.id` 契约逐节点成立。
- 续跑时按已完成 DataInput 重建数据集文件清单与挂载映射，只包含已完成节点。
- 单体模式（`RADSTUDIO_RUN_EXECUTION_MODE=monolith`）不支持多数据源：运行创建后执行会以「单体执行模式不支持多数据源运行，请改用节点执行模式」失败，不做静默降级。

## 明确拒绝的能力

- 数据广场投稿只描述一个数据集版本：多数据源运行返回 400，Web 分享对话框只列单数据源运行。
- AI 结果分析对多数据源运行返回 `MULTI_DATASET_UNSUPPORTED`（400）。
- AI 运行提议（`assistantCheckRunIR`）要求所有 DataInput 都等于提议的 `datasetVersionId`，实际只接受单数据源工作流；多数据源提议留给后续版本。
- 多值端口（一个输入端口接多条边）已在协议 3 落地，见 [多值端口与合并算子](multi-value-ports.md)；多数据源仍是「多个 DataInput 各自单值」，两者互不替代。

## Web 展示

`apps/web/src/lib/run-datasets.ts` 提供 `runDatasetBindings` / `runDatasetLabel` / `runDatasetTitle`：绑定优先于遗留字段并去重，列表与项目页显示版本 ID（多绑定时显示「N 个数据集」，悬停标题列出逐节点绑定），运行详情按「节点: 版本」逐行展示。

## 验证证据

- Go：`multi_dataset_run_test.go` 10 项（节点顺序派生、校验器接受多 DataInput、请求派生、创建持久化、逐节点暂存目录、续跑合并、重试绑定比较、单体拒绝、广场拒绝、分析拒绝）与 `run_dataset_bindings_integration_test.go` 的 PostgreSQL 往返；全量 `go test ./...`（PG 55434 / Redis 6382）通过。
- Web：`tests/run-datasets.test.ts` 4 项，全量 `node --test` 112 项通过；`tsc --noEmit`、`eslint .` 通过。

## 未覆盖

- 多数据源运行的浏览器端到端（创建 → 执行 → 详情 → 产物下载）未实测，仅到单元与库层。
- 复现包在多数据集下的浏览器验收与分享海报文案未覆盖。
- 运行创建与 AI 预览仍不跑深度预检；深度预检虽已按 DataInput 逐节点，但创建时不强制。
- 数据广场后续版本可扩展为多数据集快照；在此之前只能分享单数据源运行。
