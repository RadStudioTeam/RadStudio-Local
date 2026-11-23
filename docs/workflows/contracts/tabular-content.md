# 表格内容契约 v1

## 当前范围

已实现 SDK 的显式特征表和预测表内容校验；新捕获的 SDK 目录冻结 rules.contentContracts=1，执行时按冻结端口声明校验输入/输出内容。旧目录缺少此规则时不自动升级。未知规则拒绝，未知内容契约不能当作验证成功。

Go 节点模式已独立编译同一注册资源，在逻辑输入派发前、逻辑输出发布前和直接输出恢复时重检内容。新运行快照冻结 contentContracts=1，派发 SDK 单体目录传递同一标记。SDK invoke/serve 现可消费 parameterContract.artifactPorts 并检查其端口和内容；Go 已按冻结规则派发并通过实际 SDK HTTP 桥接验证该扩展，Go 的独立内容检查不依赖 SDK 自检。此能力只在显式支持的内容契约上生效，不表示所有工作流或自定义算子已具有内容验证，更不构成云资格验收。

## 内置契约

注册资源为 packages/operator-schema/artifact-content-contracts.json，通过同步脚本复制到 SDK/Go。注册表 v1 是封闭内置规则，不加载远程 JSON Schema，不接受用户提交的任意 Schema 作为可执行校验器。当前支持下列内置族（划分来源规则见 [来源绑定划分计划](split-plan.md)）：

| ID / version | 物理 type | 内容 |
| --- | --- | --- |
| radstudio/feature-table / 1 | feature_table | 特征列名及逐样本数值 |
| radstudio/prediction-table / 1 | prediction_table | 预测概率、标签、评估角色与折号 |
| radstudio/split-plan / 1 | split_plan | 绑定来源的 CV/留出计划及患者隔离 |
| radstudio/model / 1 | model | 拟合血缘：来源表、划分、特征列序与权重字节，见[模型拟合血缘契约](model-lineage.md) |
| radstudio/model / 2 | model | 折内血缘：`fit.folds` 逐折声明训练集合与该折权重，在 @1 全部规则之上核对折内泄漏 |

本表的三个表格族可不限制 task/dimension；声明时只支持 binary_classification / tabular，modelFamily 须省略或为空。model@1 与 model@2 是例外：它们的端口声明三项都会与清单字段逐条核对。其他任务、维度和模型家族需要发布对应验证器，不能隐式复用表格 v1。仅凭 legacyTypes 映射或物理 type 不会自动把旧文件升级成新契约；`model` 的 `1` 与 `2` 是两份互不放宽的规则，兼容性按精确 id/version 判定，折信封在 @1 上会被拒、全集信封在 @2 上也会被拒。

## 特征表

```json
{"featureNames":["mean"],"records":[{"imageId":"case-001","patientId":"patient-001","label":1,"values":[0.2]}]}
```

featureNames 至少一项，名称非空且唯一；records 的 imageId/patientId 为 1—255 个字符的字符串，与 `dataset_samples.image_id`/`patient_id` 的列宽同宽（`featureNames` 不在此口径内，它不落定宽列，仍按声明的 256 上限）。imageId 在同一表中唯一，禁止依赖行号猜测样本匹配。每行 values 数量等于特征列数，数值或 null；label 是 0、1 或 null，true/false 不等同标签整数。多影像属于同一患者是允许的，不强制 patientId 唯一。

null 特征或标签表示缺失/未标注，不表示该表已经满足训练条件。患者归属、标签来源、样本画像、缺失值处理、训练/验证隔离和数据授权均需独立检查。

## 预测表

```json
{"evaluation":"inference","records":[{"imageId":"case-001","patientId":"patient-001","label":null,"probability":0.7,"fold":null}]}
```

probability 为 0—1 的数值；evaluation 限 out_of_fold、external、validation、inference。除 inference 外需要真实二分类标签；out_of_fold 需要非负整数 fold，其他角色 fold 必须为 null。样本键同样唯一。

