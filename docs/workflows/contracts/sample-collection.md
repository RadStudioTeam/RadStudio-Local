# 样本集合契约 v1

状态：开发分支已实现 `radstudio/sample-collection@1` 的 Go/SDK 结构、身份与路径校验，共享同一组正反例。业务栈和生产未部署，不代表真实 FC 已验收。B23 把「要不要回填绑定」从猜测变成测量：当时注册表里 15 个（本地库里含 `1.0.0-fc` 变体共 29 个）`sample_collection` 端口全部未绑定，而绑定本身是一次语义变更、不能走任何注册表写路径，结论见「端口回填：为什么不能改现有算子行」。B24 按该结论完成**生产者侧**回填：六个样本生产者各登记一行 `1.1.0`，把 `samples` 输出绑上本契约，`1.0.0` 及其 `-fc` 变体保持原样；消费方的 14 处 `sample_collection` 输入仍刻意不绑定。因此「不改变任何现有运行的放行结果」这句仍然成立——只有把工作流节点选到 `1.1.0`，发布期校验才会生效。落地形态、迁移继承的坑与三处副本的漂移门禁见「端口回填落地」。

## 它证明什么、不证明什么

`samples.json` 是整条流水线的样本源头：划分计划按 `imageId` 选样本、特征表按 `imageId` 对齐行、模型血缘按 `imageId` 回溯拟合集合。这些下游契约都默认「样本身份本身是干净的」。此前这份干净只靠 `DataInput` 算子在生成时自查（空白/重复 `imageId` 抛错），下游节点与恢复路径不再复核，因此一份手工改写过的 `samples.json` 可以把同一个 `imageId` 指向两份不同影像，或者让同一个患者在一处是阳性、在另一处是阴性，而仍然被当成合法输入。

`radstudio/sample-collection@1` 把这三件事变成确定性的 `artifact.content_invalid`：每个样本都有非空、唯一的影像身份与患者身份；同一患者的标签不互相矛盾；影像与掩膜路径是可用、有界、不越界的名称。

它不证明影像本身存在或可读：`imagePath` 指向的是数据集存储位置，不是节点工作区里的产物引用，校验器不会去打开它。它也不证明影像与掩膜几何一致、体素间距匹配或标签来自正确的标注版本——这些需要先解决「挂载直读下本地没有字节」的前置条件，属于下一个内容族。

## 结构

根节点是 1—200000 项的列表，每项一个样本记录：

```json
[
  {"imageId":"i0","patientId":"p0","label":1,"imagePath":"data/0_image.png","roiPath":"data/0_roi.png","qcStatus":"accepted"},
  {"imageId":"i2","patientId":"p1","label":null,"imagePath":"data/2_image.png","metadata":{"series":"s1"}}
]
```

`imageId`、`patientId`、`imagePath` 必填且为字符串（前两者 1—255 个字符，与 `dataset_samples.image_id`/`patient_id` 的列宽同宽，理由见文末），`label` 必填但可为 `null`，取值只允许 0/1。`roiPath` 与 `qcStatus` 可选；`qcStatus` 的枚举就是数据库里的五值域 `pending/accepted/warning/blocked/excluded`（`migrations/002_m2_dataset_qc.sql`），不另造一套拼写。记录允许额外字段（如 `metadata`、`series`、自定义标注列），契约不裁剪上游本来就在携带的信息。全局预算继承注册表：64 MiB、深度 64、科学数字文本与指数范围同表格契约。

## 路径口径为什么与产物引用不同

产物引用（`file:…#sha256=…`）走 `artifact-reference.rules.json` 的严格规则：必须是可移植相对路径，禁止绝对路径、保留设备名与结尾的点或空格。`samples.json` 里的存储路径不是这种东西——它是数据集入库时记录的存储位置，本地库里 1307 条影像路径中 1304 条以 `/data/uploads/datasets/…` 开头，还有真实文件名带空格与括号（`malignant (1).png`）。把产物引用规则直接套上去，会当场把平台自己的正常数据集判为非法。

