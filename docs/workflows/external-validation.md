# 外部验证：ModelApply 派发前的来源核验与证据

状态：B39 实施检查点，2026-10-31。面向内部开发与测试，不进入对外文档站。

## 口径

- 外部验证不是新的运行类型，也不改变图的执行方式：它是 `ModelApply` 节点在派发**之前**的一道内容守卫。只有运行快照开启了内容契约（`ContentContracts=1`）、节点类型为 `ModelApply`、且该算子版本的 `model` 输入绑定 `radstudio/model@2` 时才生效。
- 判定只有三态：
  - `verified`：训练与外部两侧输入追溯到**不同**数据源，且样本与患者均无重叠；
  - `holdout`：两侧追溯到**同一**数据源（例如同一数据集版本内自己划分的训练/留出），此时允许重叠，但不能对外声称独立外部验证；
  - 拒绝（不出证据、节点失败）：两侧独立却存在样本或患者重叠，或校验链任一环节不成立。
- 信息不足一律拒绝，不降级为 `verified`：来源不可追溯、外部表为空、缺标签、特征列不匹配、外部表与训练表同文件，都按拒绝处理。
- 本文只覆盖 `radstudio/model@2`（表格模型）。`model@1` 没有训练血缘，`model@3` 是影像血缘，二者都不触发本守卫。

## 触发条件

服务端在节点输入暂存完成、`OperatorInvokeRequest` 组装之前调用 `validateNodeExternalValidation`（`services/api/external_validation.go`），同时满足以下条件才进入校验：

1. 运行快照的 `ContentContracts` 开启（旧快照不拦截，保持既有行为）；
2. `node.Type == "ModelApply"` 且算子定义可解析；
3. 算子 `inputs` 同时存在 `model`（契约 `radstudio/model@2`）与 `features` 端口。

任一条件不满足返回 `(nil, nil)`，不写证据事件。守卫只读运行快照与节点输入工作区，不再查注册中心，也不新增数据库表或迁移。

## 校验链

1. **模型血缘**：按产物引用读取模型 JSON，核对内容寻址 SHA256；用端口契约解码后要求 `featureNames`、`fit` 对象、`fit.features`（训练特征表引用）与 `fit.folds[].trainedImageIds`（各折训练样本，取并集去重保序）。任一项缺失即 `external_validation.lineage_invalid`。
2. **训练特征表**：读取 `fit.features` 指向的产物（同样核对哈希），按 `radstudio/feature-table@1` 解码；`featureNames` 必须与模型声明**逐项相等**（顺序敏感），`records` 按 `imageId` 建索引。
3. **外部特征表**：读取 `features` 端口引用（此时已隔离到 `features/` 子目录），按 `radstudio/feature-table@1` 解码；必须包含模型的全部特征列（允许额外列），记录数必须大于 0，每条记录必须有 `label`。
4. **同文件排除**：外部表与训练表的 SHA256 相同直接拒绝 `external_validation.training_reuse`，防止把训练表再喂一遍冒充外部验证。
5. **来源追溯**：沿 `model` 与 `features` 两个端口的入边分别做 BFS，收集全部 `DataInput` 祖先（按 IR 节点顺序返回），并为每个来源解析生效的数据集版本。解析顺序：本次派发请求的单数据集绑定 → 运行级精确节点绑定 → 单条通配绑定 → 请求中的精确绑定；都拿不到时 `datasetVersionId` 留空，按节点身份参与比较。任一侧追溯不到 `DataInput` 即 `external_validation.source_untraceable`。
6. **同源判定与重叠**：对两侧来源做交叉比较，只要存在同一绑定键（数据集版本优先，无版本时按节点身份）即为同源。样本重叠按 `imageId` 交集计数，患者重叠按患者键计数——记录带 `patientId` 时用 `patient:<id>`，否则退回 `image:<imageId>`，避免多个无患者号的样本被误并成同一患者。非同源且任一重叠大于 0 → `external_validation.source_not_independent`；同源重叠容忍并记为 `holdout`。

## 拒绝码

| 错误码 | 触发场景 |
| --- | --- |
| `external_validation.input_missing` | `model` 或 `features` 输入为空 |
| `external_validation.lineage_invalid` | 模型读不到/哈希不符/契约不通过、缺 `fit` 或训练样本、训练表读不到或特征列与模型不一致 |
| `external_validation.external_invalid` | 外部表读不到/哈希不符/契约不通过/不是 JSON 对象 |
| `external_validation.feature_mismatch` | 外部表缺少模型的某个特征列 |
| `external_validation.empty` | 外部表没有样本 |
| `external_validation.label_missing` | 外部样本缺标签 |
| `external_validation.training_reuse` | 外部表与训练表是同一份文件（SHA256 相同） |
| `external_validation.source_untraceable` | 任一侧输入无法追溯到 `DataInput` 祖先 |
| `external_validation.source_not_independent` | 两侧独立但存在样本或患者重叠 |

错误经 `recorder.nodeFailed` 落为节点失败事件，`errorCode` 使用上表的值；节点终止，下游不再执行。守卫在派发前失败，因此不会出现「算子已执行但证据被拒」的半成品节点。

## 证据事件与运行投影

- 通过时先发 `node.external_validation`，载荷为 `{"nodeId": "...", "externalValidation": {...}}`，**早于**该节点的 `node.started`；失败时不发证据事件。
- 报告 `version` 固定为 `1`，字段是稳定公开契约：
  - `status`：`verified` 或 `holdout`；
  - `model` / `trainingFeatures` / `externalFeatures`：`path` + `sha256`（外部表路径已含 `features/` 前缀）；
  - `trainingSamples` / `externalSamples` / `sampleOverlap`；
  - `trainingPatients` / `externalPatients` / `patientOverlap`；
  - `trainingSources` / `externalSources`：`nodeId` + `datasetVersionId`（可能为空）。
