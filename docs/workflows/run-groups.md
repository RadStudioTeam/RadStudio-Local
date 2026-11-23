# 模型比较组：入组校验、指标聚合与缺失成员

状态：B37 实施检查点，2026-10-30。面向内部开发与测试，不进入对外文档站。

## 口径

- 比较组是实验语义上的并列，不是并发执行：当前执行器按拓扑序串行派发节点，创建或加入比较组不会触发运行、不会重跑，也不复制样本与产物。
- 一个运行最多属于一个比较组（`run_group_members.run_id` 唯一），成员关系随组或运行删除级联清理。
- 组属于项目，权限按项目角色解析：查看需 viewer，创建与加入需 editor；所有路由先经 `requireBoundAccount`，微信未绑定账号按既有规则拒绝。
- 入组是运行**完成后**的显式动作，创建运行请求未改动：三项比较事实中的评估指标只有运行结束才有产物，若在创建时选择加入组就只能事后补校验，因此本批选择事后显式加入。
- 组只保存成员资格与三项比较事实（绑定摘要、划分摘要、评估角色），指标在读取比较视图时实时读取成员运行的 `metrics` 产物，不复制进组表。

## 数据模型（迁移 053）

- `run_groups`：`id/project_id/name/dataset_bindings_digest(CHAR(64))/split_digest(CHAR(64))/evaluation_role/created_by/created_at`，索引 `idx_run_groups_project(project_id, created_at DESC)`。
- `run_group_members`：`group_id/run_id/created_at`，主键 `(group_id, run_id)`，`UNIQUE(run_id)` 保证一个运行只入一组；两列均随组或运行删除级联。
- 迁移已登记 `scripts/dev-migrate.sql`；门禁库 `b28_chain_check` 已应用并登记 053。

## 三项入组事实

### 绑定摘要

`runGroupBindingsDigest` 只按数据版本与样本内容计算：取 `effectiveRunBindings` 的版本 ID（去重、排序），逐版本列出 `imageId` 与 `imageHash`（排序），再按固定字段顺序序列化后取 SHA-256。节点 ID、DataInput 顺序和工作流布局都不参与摘要，因此同一批冻结样本的不同工作流布局仍可比较；样本内容或版本变化必然改变摘要。

### 划分摘要

`normalizeSplitPlanDigest` 从 `split_plan` 产物按内容契约读取（上限 64 MiB；分析上下文默认 2 MiB 不足），只物化实际样本归属：

- 交叉验证：逐折取 `trainIds`/`testIds`，集合排序，再按折内容排序；折编号只是算子标签，不参与比较语义。
- 留出划分：取 `partitions.train`、`partitions.validation`、`partitions.test`，缺失集合按空集处理。
- `seed`、来源路径、`identityHash`、折编号与书写顺序都被忽略；同一切分在不同运行中应得到相同摘要。

一个运行有多份内容不同的 `split_plan` 返回 `run_group_split_ambiguous`；没有划分产物返回 `run_group_split_missing`；产物不可读或格式非法返回 `run_group_split_invalid`。

### 评估角色

`deriveRunGroupEvaluationRole` 把指标产物的 `evaluation` 字段归一为：

| 原始值 | 角色 | 产品标签 |
| --- | --- | --- |
| `out_of_fold` | `out_of_fold` | 交叉验证（OOF） |
| `holdout`、`validation` | `validation` | 内部验证 |
| `external` | `external` | 外部验证 |
| `inference` | `inference` | 推理 |

`holdout` 是 DataSplit 的单次留出划分，指标语义等同内部验证。同一运行内出现多于一种角色返回 `run_group_evaluation_ambiguous`；没有可识别角色返回 `run_group_evaluation_missing`。空值与未知值不算事实，不能入组。

## API

| 方法与路径 | 权限 | 说明 |
| --- | --- | --- |
| `GET /api/v1/runs/{id}/run-group` | viewer | 反查运行所属组；未入组返回 `{"group":null}` |
| `GET /api/v1/projects/{id}/run-groups` | viewer | 列出项目内比较组（`created_at` 倒序，含 `memberCount`） |
| `POST /api/v1/projects/{id}/run-groups` | editor | body `{name,runId}`，以该运行创建组并加入，201 返回组 |
| `GET /api/v1/run-groups/{id}` | viewer | `{group,members}`，成员按加入时间排序 |
| `POST /api/v1/run-groups/{id}/runs` | editor | body `{runId}`，校验三项事实后加入，201 返回 `{groupId,runId}` |