因此样本路径只拒绝「无论如何都不该出现」的形态，命中即定位到该记录的 `/imagePath` 或 `/roiPath`：

- 超过引用字节预算（按 UTF-8 字节计，与产物引用共用同一个 `maxPathBytes`，不另设常量）；
- 含反斜杠——Windows 分隔符在跨端存储里没有稳定含义；
- 含控制字符或 DEL；
- 名称段里出现 `.` 或 `..`——只拒绝越界段，不拒绝绝对路径前缀的空首段；
- `imagePath` 全空白。

`roiPath` 缺省或空串都表示「该样本没有勾画」，这是当前数据的真实形态（`RunnerSample` 无 `omitempty`，序列化为 `""`；库里也有 `roi_path` 为 NULL 的行），所以两者都合法，只有非空且自身形态不良的掩膜路径才被拒绝。校验器不读取这些路径，也不把宿主绝对路径写进错误信息。

## 身份规则

按记录顺序执行，第一个失败即返回，字段路径为该记录的 JSON Pointer（`/3/imageId`）：

1. `imageId` 去空白后不得为空，且不得与之前的样本重复。重复键会让下游「按行号合并」与「按身份合并」得出不同结果，属于必须当场拦截的错误。
2. `patientId` 去空白后不得为空。没有患者身份的样本无法参与患者级隔离，而划分与模型血缘的患者不泄漏检查全部依赖它。
3. 同一 `patientId` 的已知标签必须一致；`null`（未知）标签不参与该比对，因此「先标为未知、后来补标」不会被误判为冲突。

这三条与 `DataInput` 生成时的自查同向但不等价——B23 用真实算子实测过这条边界：`DataInputOperator` 的门禁写的是 `if not s.get("imageId") or not s.get("patientId")`，判的是假值，所以它拦得住 `NULL` 与空串（`RunnerSample.PatientID` 无 `omitempty`，库里的 `NULL` 到 JSON 就是 `""`），拦不住 `" "` 这种只有空白的身份；契约按去空白判定，因此绑定后 `patientId:" "` 会以 `/0/patientId` 被拒。同类的新增拒绝面还有影像路径为空白或缺字段、Windows 反斜杠分隔符、含 `..`/`.` 名称段，以及同一患者标签互相矛盾——它们都是 `DataInput` 从来不看、下游却据此合并的形态。

## 样本键宽度为什么统一收成 255

平台里只有一套样本键命名空间：`dataset_samples.image_id`/`patient_id`，两列都是 `varchar(255)`。特征表、预测表、划分计划与模型血缘的 `imageId`、`patientId`、`trainIds`、`testIds`、`trainedImageIds` 以及 holdout 的 `train`/`validation`/`test` 全部引用这同一套键——`operators_builtin.py` 的 `record_for()` 就是把样本的 `sample["imageId"]` 原样写进特征表行，划分与血缘再按同一批键做成员匹配。

此前这些声明写作 `maxLength: 256`，比库列宽一个字符。方向上它是「契约放行、下游失败」：一条 256 字符的键能过内容契约，却进不了样本表，也永远不可能出现在它声称引用的样本集合里。因此本批把 5 份契约里全部 12 处样本键声明收成 255，并让门禁接管后续漂移：`TestDatasetSampleConstraintsMatchMigrationAndContract` 递归遍历注册表里每一份契约，凡按上述字段名声明的字符串（含字符串数组的 `items`）都必须等于导入预检的列宽与迁移的 `VARCHAR` 宽度，缺 `maxLength` 记为 0 同样判失败；五份引用样本键的契约若一份没采到声明，门禁直接报「已空转」，防止字段改名后静默通过。

同名字段里 `featureNames` 的 256 没有动：特征名不落任何定宽列，只活在产物 JSON 里，与样本键不是同一套口径，收窄它只会拦住本来能存的长特征名。

`imagePath`/`roiPath` 也刻意保持「契约无 `maxLength`」，门禁反过来拦住给它加上限的改动，理由见文末的列宽口径条目。

## 数组根节点带来的两处缺陷

这是第一个根节点不是对象的契约族，两侧都有先前未暴露的假设：