OOF 标记和 fold 存在只证明字段符合此结构，不能证明预测来自真实独立折、患者从未泄漏或模型没有访问验证标签；这些来源与独立验证仍属后续契约和预检门禁。

## 解析与资源预算

每个内容文件最多 64 MiB、深度 64；最多 20 万行、10 万特征列，字符串长度及明细限制见资源文件。允许额外行/表元数据，但所有 JSON 仍受解析预算约束。重复键、非有限数字、非法 UTF-8、未配对代理字符、超深与超大内容拒绝。

numericPolicy 在同一资源中固定：单数字文本最多 256 字节，显式指数 -308—308。校验使用精确 Decimal，不采用参数的 JSONB 展开 128 位限制，因而允许 1e-200 等科学值，也不会把略高于 1 的高精度概率舍入成合法值。此规则不保证后续 NumPy/sklearn/深度学习库仍以同等精度运算，数值稳定性需另行验证。参数契约原有数字预算不变。

## 调用与诊断

```python
from radstudio.content_contract import validate_content_bytes

check = validate_content_bytes(raw_bytes, "feature_table", {
    "id": "radstudio/feature-table", "version": "1",
})
```

成功返回 artifact-content/1、契约身份与记录数，只是当前校验记录，不是环境或科研证明。错误为 artifact.content_invalid，details.fieldPath 使用 JSON Pointer 定位内容字段，details.contract 标识契约。输入/输出边界包装为 operator.input_invalid / operator.output_invalid 并保留细节；runner 的 node.failed 与 CLI run.failed 透传 details。

例如 /records/0/values 表示首行特征值数量错误；/records/1/imageId 表示重复样本键。应修正数据生成或映射节点，再重新执行；不要修改冻结目录关闭检查来宣称原结果有效。发布验证通过前，节点不发送产物和完成事件。

## 验收边界

共同结构案例与原始 JSON 数值/深度案例位于 packages/operator-schema/tests/content-contract-cases.json 和 content-json-cases.json。SDK 回归覆盖双端校验、坏内容不发布、旧目录兼容、两节点显式契约交换、数值精度和结构化错误。Go 使用共同结构与原始 JSON 数值案例独立重算同样契约；仍需继续补模型/清单递归引用、真实数据来源和 FC 验收，不能仅复用 SDK 自检回执。


## Go 执行与兼容（B13b）

内容规则 1 依赖 artifactBoundary=1；内容规则开启而产物边界关闭属于不支持的组合。缺省旧内容规则继续按 0 处理，不改历史摘要或在恢复时偷偷套用新规则。SDK 目录从已冻结运行规则派生，不能用当前默认值覆盖父运行/重试语义。

Go 严格解析保留 json.Number，在交给 JSON Schema 之前检查同一 numericPolicy，避免极大指数或超长文本触发不一致或无界数值工作。合法 1e-200、1e308 和概率精确边界与 SDK 使用同一 raw JSON 回归；参数规范化预算保持不变。

节点输入失败返回 operator.input_invalid；输出失败返回 operator.output_invalid，消息保留契约与字段位置。Go 校验错误结构 contentContractError 提供 Contract / FieldPath；当前节点事件将其格式化进消息，统一结构化诊断展示仍待后续。坏内容不登记产物、不发送 artifact.created 或 node.completed。

恢复时即使文件与保存引用的哈希一致，也会再次应用冻结的表格内容契约；不匹配报 artifact.restored_invalid / run.restore_failed，不静默重执行已完成节点。旧内容规则运行仍按旧语义恢复，不宣称原结果因此通过了新验证。

覆盖范围为逻辑端口的输入/输出及直接恢复引用。内部合并文件已有物理登记/哈希检查，本批不将其当作全部分片科研内容和来源均已验证；单体路径仍主要依赖 SDK 内容检查，服务端发布闭环、递归引用和完整调用/分片回执尚待补齐。OOF、外部验证与患者隔离的真实性仍须进一步验证。
