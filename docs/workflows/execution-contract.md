# 冻结参数定义与平台执行控制 v1

## 目的与当前范围

工作流中的 parameters 是用户表达的科研参数。分片、合并和内部并发控制放入单独 executionControls，不再为了调度向用户参数对象塞入未声明字段。当前接入 Go 工作流/worker、Python invoke/HTTP/once、特征提取及 CNN 训练/推理的分片和合并路径。

有运行快照的新派发发送 parameterContract。SDK 在物化输入前检查绑定、摘要、Schema 子集和实际参数，失败时不下载输入、不执行算子。成功结果携带 executionContract；worker 拒绝缺失、版本不支持或定义摘要不符的确认，不登记本次产物。旧 SDK 即使忽略新字段返回成功，也不能被当作确认过冻结定义的执行。

## 三份数据的定义

### parameterContract

字段为 version=1、dialect=radstudio-parameters/1、operatorType、operatorVersion、schema、digest。operatorVersion 使用实际冻结执行版本，例如 1.0.0-fc，而不是节点保留的逻辑版本。digest 为移除 digest 后的受控规范化 JSON 的 SHA-256，与 Go/Python 的共同 JSON 数值规则一致。

Schema 自身仍限 256 KiB、64 层，使用受支持参数子集；冻结信封独立限 512 KiB、96 层，不挤占 Schema 的深度/字节预算。Go 冻结时检查 Schema 可编译，复制和持久化恢复均保留精确数值。参数实际值另按自身 256 KiB/64 层规则校验。

### executionControls

版本为 1，当前字段范围由唯一来源 `packages/operator-schema/execution-contract.schema.json` 定义：workers 1–128、checkpointEvery 1–1000000、progressSeconds 1–3600、shardIndex 0–1023、shardCount/mergeParts 1–1024、shardOffset 0–2147483647、foldIndices 为 1–1024 个不重复的 0–1023 整数。未知字段和版本拒绝。

当前控制仅由 IBSI2DFeatureExtraction、DLCNNTraining、DLCNNInference 适配使用。shardIndex 必须小于 shardCount；合并不能同时指定分片索引、偏移或折列表；折列表仅用于训练，样本偏移仅用于推理；特征进程控制不能用于 CNN。后续自定义算子的能力声明与批准调度策略仍待交付，不向任意算子开放这些控制字段。

用户参数不能写这三类内置算子的 shardIndex、shardCount、mergeParts、shardOffset、foldIndices。API/worker 返回 parameter.execution_control 和字段位置，提示移除字段并使用平台调度设置。自定义类型不因同名业务参数被全局禁止。

SDK 算子通过 `execution_parameters(ctx)` 读取有效控制，不修改 `ctx.parameters`。没有分离信封的历史直接调用保留旧读取方式；这不赋予旧调用新的资格。新平台的分片与合并请求始终把控制和逻辑参数分开，逐次校验 SDK 的确认。

### executionContract

本版回执包含 version=1 和 parameterDigest；后者必须与本次冻结参数定义的 digest 相同。仅发送分离控制、没有冻结定义的历史请求，其 parameterDigest 为空字符串。旧无新信封请求不要求这份回执。

这是一份“SDK 确认参数定义及理解本层控制信封”的自报，不是完整调用凭证：它尚未摘要绑定请求 ID、实际参数值、控制值、输入产物或镜像/函数版本。不得拿它充当代码签名、部署可信或真实 FC 验收证明。完整调用绑定、分片级回执归档与冻结执行规则列为后续硬任务。

## 输入精度与错误

HTTP、requestRef 和 --once 请求使用保持 Decimal 的严格 UTF-8 JSON 解码；整数保留 Python int，小数保持 Decimal，再进入有界参数校验。重复键、非有限数字、未配对代理项、尾随 JSON 和超过传输预算的请求拒绝。传输预算沿用 512 MiB 的本地保护值、96 层；这不等于 FC 请求额度，云传输与资格规则仍须按真实部署验收。

算子作者会在精确输入中看到 Decimal，可在自己的数值算法边界明确转换为浮点数。SDK JSON 输出及异步终态支持 Decimal；不把参数的 128 位展开预算错误套到科研输出上。普通原生 JSON 输出沿用标准编码快路径和格式；1e-200 等有限输出不会因参数预算被拒绝。这不保证科学库用无限精度计算。

错误码：operator.contract_invalid 表示信封、绑定、摘要或控制无效；operator.parameters_invalid 表示参数不符合冻结定义（details.issues 包含字段位置）；operator.contract_unconfirmed 表示执行端未确认或确认不符。版本不匹配继续使用 B7 的 operator.implementation_mismatch。用户应修正定位的科研参数；平台契约/镜像不一致应更新 API/worker 与 SDK，不用重复重试掩盖。

