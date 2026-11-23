# 端口语义契约 v1

状态：声明兼容检查已接入 Go 注册/图校验、Web 连线与 SDK 冻结图验证；运行快照保存公开端口定义。产物路径和直接字节校验已分批接入，显式表格、来源绑定划分、模型拟合血缘与样本集合身份已接入内容校验；所有内置算子契约回填及完整科研来源仍未完成，不可将声明检查称为完整语义预检。

## 定义

端口保留 name/type/required，增加可选 contract：

```json
{
  "name": "model",
  "type": "model",
  "required": true,
  "contract": {
    "id": "radstudio/model",
    "version": "1",
    "task": "binary_classification",
    "dimension": "tabular",
    "modelFamily": "tabular_pipeline"
  }
}
```

- id：语义契约标识，平台使用 radstudio 命名空间；自定义命名空间权限在后续注册中心实现。
- version：精确数据契约版本，不是算子版本；当前不支持隐式范围匹配。
- task：任务标识，例如 binary_classification。
- dimension：2d、3d 或 tabular。
- modelFamily：模型格式/家族，例如 tabular_pipeline、cnn_checkpoint。
- 后三项输入未约束时不限制该项；输入约束而上游缺失时拒绝。模型（model）是首个把这三项也做内容核对的族：清单必须自带同名字段并逐项相等，见[模型拟合血缘契约](model-lineage.md)。

## 兼容规则

先检查 legacy type；输入没有 contract 时保持既有类型语义。输入声明 contract 时，输出必须提供相同 id/version，且满足输入声明的 task/dimension/modelFamily。未证明兼容不放行。转换必须通过显式适配节点。

这允许旧工作流继续使用旧端口，但不宣称旧产物已经经过新验证。新算子应完整声明所需契约；后续发布校验将结合任务要求检查声明完整性。

服务端返回 edge.type、节点和边定位及中文原因；前端连线即时应用同一规则。图通过仍不能证明实际文件、患者集合或训练用途正确。

## 开发与验证

Go：portContractError / validatePortContract，注册入口校验字段，ValidateWorkflow 使用数据库定义检查边。数据库 ports JSON 保留扩展字段，无独立列迁移。

Web：workflow-graph.ts 的 portContractError，精确版本定义携带契约。

回归：任务/版本/维度/模型家族错误、缺少输出声明、legacy消费者、注册中心到工作流校验。所有新契约与物理编码、实际元数据之间的映射需继续在 A/B 阶段完善。


## 当前声明约束与共同案例

精确 version 使用数字发布标识（例如 1、1.2、1.2.3，可带 -rc.1 和 +build.2），拒绝 latest、范围及通配符。id/version/task/modelFamily 不得包含 Unicode 空白或控制字符，单字段最多 256 个 UTF-8 字节；dimension 只接受空值、2d、3d、tabular。声明无效时注册接口返回 400 且不落库；图连接也不能把坏声明当作兼容。

Go/Web 共用 packages/operator-schema/tests 下的 port-contract-cases.json 与 port-contract-declaration-cases.json。前者覆盖身份、版本、任务、维度、模型家族、上游缺声明和旧消费者；后者覆盖发布版本、范围/别名、空白/控制字符、维度枚举及 UTF-8 字节边界。前端约束错误明确显示上游和要求值。Python SDK 现通过同一组兼容和声明案例，并在冻结目录捕获/读取及执行图校验中拒绝坏声明。该一致性只覆盖声明规则，不代表文件内容验证三端全部完成。

来源：旧分支 7915f677 的首批端口契约，结合当前分支审查修正声明漏洞，并提前复用后续分支的共同兼容案例。旧工作流定义未自动回填契约，不宣称模型、影像或预测文件因此已完成内容/来源验证。


## 当前检查点验证

14 项共享兼容案例、13 项共同声明案例在 Go/Web 均通过；实际注册 HTTP 覆盖非法声明 400 且不落库。Go 全量 PG/Redis（含专用 AI 持久化）通过 12.353s，vet 通过；Web 类型、62 单测、ESLint、正式 Webpack 构建通过。

真实浏览器拖动复现并修复了旧界面静默阻断问题：React Flow 的 isValidConnection 返回 false 后不触发 onConnect，原错误分支因此无法显示。现在 onConnectEnd 在确有目标端口且连接无效时计算原因，显示带上下游值的 role=alert 提示；拖到空白处不凭空报错。重连预判排除原边，避免把自己的输入当成重复占用。独立重连的完整交互矩阵仍待后续补测。

