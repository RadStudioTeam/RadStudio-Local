# 特征表后置质量评估

特征质量无法在运行前预检：提取节点尚未执行，特征表还不存在。因此平台改为**产出后评估**——节点完成、产物登记之后立刻统计一次质量指标，作为运行事件记录下来，供运行详情展示与后续排查使用。

它回答的是「这次产出的特征表好不好用」，与数据预检（回答「能不能跑」）互补，**不阻断节点、不改运行终态、不扣额外积分**。

## 目标与边界

- 只在节点执行成功（`result.Status == completed`）且产物已登记后触发，失败或取消的节点不评估。
- 只评估**本节点自己产出**、物理输出类型为 `feature_table` 的端口。按物理类型而非契约类型选择，是为了覆盖尚未登记内容契约的提取节点（如 `IBSI2DFeatureExtraction`）。
- 显式透传的上游特征表由生产者负责，本节点跳过同一产物引用，避免同一份表被重复记录。
- 只读产物文件、只写事件；没有数据库迁移，也不新增结果表。
- 恢复（resume）的节点沿用既有事件，不重新评估，保证历史结论稳定。

## 评估口径

### 读取

1. 解析产物引用，要求物理类型为 `feature_table`，路径经 `safeArtifactFile` 约束在节点输出目录内。
2. 用 `io.LimitReader` 读取，上限为内容契约注册表的 `MaxBytes`（多读 1 字节用于识别超限）。
3. 复用内容契约的有界 JSON 解码（`MaxBytes` + `MaxDepth`），不另开一套解析。

任一步失败都记为 `unavailable` 并附 `feature_quality_unreadable`，**不抛错、不影响节点完成**。

### 统计

| 指标 | 口径 |
| --- | --- |
| `recordCount` | `records` 条数 |
| `featureCount` | `featureNames` 条数（必须非空、全为非空字符串） |
| `patientCount` | 非空 `patientId` 的去重计数 |
| `missingValues` | 值为 `null` 的单元格个数 |
| `missingFeatureCount` | 至少出现一个 `null` 的特征数 |
| `constantFeatures` | **至少出现 2 个非空值、且这些非空值全部相等**的特征数 |

- 每条记录的 `values` 长度必须等于 `featureNames` 长度，否则整份报告记 `unavailable`。
- 单行或只有一个非空值的特征不判常量——没有比较对象，判常量只会制造噪声。
- `missingFeatureNames` / `constantFeatureNames` 各最多保留 5 个名称；报告里不出现样本 ID、患者标识或真实路径。

### 多端口聚合

一个节点可能产出多个 `feature_table` 端口（如训练集与验证集特征表）：

- `outputs` 逐端口保留 `port / status / recordCount / featureCount / missingValues / constantFeatures`；
- 行级计数（记录数、患者数、缺失值）求和；特征级计数（特征数、缺失特征数、常量特征）取最大值；
- 特征名取并集并同样截断到 5 个；问题逐条保留并带上 `port`；
- 总体状态取最坏：`unavailable > degraded > warning > clean`。

## 状态与问题码

| status | 含义 |
| --- | --- |
| `clean` | 无缺失值、无常量特征 |
| `warning` | 存在缺失值或常量特征，但特征表整体可用 |
| `degraded` | 没有记录（`recordCount = 0`）或全部特征列均为空 |
| `unavailable` | 产物读不出来、不是受支持的有界 JSON，或结构不合法 |

| code | 触发 | 级别 |
| --- | --- | --- |
| `feature_rows_empty` | 特征表没有记录 | degraded |
| `feature_values_all_missing` | 全部特征列均为空 | degraded |
| `feature_values_missing` | 存在缺失值，说明缺失数量与涉及特征数 | warning |
| `feature_values_constant` | 存在常量特征 | warning |
| `feature_quality_unreadable` | 读取或解析失败 | unavailable |

`degraded` 优先于 `warning`：一张没有可用数值的表不应因为同时存在缺失值而被降级描述。

## 事件与派生

```json
{"type": "node.quality", "payload": {"nodeId": "feature_extraction", "quality": {"version": 1, "status": "warning", "recordCount": 120, "featureCount": 107, "patientCount": 120, "missingValues": 8, "missingFeatureCount": 2, "constantFeatures": 3, "missingFeatureNames": ["…"], "constantFeatureNames": ["…"], "issues": [{"code": "feature_values_missing", "message": "缺失值 8 个，涉及 2 个特征"}]}}}
```

- 顺序固定：`artifact.created` 全部发出之后、`node.completed` 之前发 `node.quality`，因此前端读到完成态时质量结论已经就位。
- 没有 `feature_table` 输出的节点不发事件；`quality` 在运行节点状态里是可选字段（`omitempty`），旧客户端忽略即可。
- `deriveRunNodes` 把事件投影到 `RunNodeState.Quality`，运行详情 API 的 `/runs/{id}/nodes` 自动带上，无需新增路由。

## Web 展示

运行详情页「节点状态」面板：

- 每个节点徽标后追加一个质量徽标：`特征质量良好` / `告警` / `不足` / `未评估`；未知状态回落到 `特征质量 <status>`，色调回落 neutral，不把未知当通过。
- 徽标外层带 `title`，悬浮即可看到统计（样本·特征、患者、缺失值涉及特征数、常量特征）。
- 非 `clean` 的节点在面板底部「特征表后置评估」列表逐条展示：节点名、服务端 message（去重）、统计摘要、最多 5 个缺失/常量特征名；`clean` 不占版面。
- 没有 `quality` 的节点不显示质量徽标，避免把「未产出特征表」画成「质量良好」。
- 面板有评估详情时最大高度放宽到 264px（默认 144px），更长的列表仍在面板内滚动；移动端不限制高度，避免详情被裁切。

## 验收

- Go：`feature_quality_test.go` 覆盖干净表、缺失值/常量特征分级、全空与空表、结构不合法与超限、多端口聚合最坏状态、透传输出跳过、事件顺序与派生投影、敏感标识不外泄；`go build ./...`、`go vet ./...`、定向测试与全量 `go test ./... -count=1` 在内存与 PG/Redis 两套存储下均通过。
- Web：`tests/workflow-quality.test.ts` 5 组（状态与色调含未知回落、`hasFeatureQuality`、统计描述与空表、message 去重与 fallback、notes 过滤与名称回退）；全量 `node --test "tests/*.test.ts"` 108 项、`tsc --noEmit`、`eslint .` 通过。
- 浏览器（真实登录 + 路由 mock 的合成运行）：16 项断言全绿——`warning` 节点出「特征质量告警」徽标与统计详情、`clean` 节点只有徽标、无 `quality` 的节点不显示徽标、详情含缺失/常量特征名、桌面 1440×900 与 390×844 均完整可见且无横向溢出、控制台无脚本错误。

## 尚未覆盖

- 只统计缺失值、常量特征与患者去重，不做分布/相关性/方差膨胀/离群值/训练验证泄漏检测。
- 不读影像字节、不回链 ROI，特征质量与分割或影像质量之间没有关联分析。
- 只对物理类型 `feature_table` 的输出生效；其他容器格式（如 CSV、Parquet）需要先在内容契约里登记类型。
- 常量判定用精确相等比较，浮点误差敏感。
- 阈值与口径固定，没有后台配置、不做质量门禁、不发告警通知、不阻断节点、不影响计费。
- 读取上限跟随内容契约，超限直接 `unavailable`，不降级抽样；没有增量或分块评估。
- 事件不落独立表，随运行事件保留期清理；恢复的节点不重算。
- Web 只在运行详情展示，没有历史对比、没有导出，也没有独立的特征质量页。
