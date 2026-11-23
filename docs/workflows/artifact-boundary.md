# 产物边界与发布顺序

## 当前接入范围

SDK ArtifactStore、BundleArtifactStore 与单体 runner 共用 artifact-reference.rules.json 规则；框架 invoke 在完成前验证输入和输出，单体 runner 使用冻结定义验证端口与物理类型。Go 节点执行器已独立核对结果文件与声明，在节点登记之前重算哈希/大小，并在恢复时核对引用文件。此校验不信任 SDK 自报的 artifactChecks。单体事件路径同样由服务端闸门复核：公告必须指向输出工作区内真实存在的普通文件，物理类型与引用路径先过共享冻结规则，声明的哈希/大小与重算字节相符才登记，落库清单写入的是服务端重算的证据；首个被拒绝或重复的公告即决定运行终态（operator 错误码 artifact.publish_failed），登记失败不再只写日志。

这层检查保证路径、字节哈希、大小和物理类型声明的一致性，不证明科研内容、数据无泄漏、模型有效性或环境可信。它不是 Python 安全沙箱：自定义代码隔离、依赖审核和真实阿里云 FC 资格仍是强制门禁。

## 引用和文件规则

引用格式：`file:<POSIX相对路径>#sha256=<64位小写十六进制>;type=<物理类型>`。路径最多 1024 UTF-8 字节，物理类型最多 128 字节，具体标识模式见共享规则。禁止空路径、绝对/驱动器路径、反斜线、冒号、控制字符、空段、.、..、尾点/尾空格及 Windows 设备保留名。

读取、登记和写入均检查路径组件与工作区边界，拒绝符号链接；要求读取或登记的文件存在且为普通文件。输入引用和产物输出按实际文件重新计算 SHA256，元数据中的大小也必须匹配。

SDK 本地 ArtifactStore 的 resolve_path 保留历史单体影像绝对路径用法，仅用于本地显式输入；BundleArtifactStore 禁止绝对路径。不能将本地兼容接口作为自定义代码的访问授权。

## 输出与透传

输出只能引用本次登记且校验通过的产物，或者与某个显式输入完全相同的透传引用。输出引用的哈希、物理类型须与登记一致；重复声明拒绝。框架只返回本次新登记的产物，不把输入登记混入输出列表。

单体 runner 使用冻结目录核对必需输出端口、未声明端口和物理类型。invoke 的 HTTP 框架当前不具备完整冻结端口声明，先执行路径/哈希/登记与透传检查；端口契约跨 Go/HTTP 的完整绑定仍待交付。

## 发布与失败

invoke 拦截算子直接发送的 artifact.created 和节点/运行生命周期事件，由框架在验证后发布完成与产物。算子可报告进度和日志，不能通过 SDK 事件接口提前声明完成。

单体 runner 按节点暂存产物元数据，所有输出验证通过后才写 manifest、发送 artifact.created 和 node.completed。验证失败清空暂存元数据，记录失败与 provenance；调试文件可能仍保留在工作区，不视为已发布产物。路径统一为 POSIX，避免 Windows 反斜线引用无法在云端解析。

主要错误码：artifact.path_invalid / artifact.ref_invalid / artifact.type_invalid 描述引用问题；operator.input_invalid / operator.output_invalid 描述输入或输出验证失败。execution-provenance.json 的 artifactChecks 仅是 SDK 自检记录，不能替代服务端重算或云环境证明。

## 检查方式

共享正反例位于 packages/operator-schema/tests/artifact-reference-cases.json；同步命令为 python scripts/sync-operator-schemas.py，CI/提交前使用 --check。回归覆盖越界读写、正确哈希的越界引用、写后篡改、伪造完成、错误端口及失败节点不发布，并保留完整科学流程和独立重放验证。

显式表格结构已接入 [内容契约](contracts/tabular-content.md)；模型拟合血缘（来源表/划分重读重哈希、列序、留出隔离、权重字节，`radstudio/model@2` 另核对逐折训练集合、折内泄漏与每折独立权重）已接入 [模型拟合血缘契约](contracts/model-lineage.md)；清单间接引用的传递闭包已接入 [输入间接引用闭包](input-reference-closure.md)（节点派发前预检、跨节点物化与分片逐片暂存均按同一闭包展开）；隔离执行下的写竞争已接入 [分片输入的写隔离](input-isolation.md)；单体事件路径的服务端发布校验与证据绑定、以及多产物登记的批量原子性已落地（见下节）。后续需补影像/掩膜几何来源、科研来源验证、完整调用回执及真实 FC 验收。


## Go 节点执行与恢复

新快照冻结 rules.artifactBoundary=1。历史缺省字段按旧规则兼容，未知规则拒绝，不重写旧摘要。所有节点声明产物和输出引用执行路径、实际文件哈希/大小及显式输入透传检查；规则 1 额外核对请求/运行/节点身份及冻结输出端口。completed 结果同时包含 error 时拒绝。

逐调用检查器覆盖每个分片和合并调用，完整节点结果还要通过最终端口校验。合并结果改回逻辑父节点时，只能使用 Go 本进程已校验的调用身份信息；该信息不从 JSON 导入。特征、训练模型和预测文件在被合并器读取时，规则 1 要求其正好登记一次且类型/哈希/大小与实际消费字节相符，不能仅因文件存在就使用。

通过校验后，一个节点的全部产物先整体复核（含同批内 (节点, 路径) 去重），再一次性写入：PostgreSQL 用单事务、内存仓储先查重复再整批落图，任何一件被拒绝都回滚为零登记，公告也在整批成功之后才发出，失败返回 operator.output_registration_failed 且节点不写完成。同批行的 created_at 由服务端逐条递增，使 ListByRun 在同一事务时间戳下仍保持声明顺序。TestPostgresArtifactBatchRollbackAndOrder 用重复主键证明整批回滚与顺序，TestNodeArtifactBatchLeavesNoOrphanWhenStorageRefuses 证明 storage 拒绝时失败节点既无登记也无 artifact.created 公告，TestArtifactBatchValidatesEveryProductBeforeWriting 证明缺失产物在写入发生之前就被发现。正常完成事件保存 artifactValidation 和 inputReferences，作为后续恢复检查的条件，而非环境或科研正确性的证明。

规则 1 的恢复同时检查：冻结输出端口、验证规则和端口标记、输出数量、与当前图计算得到的输入引用一致，以及每个恢复输出的实际哈希。显式输入透传可在该节点输入目录核验。损坏、缺失或证据不匹配返回 artifact.restored_invalid / run.restore_failed，不静默重跑历史已完成节点。未声明新规则的旧运行仍保留旧恢复行为，不补造新的验证证据；历史兼容不代表旧结果通过了新门禁。

恢复当前只覆盖直接输出引用；contentContracts=1 时也重检显式表格内容。样本清单的间接文件已在派发前预检与跨节点物化时按 [传递闭包](input-reference-closure.md) 检查存在性，但恢复阶段仍不递归重验这些间接条目；模型指向的其他权重与科研来源仍需后续递归契约检查。文件检查也不替代执行沙箱或抵御恶意代码对同一工作区的并发修改。