最终正式构建运行六项合成 API 浏览器用例：不兼容连线拒绝并解释、兼容连线创建，以及历史/缺失版本在桌面和窄屏四项回归，均通过。这不能替代真实数据、SDK、容器或 FC 执行验收。


## SDK 接入与严格 JSON 边界（B12）

SDK 的 catalog_snapshot 在读取冻结定义时验证每个端口契约；runner.validate_graph 使用冻结的输入/输出声明检查边。错误在执行目录创建、节点运行和产物发布之前返回，禁止回退到当前安装目录的声明。edge.type 带连线 ID（存在时）及上下游节点/端口；runner.catalog_invalid 的契约声明错误带 nodeId、fieldPath，定位到 operatorCatalogSnapshot.nodes.<node>.definition.inputs/outputs.<index>.contract。CLI run.failed 透传 details。

契约对象当前只支持 id、version、task、dimension、modelFamily。未知字段拒绝，避免 model_family 等拼写错误静默丢失约束；增加语义字段须同步公共版本与三端实现。contract 本身缺省或 null 表示 legacy；false、0 等非对象不表示“无约束”。对象内可选字段可省略或用空字符串表示未约束，显式 null 无效。Go 自定义 JSON 解码不再把 null 字段转成字符串零值，注册 HTTP 验证 400 且不落库。

版本仍为精确数字发布标识，256 字节限制按 UTF-8 计算，Unicode 空白/控制字符禁止。SDK 补齐此前待迁入代码缺失的精确版本、空白/控制字符及长度规则，保留已建立的 Go/Web 约束，没有覆盖为更宽松的旧规则。

当前共享案例：14 项兼容、19 项声明（含 null、非对象、未知字段）。SDK 还覆盖冻结图匹配/不匹配、非法声明、CLI 连线定位及执行前无输出副作用。完整语义/内容预检、模型家族实际格式、任务角色与训练/验证来源检查仍按总体方案后续交付。


## 显式表格内容校验

首批显式特征表/预测表契约与数值预算见 [表格内容契约](tabular-content.md)。仅声明兼容不等于内容有效；Go 节点与 SDK 已独立消费同一表格资源，其他数据类型、递归内容和来源验证仍待后续。


## 已实现的内容族

- [特征表和预测表](tabular-content.md)：结构、精确科学数值、样本键及预测角色。
- [来源绑定划分计划](split-plan.md)：CV/留出计划、来源文件哈希、样本身份和患者隔离；通过显式验证节点与旧特征输出衔接。
- [模型拟合血缘](model-lineage.md)：任务/维度/家族声明核对、来源表与划分的重读重哈希、特征列序一致、训练样本在划分全集内且有标签、留出测试集不得参与拟合、权重字节按摘要与大小复核。`2` 版把 `fit` 换成逐折数组，额外核对每折的训练/测试样本互不重叠、折权各自独立且确实来自该折的训练集合。`3` 版是影像形态（`dimension=2d`、无 `featureNames`、`family` 为骨干名）：`fit` 仍是特征表与划分计划的引用，折列表与划分计划逐折对齐，权重为单份合并 checkpoint。四个表格分类器自 `1.1.0` 起把 `model` 与 `predictions` 输出绑到相应契约（`1.2.0` 绑 `model` 到 `model / 2`）；深度学习链由 `DLCNNTraining 1.1.0` 产出 `model / 3`、`DLCNNInference 1.2.0` 消费 `model / 3`，训练行的 `split` 输入另绑 `split-plan / 1`。消费端输入侧共 3 行绑定：`ModelApply 1.1.0`、`Report 1.2.0`（`model / 2`）与 `DLCNNInference 1.2.0`（`model / 3`），均为画板默认；5 处已发布行（`ModelApply 1.0.0`、`Report 1.0.0`/`1.1.0`、`DLCNNInference 1.0.0`/`1.1.0`）继续未绑定，留作历史工作流与旧 DL 组合的通路。存量工作流靠编辑器里的「算子版本」选择器逐节点迁移。
- [样本集合](sample-collection.md)：影像与患者身份的非空唯一性、同患者标签一致、数据集存储路径的有界与越界规则；含首个数组根契约在 Go/SDK 两侧的崩溃修复。六个样本生产者自 `1.1.0` 起把 `samples` 输出绑到该契约，消费方输入仍全部未绑定。

以上均不代替影像/掩膜的几何递归来源、预检、批准环境或真实 FC 资格。
