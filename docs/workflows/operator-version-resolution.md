# 版本目录与精确解析

## 用户行为

新增算子目录继续展示每个类型最新 available 版本。现有草稿及历史快照中的节点按 type + operatorVersion 读取参数和端口，不因目录更新自动升级。

版本定义缺失时，参数面板显示不可用提示，不猜测使用最新版，也不改写现有参数。应联系管理员恢复定义或明确迁移。deprecated 定义仍可用于历史显示；能显示不等于允许新运行，服务端校验仍拒绝不可用版本。

## API

- GET /api/v1/operators：保留最新 available 目录行为。
- GET /api/v1/operators?include=versions：返回全部历史版本的 OperatorDefinition 数组，用于精确渲染。需要登录及已绑定账户。
- 新查询仅返回定义，不返回 runtime、端点、令牌或资源执行凭据。当前无 namespace/私有发布；后续加入所有权时必须同步过滤该接口，不能把全量查询用于私有算子。

## 开发约束

连线查找精确匹配节点的 operatorVersion 与定义 version。找不到任一端口或类型不一致即拒绝连接，不回退最新版本。新增目录与历史定义分开读取，避免历史/停用版本进入新增菜单。

## 验证

Go 测试 TestOperatorHistoricalDefinitionsExcludeRuntime；Web workflow.test.ts 的跨版本连接案例；Playwright workflow-versions.spec.ts 的旧参数渲染案例。浏览器案例使用模拟接口验证 UI，不能替代真实 API 或 FC 端到端。


## 当前分支验收（2026-10-26）

本批从旧分支 f8473df1 按文件迁入并重新验证，保留当前资源授权、原子预扣和重试保护。历史定义接口只返回 OperatorDefinition，测试确认运行端点与认证令牌不出现在响应。

Go 全量（独立 PG/Redis，含专用 AI 持久化）及 vet 通过，耗时 12.976s；Web Next 路由类型生成、TypeScript、59 项单测、当前 worktree ESLint 与正式 Webpack 构建通过。新 worktree 初次 tsc 缺少 LayoutProps，由 next typegen 生成官方路由类型后通过，未加入手写声明。

当前正式构建在独立 3141 端口，用合成 API 夹具完成 1600px / 390px 共四项浏览器交互：旧节点显示旧版参数并保留值；定义缺失显示可播报的恢复提示、不使用最新版字段。缺失用例初次误匹配 Next 路由播报器，改为限定节点参数面板后通过。截图位于 apps/web/.data/visual/version-*.png（忽略文件），已查看桌面参数与窄屏缺失提示。此处不宣称真实后端工作流或整体移动端布局端到端通过。

shadcn CLI 在当前 @radstudio/ui 工作区别名上解析失败，因此核对了既有 Base 组件和官方 Badge/Empty 文档；没有为 CLI 改写项目别名。缺失版本提示依据错误恢复指导提供恢复/明确升级路径并使用 role=alert，不自动迁移旧参数。

## 升级预检与改判报告（2026-10-29）

`POST /api/v1/workflows/upgrade-plan` 接收 `{projectId, ir, nodeIds?}`，只读地给出「把这张图推到当前目录最新行会发生什么」。接口不改写请求里的 IR、不写 `workflow_versions`、不生成迁移脚本；候选图仍需用户在编辑器确认后保存。需要登录及已绑定账户；`nodeIds` 缺省扫描全图，指定时只改判列出的节点，不存在的 ID 回填到 `unknownRequestedIds`。

判定口径：

- 候选只收纯数字版本，`*-fc` 部署变体即使数字前缀更新也不参与；节点只向严格更新的版本移动。
- 多轮单调推进：每轮只落下「整图校验不新增问题」的候选版本，一轮结束后重跑，直到无人前进。消费端排在产出端之前也能在后续轮次跟上，结论不依赖节点顺序。
- 基线按请求里的图计算：升级前已存在的问题按 `code+nodeID+edgeID+fieldPath` 计数容忍，不算到候选版本头上；候选被拒时按节点给出 `版本 不可用：消息（字段路径）`。
- 新版本要求新必填输入或移除现存端口时优先降级：能整体成立就用最新的可用版本（`partial`），一个都不成立则停在原版（`blocked`）。
- 参数按目标版本的 `properties/required/defaults` 收口：保留仍声明的键、丢弃已移除的键、缺失且有默认值的补默认值；缺失的必填项只记 `missingRequiredParameters`，不臆造。
- 端口消失导致的连线只在「升级前两端端口都成立」时移除，并记录 `causedBy` 与原因；历史断边原样留给校验器。
- 输出 `candidate`（预检后的 IR）、`validation` 与汇总计数，`summary.safeToApply` 即候选通过校验；`notChecked` 列出本次升级新引入、但只核对了端口契约、没有核对产出侧内容的契约（例如 `radstudio/model@2`）。

模板实测：12 个节点推到目录最新行后 10 个 `upgraded`，`DataSplit` 因上游没有 `radstudio/feature-table@1` 产出停在 `1.1.0`（`partial`），`IBSI2DFeatureExtraction` 只有 `1.0.0` 一行（`unchanged`）；`Report 1.2.0` 移除 `features`/`roc`/`confusion` 三个输入并丢弃对应 3 条边，候选 15 条边、校验通过、`notChecked` 恰为 `["radstudio/model@2"]`。已创建的运行继续按冻结快照执行，不受预检影响。

Go 测试 `workflow_upgrade_plan_test.go` 8 项覆盖模板推进、指定节点、参数收口、新输入降级、无可用版本阻断、`*-fc` 跳过与未知节点、消费端先于产出端的多轮推进与 HTTP 合约；四处变异（放宽 `*-fc` 过滤、跳过断边、取消默认值填充、清空 `notChecked`）均如期失败。


### Web 编辑器入口（2026-10-29）

工作流编辑器工具栏在「校验」与「保存草稿」之间新增「升级预检」：点击时以当前草稿的 `projectId + ir` 请求一次接口，结果在只读对话框中展示，不随草稿继续编辑自动刷新，重新点击才取新快照。对话框按节点列出 `v旧 → v新`、状态徽标、服务端 reasons 与参数/端口变更笔记（丢弃参数、补默认值、缺少必填、新增/移除输入输出、移除连线），并单列将被丢弃的连线（前 8 条）与候选校验问题、`notChecked` 说明。

「应用候选图」仅在 `summary.safeToApply && validation.valid && 存在 upgraded/partial` 时启用；应用只把候选的 `nodes/edges` 写回草稿（保留 layout、名称与 revision），走编辑器统一撤销栈与自动保存，因此可用「撤销」整体退回，仍需用户显式保存草稿/保存版本并运行校验。历史快照只读态与保存冲突时入口禁用。

前端纯逻辑在 `apps/web/src/lib/workflow-upgrade.ts`（状态文案与色调、行装配、可应用判定、候选应用），单测 `apps/web/tests/workflow-upgrade.test.ts` 5 项固定展示与不可变语义；浏览器验收用门禁库 `b28_chain_check` 的默认模板：摘要「扫描 11 / 已升级 9 / 部分升级 1 / 无法升级 0 / 无需升级 1 / 移除连线 3」，`DataSplit` 显示上游缺少契约输入而停在 `1.1.0`（`partial`），`Report` 显示移除 `confusion`/`features`/`roc` 三个输入与三条断边，`notChecked` 为 `radstudio/model@2`；应用后自动保存到 r4（`Report 1.2.0`、边 16→13），撤销后复原为 16 条边与 `1.0.0`，390×844 下对话框无横向溢出。