- Go 的 `decodeValidatedContent` 在结构校验之后无条件做 `value.(map[string]any)` 类型断言。数组根会让它 **panic**，而不是返回契约错误——进程级崩溃伪装成「内容不合法」，节点失败原因会丢。现改为带 `ok` 的断言，`sample_collection` 走列表分支并原样返回解码值。
- Python 的 `validate_content_bytes` 在分派完 `split_plan`/`model` 后直接 `value["records"]`。数组根会抛 `TypeError`，越出 `ContentContractError` 契约，边界包装拿不到 `fieldPath`。现按物理类型先分派，`sample_collection` 返回 `records = len(value)` 的证据。
- `validatePortContent` 与两处血缘读取同理改为受保护断言：注册表当前不会让 `split_plan`/`model` 走到数组根，所以这两处是纵深防御，不是新增可达路径。

停用 `sample_collection` 分派后，Go 与 Python 各自有 14 项共享用例转红（有效性用例被「根节点必须是对象」拒绝，无效性用例得到错误指针），Python 侧另出现 28 处原始 `TypeError`——这确认分派本身是承重结构，而不是装饰。

## 计划要求的旧类型映射

总体方案要求给出旧物理类型到契约标识的映射。注册表新增 `legacyTypes`，把 `sample_collection → radstudio/sample-collection@1`、`feature_table → radstudio/feature-table@1`、`split_plan → radstudio/split-plan@1`、`model → radstudio/model@1` 固定为文档可见的常量。该映射只用于说明「若某端口要为旧类型声明契约，应当声明哪一个」，运行时不做隐式升级：没有显式 `contract` 的端口依旧不做内容校验，这是既有边界而不是漏洞。

## 端口回填：为什么不能改现有算子行

契约写好了却没人绑定，等于零收益；但「给 `DataInput` 输出加一行 `contract`」看着像元数据编辑，实际会同时改变注册表可写性、在途运行可派发性与旧运行的判定依据。B23 先把这三处口径测清楚，再决定回填方式，本批不碰注册表。

**端口是算子身份的一部分。** `operatorSemanticBytes`（`services/api/operator_immutable.go`）参与语义摘要的字段包含 `Inputs` 与 `Outputs`，`OperatorPort.Contract` 随端口整体进摘要。因此给已发布端口补契约，注册表写路径必然命中 `ErrOperatorImmutable`——`TestContractBackfillOnPublishedPortIsRejected` 在内存与 PostgreSQL 两种仓储上都确认了这一点。想绕过它只能直接写库（迁移 039 就是 `UPDATE operator_versions` 的先例），而直接写库会把问题挪到运行侧。

**绕过守卫之后，冻结快照会替用户拒绝新代码。** `TestContractBackfillBreaksFrozenSnapshotBinding` 同时注册 http 与 docker 两个算子、各自捕获运行快照后回读：改行之后，http 算子的 `resolveRunOperator` 报 `snapshot execution binding changed`，已经排队的运行不再能派发；docker 算子的冻结端口仍解析为无契约（该分支不比较绑定，靠的是快照本身不被覆盖）；而没有快照的旧运行（`provenance = legacy`）按当前注册表解析，当场拿到新校验器。这三种结局里最难察觉的是最后一种——工作流一个字节没改，同一份输入昨天能过、今天以 `operator.input_invalid` 失败（Go 侧错误正文带 `artifact.content_invalid: …（契约 指针）`，Python 侧把 `ContentContractError` 重新包成同一码、保留消息与 `details.fieldPath`，`node_orchestrator.go` 与 `artifact_boundary.py` 各按这条归因链呈现）。所以回填只有两条安全路径：登记新的算子版本，或在明确停摆窗口里迁移并让在途运行先跑完；两者都必须先确认下面这条追溯性拒绝的量化结论。