## 续跑与复现记录

node.completed 保存 executionContext：版本、冻结定义、平台控制和回执。复现请求记录保留逻辑版本、执行版本、逻辑参数、控制和确认。续跑比较冻结定义的完整规范化内容及摘要，不能只相信旧记录声称的 digest。存在但为空/错误的上下文拒绝；冻结运行的已有上下文不能删除参数定义来绕过检查。

新尝试、失败、取消、最新损坏的完成记录和产物缺失会清理旧上下文。没有上下文的历史完成事件仍按历史兼容路径处理，不自动补造证据。完整冻结规则、实际参数/控制/输入绑定和旧历史资格分类仍需后续任务，不能把历史兼容等同于完整可复现。

## 开发与验证

唯一来源是 `packages/operator-schema/execution-contract.schema.json`；`python scripts/sync-operator-schemas.py --check` 验证目前五份契约的十份 Go/SDK 副本。SDK 新增 execution_contract.py；不用额外依赖，沿用批准环境已有 JSON Schema 库。

回归覆盖无效参数在执行/下载前失败、冻结摘要与实际版本绑定、控制不混入逻辑参数、未确认结果不登记产物、续跑定义篡改、精确小数真实 HTTP 往返、异步 Decimal 终态、原生输出格式和深度边界。测试使用合成数据及本地服务，不修改模型配置、不部署云资源。

API/worker 和 SDK 镜像应一起交付，无数据库迁移。旧镜像会对需要确认的新请求报未确认；回滚应保持契约版本一致，并保留历史未核验标记。自定义算子、批准依赖、代码隔离、真实阿里云 FC 执行/权限/隔离与使用、开发、契约、部署运维完整文档仍是首次开放门禁。

最终验证：当前分支 Go 全量独立 PG/Redis 与实际 SDK HTTP 桥接 15.008s、vet 通过；Python SDK 全量 209 项（148.55s）通过；Ruff、变更文件格式和五份契约/十份副本检查通过。未执行真实 FC，也未推送或部署。


## SDK 冻结端口执行扩展（B14a）

parameterContract 可附带 artifactPorts，字段为 version=1、contentContracts=1、inputs、outputs；端口结构与公开定义一致，支持 name/type/required/contract。字段一旦存在，参与同一个 parameterDigest；修改端口、必需性或内容契约都会改变摘要。SDK 冻结函数复制 Schema 和端口，不共享调用者的可变定义。

```python
from radstudio.execution_contract import freeze_parameter_contract

contract = freeze_parameter_contract("MyFeatures", "1.0.0", {"type": "object"},
    artifact_ports={
        "version": 1, "contentContracts": 1, "inputs": [],
        "outputs": [{"name": "features", "type": "feature_table", "required": True,
            "contract": {"id": "radstudio/feature-table", "version": "1"}}],
    })
```

这是本地 SDK 开发示例，不代表平台已经开放私有包发布或云执行。Go 新运行已生成此扩展，SDK invoke/serve 共用执行函数和 --once 验证它；本地实际 HTTP 桥接覆盖有效内容、无效内容和精确参数阈值。没有 artifactPorts 的既有请求保留旧规则。

SDK 在输入物化前检查摘要、类型/版本、声明、重复端口、物理类型标识和受支持内容契约；未知/不支持的声明返回 operator.contract_invalid。输入执行前校验、输出发布前校验沿用 operator.input_invalid / operator.output_invalid。失败不能发布产物或节点完成。物理类型声明的错误位置如 /artifactPorts/outputs/0/type，内容声明如 /artifactPorts/inputs/0/contract；内容字段定位继续使用 JSON Pointer。

null 端口列表仅表示旧定义缺声明，不构成 typed ports 已验证；新自定义发布的完整声明门禁尚待实现。非法假值 Schema（False、空列表、0、空字符串）不再被辅助函数擦成 {}，会在严格验证阶段拒绝；None 保留历史空对象约定。

新 SDK 目录冻结 artifactExecution=1，并依赖 contentContracts=1；旧目录不自动补标记。runner 为实际执行版本构造端口信封，不把逻辑 FC 标签误当本地实现版本。健康端点的 artifactExecutionVersion=1 仅说明 SDK 声称支持此扩展，不证明镜像身份、隔离或真实 FC 资格。

