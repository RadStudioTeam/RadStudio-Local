# 多值端口与合并算子：协议 3、能力协商与 FeatureTableJoin

状态：B38 实施检查点，2026-10-30。面向内部开发与测试，不进入对外文档站。

## 口径

- 端口 `cardinality` 只有三种取值：空（等同单值）、`one`、`many`。现有内置算子保持单值声明，只有显式声明 `many` 的端口接受多条入边。
- 多值端口允许多条来自不同上游输出的边，但同一上游节点的同一输出重复连到同一输入会被拒绝；服务端编排还会按 `source + sourceOutput + targetInput` 再去重一次。
- 协议 3 只在请求确实带多值时启用（`inputSets` 非空），其余请求保持协议 2，旧算子镜像与历史运行的证据链不受影响。
- 合并/对齐必须是显式算子，按稳定键执行并报告保留、丢弃、缺失与重复；禁止按行号拼接。当前稳定键只有 `imageId`。

## 协议与能力协商

- 服务端 `OperatorInvokeRequest.inputSets map[string][]string`（`json:"inputSets,omitempty"`）；`protocolVersionForRequest` 仅在 `len(InputSets) > 0` 时返回 3，否则使用常量 `operatorProtocolVersion = 2`；object 与 mount 两种传输都使用该函数，不各写一套版本判断。
- Python serve 的 `PROTOCOL_VERSION = 3`、`SUPPORTED_PROTOCOL_VERSIONS = (1, 2, 3)`；更高版本直接返回 400 `unsupported protocolVersion`，不做降级，也不静默忽略字段。
- 能力守卫 `supportsDeclaredManyInputs(base, variant)`：基础版本声明 `many` 而云 `-fc` 变体没有同样声明时，`resolveOperatorForNode` 保留 docker 回退，不把 `-fc` 作为执行目标，避免多边被折叠成单值。
- 因此部署期为多值算子登记 `-fc` 变体时，必须把 `inputs` 里的 `"cardinality": "many"` 原样复制；漏掉它会让云端多值派发退化为 docker（或直接不可用），且不会有报错提示。
- 单体 runner 无法按值隔离工作区：遇到声明 `many` 的输入直接抛 `operator.input_invalid`「单体 runner 不支持多值端口：…」，不做单值截断。

## 注册与图校验

- 注册校验只允许 `""|"one"|"many"`，其他值返回「inputs 端口 cardinality 只能为空、one 或 many」。
- 四份 `operator-request`/`execution-contract`/`operator-catalog-snapshot` schema（`packages/operator-schema`、Python 内置副本、`services/api/operator_schemas`）同步登记端口 `cardinality` 枚举与请求 `inputSets` 形状：对象、键为非空端口名、值为至少一个非空字符串引用的数组。
- 图校验 `validateWorkflowDefinitions` 的 `edge.multiple` 只对非 `many` 端口生效；`many` 端口允许来自不同上游的多条边，但 required 的多值端口零边仍报 `input.required`。
- Web `connectionError`：`many` 端口允许不同上游输出，同一上游同一输出重复报「该输入已连接同一上游输出」，单值端口第二条报「该输入已有连接」；端口类型、内容契约与环检测不变。

## 编排、暂存与证据链

- `groupNodeInputs` 把入边拆成单值 `inputs` 与多值 `inputSets`：多值 identity 为 `source + "\x00" + sourceOutput + "\x00" + targetInput`，重复去重后按 `source → sourceOutput` 稳定排序，保证同一张图每次都产生相同请求摘要。
- `stageNodeInputSets` 把第 index 个值（0 基）复制到 `inputDir/<port>/<index>/`，清单间接引用随值复制到各自子目录；`expectedInputSetRefs` 与 `rewriteInputSetRef` 生成并核对改写后的引用，同名产物在多上游之间不会互相覆盖。
- 预检 `preflightNodeInputs` 纳入多值引用；`validateInputContents` 对 `inputSets` 逐端口逐值校验内容契约，错误带 `input <port>[<index>]`。
- 执行上下文新增 `inputSetReferences`；逐节点请求审计（`recordedNodeRequest.InputSets`）与恢复校验（`validateRestoredArtifactEvidence`、`validateNodeInvocationEvidence`）都对比端口数、每端口长度与逐项引用。
- `freezeInvocationContract` 只在 `inputSets` 非空时把 `inputSets` 纳入摘要，旧协议 2 运行续跑时摘要保持不变。

## Python 读取面

- `OperatorRequest.inputSets` 在 `from_dict` 中校验形状：必须是非空名称到引用数组的对象，值必须是非空字符串数组；同一端口不能同时出现在 `inputs` 与 `inputSets`（`inputSets port repeats single-value input`）。
- `OperatorContext.input_set(name)` / `input_set_json(name)` 按声明顺序返回值列表；端口未提供时抛 `ValueError("Missing required input set: <name>")`。
- `validate_input_sets` 拒绝未声明端口、单值端口混入、必需多值缺失、空列表、类型不符、哈希不符与内容契约失败；`_ports` 对 `many` 端口要求只能经 `inputSets` 提供，并跳过其单值必填检查。
- `validate_outputs` 把多值引用并入透传判定：直接透传上游多值输入的产物不会被误判为新增输出。