**追溯性拒绝在本地库实测为零。** 契约一旦绑定，历史上「曾经放行」的样本集合可能被重新判死。用本地开发库（只读）逐条按契约口径统计 `dataset_samples`：1307 条样本、4 个数据集，命中项只有 3 条 `patient_id IS NULL`；空白-only 的患者与影像身份 0 条，空白/缺失的 `imagePath` 0 条，含反斜杠、控制字符、`.`/`..` 名称段的路径 0 条，超引用字节预算 0 条，同数据集内重复 `imageId` 0 条，同一患者标签冲突 0 条，`roiPath` 形态不良 0 条。而那 3 条 `NULL` 行今天就已经失败：`NULL` 序列化成 `""`，`DataInputOperator` 的假值门禁直接抛 `Missing identity or duplicate imageId`，与契约无关。换句话说，本地数据上「绑定契约」新增的拒绝是 0 条；契约真正的新增拦截面（空白身份、Windows 分隔符、越界名称段、患者标签冲突）在这批数据里一次都没出现，它们是防御性的而不是收窄性的。生产库未测（本批不接触生产）。

**已有绑定全部是随新版本进来的，没有一处是回填。** 注册表 107 个端口里 8 个带契约：`DataSplit 1.2.0`、`HoldoutSplit 1.2.0`（都是把 `feature_table`/`split_plan` 契约带在新版本上，同算子的 1.0.0/1.1.0 保持无契约）与 `ValidateFeatureTable 1.0.0`（新增算子，输出侧声明了契约）。也就是说平台已经有了「用新版本承载契约」的成功先例，而 `sample_collection` 的回填只需沿用同一形态：给样本生产者登记新小版本并成对声明输出与消费侧契约，`DataInput`/`QualityControl` 这类被存量工作流引用的版本一律不动。

**声明缺失时两侧都不校验。** `validatePortContent` 在 `port.Contract == nil` 时直接返回放行，输入侧与输出侧对称；只在输出端口声明契约，消费方仍不复核内容——`ValidateFeatureTable 1.0.0` 的 `features` 输入正是这个形态（输出已绑定、输入未绑定）。回填时必须成对声明，否则「上游产出合规」不会转化为「下游拒绝脏输入」。

## 端口回填落地

B24 执行的是上一节给出的那条安全路径：**登记新的算子版本，不动任何已发布行**。

- 六家样本生产者 `DataInput`、`ImageFormatConversion`、`ImageFilterBank`、`QualityControl`、`ROIValidation`、`Preprocessing2D` 各新增一行 `1.1.0`，目录文件与 SDK 内嵌副本各 +317 行并由同步脚本核对一致。实现是 `@operator(<type>, version="1.1.0")` 的纯子类（`packages/python-sdk/radstudio/operators_builtin.py:715-736`、`operators_radiomics.py:232`），`tests/test_content_contract.py` 断言子类的 `run` 与父类是同一个函数对象——升级的是声明不是实现，回填因此不需要重算一遍算子。
- 只绑输出、不绑输入。上一节说「必须成对声明」，本批刻意只做了一半，代价与理由都要写明：绑生产者输出让身份保证在**发布点**生效；绑消费方输入则等于把闸门加在存量工作流仍然钉着的 `1.0.0` 生产者链上，而那些行不可变、永远不会自行声明，历史图会当场从「照常放行」变成「拒绝执行」——那是追溯性收紧，不是本契约想要的东西。完整的闭环因此仍然欠着，它需要的不是改一行 json，而是工作流引用迁移与前端算子版本选择一起排期（见「尚未覆盖」）。回填后全目录端口 107→120、带契约端口 8→14，新增 6 处全部在输出侧；14 处 `sample_collection` 输入（分布在 8 个类型）由 `TestSampleCollectionInputsStayUnbound` 逐条断言「仍未绑定」，防止后续顺手绑一半。
- 注册表侧由 `migrations/046_sample_producer_contract_ports.sql` 落库，六条 `INSERT … SELECT … FROM operator_versions src WHERE src.version = '1.0.0' ON CONFLICT (operator_type, version) DO NOTHING`。这里有一处必须继承、照抄就出错的字段：目录文件不携带 `runtime` 与 `resource_requirements`，按文件字面量生成迁移会把这两列写成空对象——`ImageFilterBank` 的 2 vCPU/2048MB 来自 `migrations/022`，写成 `{}` 等于「升级声明之后容器悄悄降配」，客户侧只表现为变慢或被限而没有任何报错。因此这两列逐行继承各自的 `1.0.0` 行，`TestDeclarationVersionInheritsRuntimeAndResources` 在内存与 PostgreSQL 两个后端都断言声明行的 runtime（`Kind`/`ImageDigest`/`Endpoint`）与资源请求逐项等于已发布行。
- 同一份声明从此抄在三处：目录文件、SDK 内嵌副本、迁移里的 jsonb 字面量。前两处早有 `scripts/sync-operator-schemas.py --check`，迁移行却没有任何写路径可以校验；而内存仓储按目录文件播种、PostgreSQL 按迁移播种，一旦漂移，两个后端给出的闸门就不一样，画板与运行各说各话。`TestDeclaredPortsMatchRegistryRow` 因此把目录定义与注册表行喂进 `sameOperatorSemantics` 逐字段比对（先清零 runtime 与资源请求，那两列由上一条单独守），并反向断言 PostgreSQL 里的 `1.0.0` 行仍然不带契约——「046 只 INSERT…SELECT、不改历史」从注释变成断言。
- 一处对客户可见的副作用：画板默认取自 `ListCatalog`（按类型取最新的 **available** 行），这六类的默认版本从此是 `1.1.0`；已保存的工作流仍钉 `1.0.0` 且照常解析，判定不变。但 `1.1.0-fc` 云函数注册行还不存在，按 `docs/production-deployment.md` 的云端优先口径「执行期自动映射到同版本云函数注册行；未登记云变体时回退本地 docker」，选到 `1.1.0` 会**静默回退 docker** 而不是报错。真实云端要用上这批声明，前置是重建算子镜像并注册 `-fc` 行。