创建与加入前还要求：运行存在且属于该项目（否则 404 `run_not_found`）、运行 `completed`（否则 409 `run_not_completed`）、运行未属于其他组（409 `run_already_grouped`）。加入时依次比较绑定、划分与角色摘要，任一不一致返回 409，错误码分别为 `run_group_binding_mismatch`、`run_group_split_mismatch`、`run_group_role_mismatch`。

其他错误：404 `run_group_not_found`、403 `project_access_denied`、400 `invalid_request`、500 `run_group_failed`。请求体上限 64 KiB。

## 比较聚合

`GET /run-groups/{id}` 的成员视图：

- `runId`、`runStatus`、`createdAt`。
- `evaluations`：每个可读 `metrics` 产物一条，含 `artifactId`、`nodeId`、`evaluation` 与**原始 `metrics` 对象**；`evaluations` 始终是数组。
- `missing`：问题列表（`code`、可选 `nodeId`、`message`），`missing` 始终是数组。

聚合只读已有评估产物，不重算指标、不推断阈值、不补零：成员未完成、运行被删除或产物不可读时只给 `missing`（`RUN_NOT_COMPLETED`、`METRICS_UNREADABLE`、`INVALID_METRICS`、`NO_EVALUATION`），不会生成占位指标。单个产物不可读不影响同运行其他评估结果；成员内评估产物读取总量按 10 MiB 预算计。成员顺序按加入时间，重试产生的新运行不会自动替换旧成员，需要手动加入（旧运行保留原状态）。

## Web 展示

运行详情页新增「模型比较」页签（`apps/web/src/components/run-group-panel.tsx`）：

- 未入组：已完成运行显示创建卡（名称默认 `比较组 <runId 前 8 位>`）与已有组加入卡；未完成运行显示「暂不能创建或加入比较组」及原因。
- 已入组：组名、评估角色徽标、成员数、绑定/划分摘要短值与悬停全值、成员卡（状态、指标 chips、缺失问题列表），下方「指标对照」表按成员评估列并排，缺值显示「—」；只有 1 列时提示加入其他运行。
- 纯函数在 `apps/web/src/lib/run-group.ts`（角色/状态标签、入组资格、成员状态、指标格式化、列行构造、错误文案），单测 `apps/web/tests/run-group.test.ts`。

## 验证证据

- Go：`run_group_test.go` 6 项（绑定摘要忽略节点与顺序、划分摘要忽略 seed/路径/折编号、角色归一、创建与加入的绑定/划分/角色/未完成/重复入组/权限校验、比较保留原始指标且失败成员不补零、反查与项目列表）与 `repository_run_group_integration_test.go` 的 PostgreSQL 往返；`go vet ./...` 退出码 0，定向用例与全量 `go test ./... -count=1 -timeout=240s`（PG 55434/Redis 6382，门禁库 `b28_chain_check` 已应用迁移 053）`ok 19.986s`。
- Web：`node --test "tests/*.test.ts"` 120/120、`tsc --noEmit` 0、`eslint .` 0。
- 真实浏览器：worktree Next dev :3457 + 自造未过期 `rad_token` cookie，`/api/v1/**` 全部路由 mock，22 项断言全绿——创建组后 3 名成员、缺失成员显示「运行缺失」、两列指标并排、AUC 0.91/0.88、缺失 PR AUC 显示「—」；加入已有组后显示该组 2 名成员；运行中状态只显示不可入组原因；1440×900 与 390×844 无横向溢出，控制台无错误。

## 未覆盖

- 组只有项目私有视图：没有分享、公开发布或跨项目比较；没有重命名、删除、退出组或更换成员的 API。
- 比较组创建与加入未写审计日志（14.3.8 的审计项留给后续批次）。
- 指标只读物理类型 `metrics` 的产物，不解析 PDF/图表；不检查评估口径细节（阈值、校准、样本去重）是否一致，只信任归一的评估角色。
- 不重算指标，也不校验指标与划分/样本的对应关系；成员运行的产物被替换或指标字段被手工改写时，比较视图会直接展示。
- 不触发执行：组内运行仍需分别创建与执行，当前串行执行器不会因组而调度或限流；没有组级并发上限。
- 重试产生的新运行不会自动进入原组，需要手动加入；旧运行若失败留在组里并显示为缺失成员。
- Web 只从单个运行详情入口进入比较；没有项目级比较列表页、指标导出或图表。
- 组内评估可有多条（多节点指标），但 Web 对照表把每条评估作为一列，不做节点维度折叠或去重。