## FeatureTableJoin@1.0.0

- 注册：迁移 `054_feature_table_join.sql` 插入 `FeatureTableJoin@1.0.0`（docker runtime，输入 `features` 声明 `"cardinality":"many"` + `radstudio/feature-table@1` 契约），已登记 `scripts/dev-migrate.sql`；`packages/operator-schema/operators.json` 与 Python SDK 目录同步登记。
- 参数（`additionalProperties:false`）：`joinKey` 仅 `imageId`（稳定键，禁止按行号对齐）、`join` 为 `inner|left`、`conflict` 为 `error|prefix`；默认 `imageId` / `inner` / `error`。
- 语义：第一张表为锚；`inner` 取所有输入共有的 `imageId`，`left` 保留锚全集并以 `null` 补齐缺失列；列名冲突时 `error` 直接失败，`prefix` 以 `t<index>__` 加前缀，前缀后仍与其他列冲突继续拒绝。
- 一致性：同一 `imageId` 的 `patientId`/`label` 取首个非空值，两个非空值不一致直接拒绝；`null` 标签采用其他表的已知值；缺失行在 `left` 下为 `null`。
- 重复：任一输入出现重复 `imageId` 直接拒绝（内容契约会在算子执行前也拦截一次），报告不复述可能被静默修正的重复。
- 报告 `join-report.json`：`rules:"feature-table-join/1"`、参数回显、`kept/dropped/missing` 汇总、`duplicates:0`、`featureNames`、`renamed` 与逐输入 `records/uniqueKeys/columns/kept/dropped/missing`（`missing` 以锚键集为基准）。
- 输出：`features.json`（`feature_table`）与 `join-report.json`（`report`）；单输入为恒等合并。

## Web 连线与展示

- `Port` 增加 `cardinality`；`inputConnectionCounts` 按 `target/targetInput` 计数。
- 节点 `many` 输入显示「N 路」徽标，tooltip 为「多值输入 · 可连接多个上游输出，当前 N 路」；参数摘要补 `joinKey`/`join`/`conflict` 中文标签与枚举。
- 界面不展示服务端的排序结果，也没有调整多值顺序的入口；值顺序固定为 `source → sourceOutput` 稳定排序。

## 验证证据

- Go：`multi_value_ports_test.go` 10 项（多边需 many、注册 cardinality、拆分去重与稳定排序、逐值目录暂存、摘要含 inputSets、协议版本、恢复证据、参数/目录快照接受 many、`-fc` 缺声明回落 docker）；全量 `go test ./...`（PG 55434 / Redis 6382，门禁库 `b28_chain_check` 已应用并登记迁移 054）`ok 19.235s`。
- Python：`tests/test_feature_join.py` 15 项与 `test_operator_framework.py` 新增 4 项（inputSets 形状、Context 读取、serve 接受协议 3、拒绝更高版本）；全量 `uv run --frozen pytest` `524 passed, 1 skipped`。
- Web：`tests/workflow.test.ts` 补多值连接与计数用例；全量 `node --test "tests/*.test.ts"` 122/122、`tsc --noEmit` 0、`eslint .` 0。
- 真实浏览器（worktree Next dev :3457 + 自造未过期 `rad_token` cookie，`/api/v1/**` 全部路由 mock）：16 项断言全绿——`FeatureTableJoin` 多值输入显示「2 路」与 tooltip、连第三个上游变「3 路」/3 条边、同一上游重复被拒且计数与边数不变、端口类型不匹配被拒、单值端口首条成功第二条「该输入已有连接」、1440×900 与 390×844 无横向溢出、控制台无错误；截图核验「特征表 * 3 路」与三条入边。

## 未覆盖

- 真实 `-fc` 多值变体与 object/mount 两种传输的多值端到端未验证；仓库尚无 `-fc` 注册行，部署期须原样复制 `"cardinality":"many"`。
- 单体 runner 的多值拒绝分支有实现但无独立测试。
- 只有 `FeatureTableJoin` 一个多值算子，稳定键只支持 `imageId`，没有外连接、模糊对齐或按列值连接。
- 没有每端口最大路数、总大小或耗时预算；值数量只受图边数与注册校验约束。
- 多值顺序由服务端按 `source → sourceOutput` 固定排序，界面不展示也不允许调整。
- 直接构造 IR 时同一上游同一输出的重复边会被静默去重，不返回错误。
- `report` 端口没有内容契约；合并报告不参与运行门禁，也不阻断下游节点。
- 没有跨 DataInput 的自动连接，也没有把多个 DataInput 直接接到同一多值端口的专用模板。
- 只验证了 1440×900 与 390×844 两个宽度。