## 验证

`packages/operator-schema/tests/content-contract-cases.json` 是唯一来源，Go 与 Python 各自加载全部 47 项共享用例，其中 24 项属于 `sample_collection`（6 正 18 反）、14 项属于 `feature_table`（4 正 10 反）。负例不仅要求被拒，还要求错误指针与案例声明的 `fieldPath` 逐字符相同。

正例刻意包含真实数据形态而非理想形态：绝对路径 + 空格 + 括号的文件名（`/data/uploads/datasets/…/malignant (1).png`）、`qcStatus:"pending"`、`roiPath:""`、以及整条记录缺省 `roiPath`/`qcStatus` 且 `label` 为 `null`。这样「放宽到能过」与「收紧到误伤」都会被用例抓住——把存储路径按产物引用规则校验时，绝对路径正例当场转红。

守卫与用例的互相咬合用变异核对，双侧各 12 次：逐条停用重复键、反斜杠、越界段、控制字符、字节预算、`imageId`/`patientId` 去空白、患者标签一致性、掩膜分支、空白影像键、空白患者键共 11 项检查，每次只有对应的 1 项（越界段为 3 项，因三例共用该分支）转红，其余保持绿色；第 12 次整体停用 `sample_collection` 分派，用于证明上一条的数组根修复确实承重。

Python 全量 409 项通过、`ruff check` 干净；Go 全量 PG/Redis 回归 `ok 18.553s`，`go build`/`go vet` 无输出。

B21 追加的三处收口另做过六次变异：导入预检两处（`RuneCountInString` 换成 `len()` 只翻转 512 字符中文路径正例；从预检域里删 `blocked` 只翻转声明该值的用例），副本一致性门禁四处（预检列宽 512→511、预检域删 `blocked`、断言字面量改 `IN (0,1)`、从列宽表删 `roiType` 键），各自只让预期的 1—2 条断言转红；删 `roiType` 键那处只翻转门禁的缺键断言——预检上限同时退化为 0，报错仍点名 `roiType`，故三条广场用例不变红，该列的预检由 51 字符负例与 rune 计数变异覆盖；另有两处失败优先证据来自「改造前写法直接被放行」——10 个越界字段与派生分支的 51 字符 `roiType` 一路透传，完整 HTTP 导入链在内存仓储下 201 成功并在库里留下数据集。复跑后 Python 全量 411 项通过/1 跳过、`ruff check` 干净，Go 全量 PG/Redis 回归 `ok 17.692s`。