合并调用仅允许三个已有内置模式派生输入：特征提取使用 partN，CNN 训练使用原输入及 partNModel/partNPred，CNN 推理使用 partNPred。类型/语义继承冻结输出，必需性为 true；不得改写原定义。派生名称与原冻结输入冲突时拒绝，不能通过字典覆盖丢失原约束。其他类型没有隐式合并规则；新增布局需发布明确协议。

输入解码仍保留 Decimal；本批没有复制旧分支把 ctx.parameters 全局 json.loads 转为浮点数的做法。算子中的第三方科学库转换继续由算法边界显式处理。HTTP、requestRef、--once 和结果输出保持现有严格运行传输编码，不给科学输出套参数预算。

此扩展只让回执确认更多执行定义，仍未摘要绑定请求 ID、实际参数值、控制值、输入引用、分片身份或代码/环境。不得称为完整调用证明、不可伪造签名或 FC 验收。批准环境、分片回执归档、真实云资格仍须后续完成。


## Go 派发、恢复与诊断（B14b）

新运行冻结 artifactExecution=1；它必须依赖 contentContracts=1，后者必须依赖 artifactBoundary=1。读取旧快照时缺省值保持 0，不补写规则或改变旧摘要；未知版本和违反依赖的组合拒绝。运行目录继承保存的规则，而非当前进程默认值。共享快照 Schema 与 Go 同步执行依赖约束，回归覆盖 64 种组合。

节点派发使用冻结的实际执行版本构造 parameterContract，并将输入/输出端口整体纳入摘要；冻结时检查端口名称、重复、物理类型、语义声明及共享信封 Schema。实际 HTTP SDK 接收同一个信封，在输入处理前验证声明、执行前验证输入、发布前验证输出。

恢复使用同一规则重新构造预期定义，比较完整规范化内容及摘要。新规则缺少执行上下文不能复用；旧规则保持原历史兼容路径。此检查不把旧回执升级为新资格，也不替代产物字节和内容重验。

SDK 失败信息中的 fieldPath 和 contract 经白名单及字节上限后写入 node.failed.errorDetails；参数错误可以从首个 issues[].fieldPath 提取位置。任意算子载荷不进入诊断。实际本地 HTTP 测试覆盖有效表格、坏表格和精确小数阈值，并验证字段路径。它证明本地 Go—SDK 协议闭环，不构成真实 FC、隔离或完整调用证明。


## 请求值绑定与 v2 回执（B15a）

B15a 引入协议与执行边界扩展；B15b 将其接入新冻结运行的派发和恢复，规则如下。既有 parameterContract 确认定义；新可选 invocationContract={version:1,digest:<sha256>} 确认下面的请求内容。SDK 直接调用、serve 和 --once 使用同一校验；Go 校验成功回执时独立重算定义正文及请求摘要，不只比较执行端回显或定义声称的 digest。

摘要规范化体固定含以下字段，不省略：

| 字段 | 来源与规则 |
| --- | --- |
| version | 固定 1 |
| requestId / runId / nodeId | 当前调用身份，非空字符串 |
| operatorType / operatorVersion | 当前精确执行选择 |
| parameterDigest | 冻结定义的 digest，定义本身另按既有规则校验 |
| parameters | 实际逻辑参数对象，保持精确数字 |
| inputs | 实际输入端口到引用字符串的映射 |
| executionControls | 实际平台控制；缺失或空对象规范化为 null |
| environment | 声明的环境对象；缺失或空对象规范化为 null |

体使用现有精确规范化 JSON：键排序、UTF-8、不转义 HTML/Unicode、数字精确归一化，SHA-256 取小写十六进制。体上限 32 MiB、深度 96；parameters 另独立保持 256 KiB/深度 64 预算，冻结定义保持 512 KiB/深度 96。数字仍受参数契约文本/指数/展开预算约束。该分离避免把大数据集环境元数据误当成超大逻辑参数。Go/Python 共享测试向量覆盖空对象、精确小数、指数、负零、中文/emoji/行分隔符、控制和环境。普通调用的 parameters 和 inputs 必须是对象；新绑定需要精确版本及完整冻结定义。显式 null 的 invocationContract 拒绝，缺省字段才表示旧调用。

```python
from radstudio.operator import OperatorRequest
from radstudio.execution_contract import freeze_invocation_contract

# request 已包含 parameterContract、精确版本、实际参数/控制和输入引用。
request = OperatorRequest.from_dict(payload)
request.invocationContract = freeze_invocation_contract(request)
```

有绑定时，成功回执必须为 {version:2,parameterDigest,invocationDigest}；无绑定的已有契约请求保持 v1 回执，v1 不携 invocationDigest。篡改调用身份、参数、控制、输入引用或环境声明后，SDK 在算子启动/输入下载前拒绝；Go 拒绝与实际请求不匹配的 v2 回执，也拒绝拿旧 v1 回执确认绑定请求。失败结果不构成成功执行证据。