- `deriveRunNodes`（`services/api/run_worker.go`）把事件投影到 `RunNodeState.ExternalValidation`；只接受 `version=1` 且状态为 `verified`/`holdout` 的载荷，非法载荷不会清空已有证据。运行详情 API 因此无需新增端点。
- 事件只记录判定结果，不复制特征表内容或患者明细，载荷里只有计数与节点/版本标识。

## 输入暂存与隔离

- `ModelApply` 消费 `model@2` 时，`features` 端口暂存到 `inputDir/features/`，引用同步改写为 `features/<path>`；否则当外部表与模型血缘训练表同名（如都叫 `features.json`）时会互相覆盖。Node 工作区、算子请求里的 `inputs` 与续跑上下文看到的都是改写后的引用。
- 模型 JSON 暂存时除清单间接引用外，还会递归收集 `weights` 与 `fit.folds[].weights` 里的 `{path, sha256}` 对象，把权重文件复制到模型所在目录并校验哈希；越界路径或哈希不符直接拒绝。旧模型没有权重对象时行为不变。
- 续跑恢复时先对保存的引用做一次幂等改写（已在 `features/` 下的不再重复加前缀），保证证据校验与首次执行逐字一致。B39 之前产生的续跑上下文用的是未隔离引用，恢复会在暂存阶段报错，需要重新运行。

## Web 展示

运行详情页（`apps/web/src/app/projects/[projectId]/experiments/[runId]/page.tsx`）读取节点状态里的 `externalValidation`：

- 节点徽标：`verified` → 「外部验证已核验」（success），`holdout` → 「同源留出验证」（warning）；未知版本或不认识的状态不展示，工具提示为模型产物路径。
- 证据列表「外部验证（ModelApply）」逐节点展示样本/患者计数与重叠数，并分别列出「训练来源：…；外部来源：…」；来源名缺失时退回节点 id，数据集未绑定显示「数据集未绑定」，两侧都为空显示「未追溯」。
- 纯函数在 `apps/web/src/lib/run-external-validation.ts`，只做展示映射，不重新计算或推断结论。

## 验证证据

- 单元测试 `services/api/external_validation_test.go`：独立来源 `verified`、同源 `holdout`、同源重叠容忍、12 项拒绝场景、覆盖 9 个错误码（缺输入、模型不可读、血缘不一致、外部表坏/缺文件、特征列不匹配、空表、缺标签、训练表复用、来源不可追溯、样本重叠、患者重叠）、不适用节点跳过、事件载荷回读、`deriveRunNodes` 投影、隔离子目录与引用改写、模型权重暂存、数据集版本解析优先级。
- 端到端 `services/api/external_validation_node_test.go`：真实执行一条 `数据 → 特征表 → 训练 → ModelApply` 的 node 模式运行，断言外部表确实落到 `features/` 子目录、模型/训练表/权重文件齐全、证据事件恰好一条且早于 `node.started`、报告计数与来源正确、运行详情投影与事件逐字段相等。
- 前端 `apps/web/tests/run-external-validation.test.ts` 5 项：只有 v1 且已知状态才展示、两种状态各自文案与色调、概览同时给出两侧计数与两个重叠数、来源缺失回退、证据列表只保留有证据的节点并分列两侧来源。
- 门禁：`go build ./...`、`go vet ./...` 退出码 0；全量 `go test ./... -count=1`（PG 55434 / Redis 6382）`ok github.com/radstudio/api 19.298s`；前端 `node --test "tests/*.test.ts"` 127/127、`tsc --noEmit` 0、`eslint .` 0。

## 未覆盖

- 守卫只在内容契约开启时生效；旧快照、单体 runner 与 Python 算子内部都没有同样的检查，绕过 API 直接跑算子不会产生证据。
- 只覆盖 `radstudio/model@2`；影像模型（`model@3`）与无血缘模型（`model@1`）不适用。
- 不支持跨运行复用模型：外部验证必须在同一次运行内、且两侧都能追溯到本次运行的 `DataInput`；从别的运行导入模型或特征表会被判为来源不可追溯。
- 患者重叠依赖特征表里的 `patientId`；外部表没有患者号时退回 `imageId`，患者级泄漏只能识别样本号相同的情况，识别不了同一患者不同检查号。
- 只有样本/患者重叠这一条独立性判据，没有设备、扫描仪、采集时间、站点或标签分布的漂移检查，也不做统计显著性。
- `preflightNodeInputs` 不理解模型权重对象，权重文件缺失只能在暂存阶段暴露；完全缺失的输入会先落 `node.input_staging_failed`，走不到外部验证的具体拒绝码。
- 续跑恢复不重新发证据事件，证据以首次执行写入的事件为准；B39 之前的续跑上下文因引用改写而不可恢复。
- 同名祖先产物在暂存时按就近优先复制，哈希不符才回溯，没有跨祖先的全局哈希索引。
- Web 只有单元测试与类型/ lint 门禁，尚未做真实浏览器端到端验收；运行详情页的证据展示依赖运行轮询拿到节点状态，不额外请求。
- 与模型比较组尚无联动：同一运行同时含 OOF 指标与外部验证指标时，入组仍会因评估角色不唯一被拒。