B22 把样本键收成 255 的证据分三层。第一层来自门禁自身的空转：把「只读 `sample_collection` 的两条声明」升级为「递归采集注册表里五份契约的全部样本键声明」后首次运行，五份契约同时报「里没采到样本键声明，列宽对齐门禁已空转」——递归遍历没有下沉到契约顶层的 `schema` 关键字，空转守卫当场抓住，补上 `"schema"` 下钻后共采到 12 处声明。此后又用同一处做反向变异（把 `"schema"` 从下钻关键字表里删掉）：门禁逐份点名五条空转断言，而 `TestSharedContentContractCases` 全程保持绿色——这正是静态门禁存在的理由，运行期用例对宽度漂移本身是无感的。第二层是「改注册表之前先让门禁点名」：在只收紧 `sample-collection@1`、其余四份仍为 256 的状态下跑门禁，输出逐份列出 4 份契约的 10 处 `maxLength 256` 而非笼统失败；另把 Go 内嵌副本里 `fit.trainedImageIds` 单独改回 256 做定点变异，只翻转该门禁的两条断言（「契约 256 vs 导入预检 255」与「契约 256 vs 迁移 VARCHAR(255)」），同批运行的共享用例与血缘用例全绿，因为 model 契约没有 255/256 边界用例——该契约的宽度由静态对齐而非运行期用例覆盖，与 split-plan 的处理一致。第三层是覆盖表的承重证明：从 `sampleKeyColumns` 里删掉 `trainedImageIds` 一个键，`model@1` 立即报空转（它是该契约唯一的样本键），恢复即绿；同一机制的边界也要说清——`split-plan@1` 有 5 处声明，删掉其中任一个键其余 4 处仍满足「采到东西」，因此新增样本键字段时必须同步登记该表，门禁只保证契约粒度不空转。两侧对 `maxLength` 都按 Unicode 码点计数（Go `santhosh-tekuri/jsonschema/v6`、Python `jsonschema`；后者 `else` 分支「参数不符合声明约束」证明该关键字未被忽略），因此 255 个中文字符的键在两引擎与导入预检的 `utf8.RuneCountInString` 下判定一致。复跑后 Python 全量 417 项通过/1 跳过、`ruff check` 干净，Go 全量 PG/Redis 回归 `ok 17.981s`，`go build`/`go vet` 无输出，`sync-operator-schemas.py --check` 报「9 operator contracts and 18 copies checked」。

B23 只做「要不要回填、怎么回填」的判定，注册表一个字节没改，因此证据分两组。回填的不可写性由 Go 侧三项测试承担：`TestContractBackfillOnPublishedPortIsRejected`（内存与 PostgreSQL 双仓储都返回 `ErrOperatorImmutable`）与 `TestContractBackfillBreaksFrozenSnapshotBinding`（http 运行报 `snapshot execution binding changed`、docker 运行保住冻结端口、无快照运行当场拿到新校验器）。非空转证明没有改动产品代码：把 `services/api` 整目录复制到仓库外，只在副本里从 `operatorSemanticBytes` 摘掉 `Inputs/Outputs`，两条拒绝用例同时转红并报「port contract compared as editable metadata」，冻结快照用例报「http run accepted a rewritten registry row: <nil>」，副本用完即删。绑定后的真实行为由 Python 侧承担，且走的是未打桩的真算子：`test_bound_sample_contract_publishes_real_datainput_output` 用真实 `DataInput@1.0.0` 目录定义（测试内深拷贝后注入契约）跑通两样本，产出摘要为 `{"rules":"artifact-content/1","records":2}` 并原字节回验通过；6 个不良形态（空白影像路径、缺字段、反斜杠、`..` 段、空白患者身份、同患者标签冲突）逐一以 `operator.output_invalid` 拦在发布之前，断言 `details.fieldPath` 逐字符相同、无 `artifact.created`/`node.completed`、不写 `manifest.json`——随后每个形态都在关闭 `contentContracts` 的同目录重跑并完成，「是绑定改变了判定」由同一条测试自证。另两项把边界钉在事实而非期望上：`TestSampleCollectionContractAcceptsProducerDocument` 用 `RunnerSample` 真实序列化出的文档（含绝对路径、空格与括号文件名、`labelValue` 经 `strconv.Itoa` 后的字符串）作为正例，避免契约只认测试自己拼的理想形状；`test_datainput_identity_gate_only_rejects_empty_patient` 固化 `DataInput` 现有门禁只看假值这一事实，规则开关两侧都以 `node.failed` 结束，从而把「空白身份」归到契约新增而非既有保证。本批 Python `tests/test_content_contract.py` 101 项通过（B23 新增 8 项），`ruff check` 干净。