transfer 不参与此摘要：预签名 URL 续期、传输模式及临时传输凭据不改变逻辑调用身份。该排除要求文件字节、递归清单、对象来源和挂载路径另有内容校验，不能以引用摘要替代文件验证。environment 只绑定调用方声明，不证明实际镜像/依赖/函数版本。此协议不提供签名、认证、代码隔离或 FC 云资格。

普通/分片/合并派发及节点恢复接入见 B15b。模型/清单内容来源、完整分片科研语义与批准环境身份仍需要独立证据，不能仅用调用摘要宣称完成。


## 冻结启用、最终派发与节点恢复（B15b）

新 Go 运行快照保存 invocationExecution=1，它依赖 artifactExecution=1 → contentContracts=1 → artifactBoundary=1。共享快照及目录 Schema 同步约束；256 种规则版本组合验证 Go 的支持范围。旧快照缺省不自动补标记、不改变摘要；旧完成事件按其原规则使用 v1 执行上下文。SDK 单体 runner 只在收到冻结目录标记时生成绑定，既有安装目录和历史请求不被静默升级。

最终绑定由执行包装器在调度修改完成后生成。普通调用、特征样本分片、CNN 按折训练、按样本推理及各自合并调用分别拥有真实 requestId/nodeId、控制、输入引用与摘要。不能把父节点请求摘要复用到派生请求，也不能用合并调用的成功回执假称父请求曾直接执行。包装器保留一份独立预期请求，在核验回执、执行身份及产物元数据时使用，防止执行器改写传入对象后让检查基准一起改变。

执行深复制保留原生整数、json.Number 和容器类型；执行中的挂载说明与临时传输信息继续存在。只有持久化证据剔除 transfer 和 MountInputs，不能因为归档脱敏破坏实际云传输。逻辑环境仍是摘要的一部分；移除传输信息不等同于对所有用户参数或环境字段做通用隐私清理。

每个节点使用并发安全的内部记录器。只有通过回执和产物校验的成功调用才进入证据；执行端不能通过结果 JSON 自行注入记录。每节点最多 65 条逻辑调用（最多 64 个平台分片及一个合并），记录合计不超过 64 MiB；基础请求另受 32 MiB 调用体预算。没有记录器时，在派发之前拒绝。证据保存请求快照、回执、报告的实现身份、输出引用及已校验产物元数据。

节点完成事件的 executionContext.version=2 保存基础请求 invocationRequest 及 invocations。全部调用证据在任何该节点产物登记、artifact.created 和 node.completed 之前校验。复现记录同步保留实际调用，不把参数值或平台控制混写。Report 自身压缩包可含自己的计划请求及已完成前序调用；其最终回执和自身输出哈希在完成事件中保存，不能把尚未生成的自身最终证据循环嵌入包内。

恢复依次检查已有产物字节/内容、完整冻结定义、节点基础请求的摘要及其与当前逻辑参数、图输入和运行 ID 的一致性，再检查每个真实调用。普通节点要求唯一实际调用与基础请求一致；分片要求连续索引、正确总数、唯一调用身份和唯一合并调用，公共控制须继承基础请求，派生控制只允许对应内置协议字段。顶层回执须对应实际普通或合并调用。缺失记录、旧回执、跨运行证据和损坏内容拒绝复用，不自动重派已完成节点；run.restore_failed 保留错误定位及 operator.contract_unconfirmed。

本地验证覆盖：普通节点执行 → 事件持久化 → 恢复不重派；自洽但属于其他运行的证据拒绝；三类真实 Go 分片/合并编排均逐调用确认；改写分片 workers 并重新计算自身摘要仍拒绝；执行参数不可反向改变校验基准；归档不包含临时传输信息而实际挂载继续保留。SDK 单体的冻结规则产出 v2 回执；实际 HTTP 桥接仍覆盖有效内容、坏内容及精确参数阈值。

边界仍保留：当前记录对应逻辑调用，不是每次底层 HTTP 重试的云端账单证据；完整 FC 请求/函数/环境身份和云资格尚未验收。分片索引/数量及公共控制检查不等于独立复算样本切片、fold 来源和所有间接模型/清单字节；单体服务端独立发布验证仍未完成。输出元数据由 Go 单独验证，调用摘要本身不摘要绑定输出或提供签名。全部 A—F、真实阿里云 FC 隔离/权限/执行与完整文档首次开放门禁保持。