B24 把判定落成回填，证据分三块。绑定之后仍然成立由真实算子实测：`ImageFilterBank` 是六家里唯一会改写样本身份的生产者，`tests/test_operators_radiomics.py` 用 `filters=["square","wavelet"]`、`includeOriginal=true` 跑通派生，得到 12 行、12 个互异 `imageId`，并把最长 id 断言为 `len("i1#wavelet-HH")`=13 字符——派生后缀最长 11 字符，本地库 1307 条样本的 `imageId` 最长 15 字符，26 相对 `varchar(255)` 有 229 字符余量，键宽不会因派生而撞墙；同一用例对已发布字节直接跑校验器，摘要逐字节等于 `{"contract":"radstudio/sample-collection@1","rules":"artifact-content/1","records":12}`。同文件另一条记录绑定新增的真实拦截面：掩膜路径带 Windows 分隔符时 `/1/roiPath` 被拒——该用例是直接校验产物字节，发布期的拦截行为由 B23 的 `DataInput` 六形态用例覆盖，此处不重复主张。迁移的真实落库效果由整条迁移链重建验证，而不是在旧库上补跑：新建库按 001→046 重放得到 11 行 `1.1.0`，`ImageFilterBank` 的 `1.0.0` 与 `1.1.0` 两行资源请求同为 `{"cpu": 2, "memoryMb": 2048}`（降配不复存在），046 重复执行幂等。第三块是播种自身的反作用：新增的 `1.1.0` 目录行推翻了 `TestOperatorRegistryStatusTransition`、`TestAdminOperatorStatusTransition`、`TestOperatorRegistryListByType` 三条既有测试，它们都隐含「每个类型只有一个版本」，而 `ListCatalog` 的语义是按类型取最新的 available 行——下线一行只会让位给同类型另一行。处理方式是把规格说得更准而不是删断言：两条目录断言改为逐版本，另起 `TestOperatorRegistryCatalogHidesDeprecatedType` 承接原本的强不变式（该类型全部版本下线后目录不再呈现该类型），`ListByType` 用例改用 `9.9.9` 并在建版本前快照、断言数量与目录指向。复跑门禁：Go 全量 PostgreSQL `ok 18.678s`、内存模式 `ok 16.607s`，`go build`/`go vet` 与 `gofmt -l`（LF 规范化副本）均无输出；Python 全量 434 通过/1 跳过，`ruff check` 干净，`ruff format --check` 仍只报 5 个历史待重排文件——本批虽有改动，新增块不在其列（`radstudio/operators_radiomics.py` 的待重排点在 `@@ -106`、`tests/test_operators_radiomics.py` 在 `@@ -107`/`@@ -171`，均早于本批新增区，故未顺手全文件重排）；`sync-operator-schemas.py --check` 通过（9 份契约/18 个副本）。

## 尚未覆盖

- 影像与掩膜的几何、像素语义、形状与间距匹配仍是下一个内容族；它需要先解决挂载直读下工作区没有本地字节、以及 `samples.json` 不携带内容摘要（`RunnerSample.ImageHash/ROIHash` 是 `json:"-"`）这两个前置问题。
- 消费方输入仍未绑定契约，闭环因此只走完一半。生产者侧已由 B24 完成（六个样本生产者的 `1.1.0` 行把 `samples` 输出绑到本契约，`1.0.0` 与 `-fc` 变体保持无契约），但 14 处 `sample_collection` 输入（分布在 `DataInput` 之外的 8 个类型：`DLCNNTraining`/`DLCNNInference`/`IBSI2DFeatureExtraction`/`ImageFilterBank`/`ImageFormatConversion`/`Preprocessing2D`/`QualityControl`/`ROIValidation`）刻意保持未绑定。给消费方加闸门会让仍钉 `1.0.0` 生产者行的存量工作流当场从放行变拒绝，而那些行永远无法自行声明（端口进语义摘要、已发布行不可写）。这一步需要的不是改一行 json，而是工作流引用迁移与前端算子版本选择配套，另批处理；在那之前「上游产出合规」仍不会转化为「下游拒绝脏输入」。
- 这批声明尚未具备真实云端可用性。注册表里没有 `1.1.0-fc` 行，云端优先配置下选到 `1.1.0` 会静默回退本地 docker；启用它需要重建算子镜像并登记云变体，属部署动作而非本契约的实现缺口。
- `qcStatus` 只核对取值域，不核对它是否与来源数据集的实际质控状态一致；`metadata` 完全不解释。
- 两处与本批无关的既有缺陷已在后续批次（B21）收口，本契约本身未因此改动：`operators_builtin.py` 的质控分支原先比较 `"failed"`（既不在数据库五值域内、也会被本契约拒绝），现改为按域内值 `blocked`/`excluded` 判定；广场导入原先把外部快照的任意 `qcStatus`、`labelValue` 与超宽文本直接透传进样本创建，现在写库前按 `dataset_samples` 的同一口径预检。
- 「库改了、契约/预检没改」的漂移已由门禁接住：`services/api/handler_plaza_actions_test.go` 的 `TestDatasetSampleConstraintsMatchMigrationAndContract` 读 `migrations/002_m2_dataset_qc.sql` 的 `dataset_samples` 表定义与 `operator_schemas/artifact-content-contracts.json` 的 `qcStatus` 枚举，逐列比对导入预检手抄的七条列宽、`label_value` 二值约束与五值域。它只保证三处副本彼此一致，不证明契约与库同时对同一字段的口径正确——这条边界仍然成立，只是原先举例的那处口径差（契约 `maxLength` 256 vs 库列宽 255）已在 B22 消除，且比对范围从 `sample_collection` 一份扩到注册表里全部引用样本键的契约，「恰好 256 字符的快照能过内容契约却被导入预检拒」这一形态不再可达。
- 列宽口径本身也是一处刻意的不一致：库列 `image_path` 为 `varchar(512)`，而本契约对路径只做 1024 字节的引用预算，因此一条 600 字符的影像路径能过契约、却在导入预检处被拒。两者守的是不同的东西（产物内容形状 vs 数据库列），未在契约侧对齐。
- 空摘要（`imageHash`/`roiHash` 为 `""`）本批核对后刻意保持「不拒绝」，不是漏检：`dataset_samples.image_hash` 虽然 `VARCHAR(64) NOT NULL`，但 PostgreSQL 的 NOT NULL 允许空串；广场侧 `plaza_files.file_hash` 是 `CHAR(64)` 且**可空**（`migrations/032_plaza_core.sql`），因此「只有元数据、没有字节」的条目本就合法存在。本地库实测 1307 条样本中 `image_hash` 空串 0 条，`roi_hash` 有 3 条为 NULL——那正是「该样本没有勾画」的形态，与 `roiPath` 允许空串同源，不是数据缺陷。同时 `RunnerSample.ImageHash/ROIHash` 是 `json:"-"`，运行阶段按路径暂存、本来就不携带摘要。若在此处强制非空，会拒掉合法的元数据-only 快照、拒掉无掩膜样本，也与运行期根本不读摘要的事实矛盾，故导入预检对这两列只校验宽度、不校验是否已计算；摘要缺失留给下一个内容族（几何/像素校验）与挂载直读前置条件一起解决。
