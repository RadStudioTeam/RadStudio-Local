# 模型拟合血缘契约 v1 / v2 / v3

状态：开发分支已实现 radstudio/model@1、@2 与 @3 的 Go/SDK 实际文件与血缘校验，共享同一组正反例。业务栈和生产未部署，不代表真实 FC 已验收。已有算子定义、旧工作流与旧产物不自动回填。`model` 物理类型此前只被深度学习检查点当作产物登记；四个表格分类器（`LogisticRegression`、`RandomForestClassifier`、`SVMClassifier`、`GradientBoostingClassifier`）已各登记两行声明版本：`1.1.0` 绑 `@1`（一份全集拟合声明）、`1.2.0` 绑 `@2`（逐折各自声明训练集与权重），两者的 `predictions` 输出都绑 `radstudio/prediction-table@1`，已发布的 `1.0.0` 行保持无契约、产物字节不变。深度学习链在 B31 单独开 `@3`（影像形态）：`DLCNNTraining 1.1.0` 的 `model` 输出绑 `radstudio/model@3`、`DLCNNInference 1.2.0` 的 `model` 输入绑同一契约，`1.0.0`/`1.1.0` 两行推理与 `1.0.0` 训练行保持无契约；`model` 输入侧仍有 5 处发布行未绑定，见「影像血缘 v3 与深度学习链绑定」与「尚未覆盖」。

## 它证明什么、不证明什么

划分计划能证明「哪些样本被留在训练侧」，特征表能证明「这些行的结构与数值是安全的」，但两者都没法回答：这个模型文件真的用这批数据训练过吗？`radstudio/model@1` 把模型清单里声明的证据逐条还原成工作区里真实存在、仍命中引用摘要、且按各自契约再次校验的文件，使「换个名字声称是同一实验」「拿旧模型配新划分」「把留出测试集也塞进拟合」成为确定性的 `artifact.content_invalid`，而不是靠人工核对论文。

它不证明拟合本身正确：不重演随机算法、不重训、不核对超参与统计显著性，也不证明权重字节由本节点产生。清单里的 `family`、`preprocess` 与方法名称是声明，内容校验只保证声明与可核对的证据互相自洽。

## 清单结构

```json
{"task":"binary_classification","dimension":"tabular","family":"random_forest","featureNames":["a","b"],"preprocess":["standardize"],"fit":{"features":"file:features.json#sha256=<SHA256>;type=feature_table","split":"file:split.json#sha256=<SHA256>;type=split_plan","trainedImageIds":["i0","i1","i2"],"trainedCount":3},"weights":{"path":"weights.bin","sha256":"<SHA256>","sizeBytes":17}}
```

`task` 目前只接受 `binary_classification`，`dimension` 只接受 `tabular`，`family` 为非空自由标识（最长 128）。特征列 1—100000 项且互不重复，预处理步骤至多 64 项，拟合样本列表 1—200000 项且互不重复。权重 `sizeBytes` 上限 4 GiB，`sha256` 必须是小写十六进制 64 位。清单本身继承全局预算：64 MiB、深度 64、科学数字文本与指数范围同表格契约。多分类、生存分析与影像模型的取值需要新契约版本，不能靠放宽枚举塞进这一版。`fit` 换成逐折数组的形态是 `@2`，见「折内血缘 v2」，它不改动上面任何一项取值。`@3` 换成影像形态：`dimension=2d`、`family` 记骨干名、没有 `featureNames`，`fit` 仍指向特征表与划分计划，见「影像血缘 v3 与深度学习链绑定」。

## 端口契约约束

`model` 是首个允许携带 `task`/`dimension`/`modelFamily` 约束的内容族：表格类契约声明这三项会被判为无效声明，因为对应产物没有可比字段；模型清单里确有同名字段，所以声明了就必须对上，且比对在内容校验里逐条执行——`/task`、`/dimension`、`/family`。这消除「下游要求 3d 卷积、上游给了表格随机森林却静默放行」的空洞声明。未声明的项不限制；声明了但清单对不上，一律拒绝，不把「无从核对」当成「已经兼容」。

## 血缘核对顺序

按引用重新读取，不接受任何旁路：

1. `fit.features` 必须是可移植的工作区引用，指向 `feature_table`；文件必须存在、不越界、不是符号链接、字节仍命中引用里的 SHA-256，并通过 feature-table@1 自身校验。
2. 模型声明的特征名必须与来源特征表的 `featureNames` **逐项同序**相等。列名相同但顺序调换也算不一致，因为按位置索引特征的训练与按名单推理不是同一件事。
3. `fit.trainedCount` 必须等于 `trainedImageIds` 的实际长度；每个拟合样本都必须在来源表中存在且标签已知（不为 `null`）。伪造一个表里没有的 ID、或声称拟合了无标签样本，都归因 `/fit/trainedImageIds`。
4. `fit.split` 同样按上述规则读取并通过 split-plan@1 校验，还额外要求它的 `source` 引用与 `fit.features` **逐字符相同**。否则可以把划分指向另一份内容合法、路径不同的表，绕过「划分确实来自这个模型所用的数据」。
5. 复用既有的来源绑定检查（身份摘要、样本全集与来源表一致、同患者标签一致、患者不跨集合泄漏），再要求全部拟合样本落在划分全集内。
6. 留出方案下，`partitions.test` 的样本不得出现在拟合列表里；交叉验证方案在 `@1` 只做到全集约束——折内泄漏要到 `@2` 才可检，见下节。

来源文件缺失、越界、被换字节都定位在 `/fit/features`，划分侧对应 `/fit/split`；两者都不输出宿主绝对路径。校验会读取来源与划分文件，因此失败可能来自被引用文件自身的内容契约，位置保持该文件内的字段路径。

## 权重

`weights.path` 走与工作区边界相同的路径规则：拒绝绝对路径、`..`、符号链接与非普通文件，命中 `/weights/path`。解析后的路径不得等于模型清单自身，否则模型文件可以自称权重、把「权重已被核验」的假象自证成立。随后重读字节，SHA-256 与声明不符归 `/weights/sha256`，实际大小与 `sizeBytes` 不符归 `/weights/sizeBytes`。

摘要先于大小核对，因为换过的权重字节往往同时改变长度；若先查大小，篡改只会暴露成「大小写错了」，掩盖真正的字节替换。声明大小超过 4 GiB 枚举上限时先被结构规则挡在 `/weights/sizeBytes`，不再重复设置第二套 Go 侧常量。

## 折内血缘 v2

`@1` 的 `fit.trainedImageIds` 是各折训练集的并集，于是交叉验证模型只被要求「拟合过的样本都在全集内」：某一折把它的测试样本也算进「我训练过」这件事，在并集形态下和别的折的训练样本长得一样，检不出来。要判定就得逐折交代，所以新增 `radstudio/model@2`。

为什么必须是新版本而不是在 `@1` 上就地收紧：端口兼容性按 `id`/`version` 精确匹配，版本是判定依据的一部分。把折内规则塞进 `@1` 会让 `1.1.0` 时代已发布的工作流在下一次恢复或复核时被同一份字节判成不合规——那是追溯性地改历史结论，不是收紧新产出。`@1` 的全部规则一个字节没动，两个版本互不覆盖。

```json
{"task":"binary_classification","dimension":"tabular","family":"random_forest","featureNames":["a","b"],"preprocess":["standardize"],"fit":{"features":"file:features.json#sha256=<SHA256>;type=feature_table","split":"file:split.json#sha256=<SHA256>;type=split_plan","folds":[{"fold":0,"trainedImageIds":["i0","i1"],"trainedCount":2,"weights":{"path":"pipelines.fold0.joblib","sha256":"<SHA256>","sizeBytes":1712}}]},"weights":{"path":"pipelines.joblib","sha256":"<SHA256>","sizeBytes":8560}}
```

`fit` 的必填项从 `features`/`split`/`trainedImageIds`/`trainedCount` 换成 `features`/`split`/`folds`：折声明 2—10 项（与 `folds` 只能是 CV 划分互为约束），折号 `0`—`9`，每一折必填 `fold`/`trainedImageIds`/`trainedCount`/`weights`，该折样本列表仍是 1—200000 项且互不重复。顶层 `weights` 仍然必填并仍按 v1 的权重规则核对——集成载荷一个字没变，所以读模型的 `ModelApply`/`Report` 不跟着改版，`pipelineArtifact` 也保持同一份引用。

核对顺序（Go 与 Python 同规则、同指针）：

1. 先做 v1 的来源核对：特征表按引用重读并校验、特征名逐项同序、划分按引用重读并校验、划分的 `source` 与 `fit.features` 逐字符相同。
2. 划分必须是交叉验证：`scheme == "holdout"` 或缺 `folds` 数组都当场归因 `/fit/split`。`@1` 两种方案都收，`@2` 只收 CV——留出法没有「每折」可交代，让它走 `@1` 而不是给它一套假规则。
3. 折声明必须是对象，指针落在数组下标上（`/fit/folds/<i>`，是声明的位置不是折号，因此重复与错位的报错能指到具体那一条）。
4. 折号不重不漏：同一折号声明两次归 `/fit/folds/<i>/fold`；声明的折号不在划分方案的折集合内归 `/fit/folds/<i>/fold`；声明条数与划分方案折数不等归 `/fit/folds`。
5. 每一折 `trainedCount` 必须等于该折 `trainedImageIds` 的长度，且每个样本都在来源表中存在、标签已知（不为 `null`），归因 `/fit/folds/<i>/trainedCount`、`/fit/folds/<i>/trainedImageIds`。
6. 每一折的 `trainedImageIds` 必须与该折 `trainIds` **代表同一个集合**。按集合相等而非子集判定是刻意的：折训练集里多出的样本正是「声称没拟合却进了模型」的泄漏方向，放宽成子集就等于不检。反过来，折训练集里少的样本同样拒绝，因为那意味着该折实际拟合的字节与声明不符。顺序不承重（集合语义，重复项先折叠），所以训练集换序不算不一致。
7. 折权重各自成立：不得指向打包后的集成载荷（归 `/fit/folds/<i>/weights/path`），不同折不得共用同一份文件路径（归 `/fit/folds/<i>/weights/path`），也不得声明同一份字节（跨折摘要集合，归 `/fit/folds/<i>/weights/sha256`）；其余路径、自引用、摘要、大小规则与 v1 逐字相同。字节而非路径才算「是不是同一份权重」，只查路径会放过把同一载荷复制成两个文件名的声明。

留出泄漏在 `@2` 下不再需要单独规则：第 6 步已经把「该折训练集恰好等于划分方案的该折训练集」钉死，该折测试样本出现在该折声明里就是集合不等。

`records` 的含义随形态变化，读摘要时别混：`@1` 给的是去重后的全集拟合数 `len(fit.trainedImageIds)`，`@2` 给的是逐折 `trainedCount` 之和，同一样本按它出现的折数重复计入（均匀 5 折、每折留 1/5 做测试时约为全集样本数的 4 倍）。两边用例各自钉住自己的算法，所以这个数跨版本不可比，只用来核对「证据量非零且与声明一致」。

## 表格分类器产出端绑定落地

绑定形态与样本集合契约一致：给四个表格分类器各登记一行 `1.1.0`（目录 `packages/operator-schema/operators.json` 与 SDK 内嵌副本、`migrations/047_tabular_model_contract_ports.sql` 的四条 `INSERT … SELECT … ON CONFLICT DO NOTHING`），折内血缘再各登记一行 `1.2.0`（同一目录文件与 SDK 副本、`migrations/048_tabular_model_fold_lineage_ports.sql`），已发布的 `1.0.0` 与上一批的 `1.1.0` 行一个字节不改。048 的 `runtime`/`resource_requirements` 从 `1.0.0` 继承而不是从 `1.1.0`：迁移不能假设 047 已应用，而 022 才是容器资源请求的来源，漏写即静默降配。不回填的根据不是谨慎而是两重实测：端口随 `operatorSemanticBytes` 进语义摘要，注册表没有写路径；而 `1.0.0` 的 `model.json` 里根本没有信封，就地绑定只会让今天的产出当场以 `/task` 被判 `operator.output_invalid`——这一条由真实四节点链跑出来，不是推断。

与样本声明版相反，这一族的 `run` 必须是新函数对象：血缘信封是要新增的产出，不是同一份字节的重新贴标。为免两套拟合代码分叉，`1.0.0` 的实现与被拒实验改为共用 `train()`/`classifier_outputs()`，声明版只多传 `lineage=` 与 `declare_lineage=True`；「历史产物字节不变」因此由用例逐字节钉住，而不是靠注释承诺——同一张合成特征表分别跑 `1.0.0` 与 `1.1.0`，`predictions.json` 完全相同，`model.json` 去掉 `task/dimension/family/preprocess/fit/weights` 六个键之后与 `1.0.0` 逐字段相等。

信封里没有一项是人工填写的声明：`family` 取自算子自身的估计器类别，`preprocess` 由实际构建的 sklearn 流水线步骤名导出（表格分类器实测为 `["missing","impute","filter","scale"]`），`fit.features` 与 `fit.split` 直接取 `ctx.inputs` 里被真正消费的两个引用，`@1` 的 `trainedImageIds` 是所读划分计划各折 `trainIds` 的排序并集，`@2` 则逐折原样写出该折的 `trainIds` 并另存一份只含该折 pipeline 的载荷（`pipelines.fold<k>.joblib`，仍是单元素列表，推理端不需要区分整包与折包），路径与摘要由落盘结果回填进声明。于是「声明拟合了哪些样本」与「划分计划里确实是这些样本」在产出点就被同一段代码绑住，契约再按引用重读、重哈希、重校验，把换名字、换来源与把留出测试集塞进拟合变成确定性失败。`modelFamily` 从此是真实门槛：四行各写自己的家族，写错家族的下游声明在 `/family` 被拒。代价如实记下：`1.2.0` 每个模型节点多写 N 份折载荷。合成 5 折链实测 `clf/` 下的 joblib 从 5,691 B（只有集成载荷）变成 15,621 B（集成 5,691 B + 五份折载荷各 1,986 B），约 2.7 倍。

绑定带来一条上游版本前提，必须如实记下：`1.1.0` 与 `1.2.0` 分类器接到 `DataSplit 1.0.0` 上都会失败，且失败落在 split-plan@1 自身校验（`fieldPath` 为空串）而不是血缘比对——`1.0.0` 的划分计划不带 `source`，而血缘核对要求划分的 `source` 与模型的特征表引用逐字符相同。`ListCatalog` 取的是按类型最新的 available 行，因此加入 `1.2.0` 后分类器画板默认已从 `1.1.0` 变成 `1.2.0`（`TestTableModelVersionsResolveForRuns` 直接断言这个默认值）；把新默认用到尚未升级划分的旧图上会当场报错，这是绑定换来的收紧而非回归。已存在的 `1.1.0` 节点仍按自身钉版运行，不会因为这次升版被追溯改判。

同一份声明从此存在四处——目录文件、SDK 内嵌副本、迁移 047 的 jsonb 字面量、迁移 048 的 jsonb 字面量。前两处由 `sync-operator-schemas.py --check` 核对，两条迁移行没有任何写路径可校验，而内存仓储按目录文件播种、PostgreSQL 按迁移播种：一旦漂移，画板与运行会给出不同的闸门。048 因此不手抄——它的四条语句由目录文件里的 `1.2.0` 行生成，端口与描述逐字取自同一来源。`TestDeclaredModelPortsMatchRegistryRow` 在 PostgreSQL 子用例里把每个声明版本（`1.1.0` 与 `1.2.0`）的目录定义与注册表行喂进 `sameOperatorSemantics` 逐字段比对（先清零 `Runtime`/`Resources`，这两列由「声明版必须逐项继承 `1.0.0`」的独立门禁守），并反向断言 `1.0.0` 行至今不带契约。消费端仍然一个都没绑：`model` 输入共 5 处（`ModelApply 1.0.0`、`DLCNNInference 1.0.0`/`1.1.0`、`Report 1.0.0`/`1.1.0`）逐条断言 `contract` 为 nil，绑它们等于把闸门加在永不自行声明的已发布生产者行上，需要工作流引用迁移配套，属下一批。

## 与运行快照、恢复和调用回执的关系

内容族版本随运行快照冻结（`contentContracts=1`），因此同一运行在发布阶段与恢复阶段按同一套规则判定，不会因目录后续演进而改变结论。校验是契约驱动的：端口没有声明契约时不读任何血缘文件，旧的 `model` 产物继续按物理类型登记，不被追溯性拒绝；一旦声明，来源表被删除也会立刻失败。这一条由专门的用例双向证明，而不是假设。

SDK 侧的边界包装保持既有语义：内容错误在输入/输出核验入口转成 `operator.input_invalid` / `operator.output_invalid`，同时保留 `fieldPath`，因此节点失败原因仍指回具体声明字段。Go 的发布与恢复共用同一入口，不各留一套语义。

## 验证

`packages/operator-schema/tests/model-lineage-cases.json` 是唯一来源，Go 与 Python 各自把占位符（`@features`、`@identity`、`@split`、`@weights`、`@weightsSize`）在规范化编码之前注入，再按 `break` 名制造一处确定性破坏。41 项共享案例分两族：20 项不带 `contract` 键走 `@1`（4 正例 / 16 负例），20 项 `contract: 2` 走折内血缘（2 正例 / 18 负例），另有 1 项显式 `contract: 1` 专门证明全集契约拿到折内信封时判在 `/fit/trainedImageIds`。`@1` 族覆盖：绑定成立、任务/维度/模型家族各自的满足与冲突、列序漂移、表外样本、数量不符、留出泄漏、划分指向别名路径、来源缺失、来源哈希过期、权重换字节、权重大小错、权重超预算、权重自引用、权重越界、来源引用越界。`@2` 族在其之上覆盖：折训练集被灌成全集、折测试样本被拟合、折样本不在表内、折数量不符、折声明缺失、折号不在划分方案内、折号重复、折权重换字节、折权重跨折同字节、折权重共用路径、折权重指向集成载荷、折权重越界，外加两条形态互斥——留出划分声明 `@2` 判在 `/fit/split`，全集形态声明 `@2` 判在 `/fit/folds`。同一份字节在两个版本下判定不同，正是版本升级承重的证据。负例不仅要求被拒，还要求错误指针与案例声明的 `fieldPath` 逐字符相同，所以「另一个检查偶然先失败」不能冒充通过。折权重的字节由运行器按「同一权重字节 + 折号」派生，因此逐折摘要天然互不相同，重复摘要才是被检查出来的破坏而不是夹具噪声。

留出泄漏用例另做了变异验证：临时停用该守卫时，只有这一项转红，其余 19 项仍绿，证明守卫与用例互相咬合而非空转。折内守卫在 Go 侧 `TestSharedModelLineageCases` 上做了两次变异：整体停用 `checkFolds` 时恰好 11 个折内子用例转红、`@1` 的 20 项全绿（另外两条形态互斥判在进入折核对之前，不受影响）；只停用「折训练集与划分方案该折集合相等」这一条时恰好 2 项转红（灌成全集、折测试样本被拟合），而「折样本不在表内」仍绿——两条守卫各自承重，不是一条覆盖另一条。两次变异后都按 MD5 精确复原文件并复测全绿。

## 产出端绑定的验证

绑定后的判定由真实算子链给出，不由契约单测自证：四个分类器各自的 `1.1.0` 都跑通 `DataInput → DataSplit 1.2.0 → FeatureFilter → 分类器` 四节点链（`DataInput` 只在测试内把出口换成特征表，用于把上游端口声明为契约），对已落盘的 `model.json` 直接再校验，返回摘要的 `rules` 为 `artifact-content/1`、`records` 恰为拟合样本数，`predictions` 另按 `radstudio/prediction-table@1` 独立回验。三条负例划出行为边界：把契约回填到已发布的 `1.0.0` 行，节点停在 `node.started → node.failed`、错误归因 `/task`、`artifact.created` 只有 3 条、`manifest.json` 只登记 source/split/filter，而被拒的三份字节仍原样留在 `clf/` 工作区——注册与落盘是两回事，这里如实钉住；同一份 `1.0.0` 链关掉 `contentContracts` 后照常四节点完成，说明改判来自绑定而不是实现回归；`1.1.0` 接到不带 `source` 的 `DataSplit 1.0.0` 上被拒于 split-plan@1 自身校验。复核侧另做两处破坏：换掉 `source/features.json` 归因 `/fit/features`，换掉 `clf/pipelines.joblib` 归因 `/weights/sha256`，两者都是对已落盘模型直接再校验时触发，证明信封不是发布期的一次性检查。

折内血缘的判定同样由真实链给出：四个分类器各自的 `1.2.0` 都跑通同一条四节点链，落盘的 `model.json` 里 `fit` 的键恰好是 `features`/`folds`/`split`（没有全集那两项），折数与划分方案一致，逐折的训练集排序后等于该折 `trainIds`、路径互不相同、摘要互不相同且都不等于集成载荷，每份折载荷按磁盘字节重算 SHA-256 与 `sizeBytes` 都对得上；`pipelineArtifact` 仍是 `file:pipelines.joblib#sha256=<集成摘要>;type=model`，与集成 `weights` 指同一份字节——推理端因此不需要跟着改版。按 `@2` 再校验返回 `contract` 为 `radstudio/model@2`、`records` 为逐折 `trainedCount` 之和。三条互斥与破坏用例：把同一个折信封交给 `@1` 契约校验，拒在 `/fit/trainedImageIds`（`@1` 只认全集声明，版本升级才是承重的）；把某一折的权重字节换掉再复核，拒在 `/fit/folds/1/weights/sha256`，指针落在被改的那一折上而不是整份模型；`1.0.0`/`1.1.0`/`1.2.0` 三个版本跑同一张合成特征表，`predictions.json` 三者逐字节相同，`model.json` 去掉六个信封键后与 `1.0.0` 逐字段相等——升版只加声明，不动数值。上游前提也对两个声明版本分别验证：`1.1.0` 与 `1.2.0` 接到不带 `source` 的 `DataSplit 1.0.0` 上都拒于 split-plan@1 自身校验。

四处副本的一致性也做了非空转证明：目录文件、SDK 内嵌副本、迁移 047、迁移 048 写的是同一份声明，前两处由 `sync-operator-schemas.py --check` 核对，两条迁移没有任何写路径可校验——内存仓储按目录文件播种、PostgreSQL 按迁移播种，一旦漂移画板与运行就给出不同的闸门。047 的漂移证明在上一批取得（改一个 `modelFamily` 字面量，只有 `TestDeclaredModelPortsMatchRegistryRow` 的 PostgreSQL 子用例转红）。这一批把同样形态的篡改做在新登记的 048 上：只把 `LogisticRegression@1.2.0` 行的 `modelFamily` 改成 `logistic_regression_TAMPERED`（目录里的 048 原文件一个字节未动，md5 取证前后同为 `61343887cd2d7fc625d819e24060167e`），46 个迁移全量重放进一次性库后该行确实落库为篡改值，同一二进制在该库上只得到一条失败——`LogisticRegression@1.2.0 registry row differs from the catalog file (schema/defaults/ports/entrypoint)`，指向干净门禁库则 `ok`；`1.1.0` 与其他三个分类器都不受影响，证明比对是逐版本生效而不是整表一锅端。内存模式全量 `go test ./...` 在这期间仍 `ok 17.663s`（PG 侧同批 `ok 18.368s`），它结构上看不见这处漂移，正是闸门必须落在 PostgreSQL 子用例上的理由。取证后一次性库已 `DROP DATABASE`、篡改副本已删除。门禁库随后整链重建（001→048 全量重放 46 个迁移文件，零错误）：`1.1.0` 行 15 条、`1.2.0` 行 6 条（四个分类器 + `DataSplit`/`HoldoutSplit`）、带契约的输出端口 28 个与输入端口 2 个；四个分类器的 `1.2.0` 行 `runtime` 与 `resource_requirements` 与各自 `1.0.0` 逐项相同（SQL 断言 4/4），四类 `1.0.0` 行的带契约端口仍为 0；048 重复执行幂等。反证保留在旧库 `b25_chain_check`（只重放到 047）：在其中解析 `1.2.0` 会得到 `operator version not found`，说明这两个后端的闸门确实由迁移决定。

## 消费端绑定的测量

绑定消费方输入这一步没有直接做，而是先把「绑了会怎样」量出来（`services/api/operator_model_consumers_test.go`，只读内存定义、不写注册表；`packages/python-sdk/tests/test_content_contract.py` 接一个真实 `ModelApply` 节点）。七条 Go 用例与两条 Python 用例的结论：

- **读面兼容、声明互斥**。`ModelApply` 从 `model` 顶层只读两把键——`pipelineArtifact` 与 `featureNames`（用包住 `OperatorContext.input_json` 的记录字典逐键取证，两个版本读到的键集完全相同）。同一张合成特征表把 `1.1.0` 与 `1.2.0` 的模型分别接进同一条五节点链，两个运行都跑满 5 个 `node.completed`，`apply/predictions.json` 逐字节相同、`pipelineArtifact` 逐字符相同；同时两份 `fit` 的键分别是 `features/split/trainedCount/trainedImageIds` 与 `features/folds/split`，互不相等。所以推理端不需要改版是事实，而两种血缘信封在契约层面互斥也是事实——绑哪一版都等于替消费端淘汰另一个生产版本。
- **精确匹配使一份绑定只能锁一种形态**。生产端 `1.1.0`（`@1`）配消费端绑 `@1` 干净通过、绑 `@2` 恰好一条 `edge.type`「数据契约或版本不兼容」；`1.2.0` 反过来同样只有一条。未绑定的消费端则 `1.0.0`/`1.1.0`/`1.2.0` 三种产出全部放行——这就是今天仍然敞着的宽松面，也是绑定唯一能换来的收紧。
- **契约语法表达不了「同时接受两版」**。`OperatorPort.Contract` 是单指针（一个端口最多一份契约），`version` 又只收 `^[0-9]+(\.[0-9]+){0,2}$`：`1|2`、`1 || 2`、`latest`、`1.x`、`^2`、`>=1.0.0`、`*`、`1,2` 一律当场判无效声明，空串判「缺少 id/version」。要在消费端同时收 `@1` 与 `@2`，必须先扩这套语法（或让生产端收敛到一版），不是改一行端口声明就能落地。
- **绑已发布消费行会当场改判随包模板**。把 `Report` 的 `model` 输入绑上 `@1` 或 `@2`，`packages/workflow-schema/examples/default-workflow.json`（全链 `1.0.0`）立刻多出恰好一条 `edge.type`，落在 `report` 节点、报「上游缺少输入要求的数据契约」——因为 `1.0.0` 生产者行自身不声明契约，而校验规则是「消费方有要求、生产方为 nil」即拒。这正是产出端那批「不回填已发布行」的根据在消费端的复现：绑定不是加护栏，是给存量图改判。
- **改判只作用于新运行**。同一份 IR 先用实时目录判、再按运行快照判：`captureRunOperators` 冻结出的定义里 `1.0.0` 仍无契约，`frozenWorkflowDefinitions` 复核通过；换成绑定后的目录则实时路径（建单 `handler_run.go:90`、重试 `run_retry_validation.go:60`）拒、快照路径放行。也就是说绑定会回溯改判已保存的 `workflow_versions.IR` 在下一次新建运行时的结论，却不会使已经创建的运行失败——这既是有界爆炸半径，也是「已建运行仍可跑完不合规模型」的残留缺口。
- **深度学习链今天靠未绑定共存**。`DLCNNTraining@1.0.0` 的 `model` 输出不声明契约（`dimension` 枚举只有 `tabular`，它发不出这张信封），把消费端绑成 `tabular` 后它就再也接不进来；未绑定时 DL 与表格两条链对同一消费端口都成立。绑表格消费端会顺手切断 DL 链，这一步必须与 DL 信封的新契约版本一起排。

测量那一批没有落绑定：注册表、目录文件与迁移一个字节未改，`TestModelInputConsumersStayUnbound` 按固定的 5 行清单断言 `contract` 全为 nil。下一批把绑定交付之后，这条守卫改成 `TestModelInputConsumerRowsAreTheLedger`——同一张清单继续钉行数与逐行版本，但「绑没绑」交给下一节的 `unboundModelConsumers` / `boundModelConsumers` 两份名单分别把守。

## 消费端绑定落地（`ModelApply 1.1.0` / `Report 1.2.0`）

按上一节量出的取舍，选择绑 `@2`（折内血缘），并且只绑在新登记的行上。

- **交付形态**：迁移 `049_model_consumer_binding_ports.sql` 新增两行 available——`ModelApply 1.1.0`（端口、参数、`predictions` 产出照抄 `1.0.0`）与 `Report 1.2.0`（照抄 `1.1.0` 的论文报告端口集：没有 `features` 输入，也没有 `reproduction` 产出），两行的 `model` 输入声明 `radstudio/model@2`（`task=binary_classification`、`dimension=tabular`），同一份声明同时写进目录文件与 SDK 内嵌副本。刻意不写 `modelFamily`：外部应用与报告都该接受任一分类器，写上家族就成了「只认逻辑回归的报告」。
- **发布行一律不动**：`ModelApply 1.0.0`、`Report 1.0.0`/`1.1.0`、`DLCNNInference 1.0.0`/`1.1.0` 这 5 处继续不绑契约。这既是历史工作流的逃生通道（把节点钉回旧行就绕开闸门），也是深度学习链目前唯一的通路——`DLCNNTraining` 发不出表格信封，任何绑了 `@2` 的消费端永远接不上它。
- **绑定不带算法变更**：Python 侧 `ModelApplyOperatorV11` 与 `ReportOperatorV12` 都只是空子类加版本声明，`run` 沿用自己的基线类；`@1` 与 `@2` 把 `pipelineArtifact`、`featureNames` 放在同一位置，所以新版读的还是同一份集成载荷。客户换版本只是加一道血缘闸门，不是换计算。
- **闸门确实落在图上**（`TestBoundConsumerGateAppliesToAGraph`）：在随包模板上挂一个 `ModelApply 1.1.0` 节点，分类器钉 `1.2.0` 时整图 0 issue；分类器改钉 `1.1.0` 时恰好一条 `edge.type`「数据契约或版本不兼容」且落在该节点，同图的报告节点不受牵连——这就是「绑新行、不回填旧行」换来的可分割性。消费端改回 `1.0.0` 又整图通过，钉版本确实是退路而不是两难。
- **同批必须升生产端**（`TestBindingNeedsThePerFoldProducer`）：分类器留在 `1.0.0`（不声明契约）时，接进绑定消费端直接判「上游缺少输入要求的数据契约」，连建图都建不出来。`@2` 产出目前只有四个分类器的 `1.2.0`。
- **必填位决定闸门的覆盖面**：`ModelApply` 的 `model` 必填，闸门无条件生效；`Report 1.2.0` 的 `model` 选填、只有 `metrics` 必填，因此只连指标的报告节点不会因为 `model` 绑了契约而冒出 `input.required`（`TestPaperReportWithoutModelEdgeStillValidates`）。
- **画板默认就是绑定行**：目录接口每类型只呈现一行，所以 `ListCatalog` 给出的 `ModelApply`/`Report` 默认版本即新行，客户不显式选版本也拿到闸门；而已钉在 `1.0.0`/`1.1.0` 的节点按自身版本解析、runtime 仍为 docker，不会被悄悄升级。
- **运行规格靠继承而不是抄写**：目录文件不含 `resource_requirements`，注册表行才是容器资源请求的唯一来源，漏写即静默降配。049 因此从各算子的 `1.0.0` 行 `SELECT src.runtime, src.resource_requirements`（不假设更晚的迁移已应用），Go 侧对内存与 PostgreSQL 两个后端逐一比对 kind/image digest/endpoint 与四项资源。
- **存量工作流的迁移路径在前端**：`apps/web/src/lib/workflow-versions.ts` + `workflow-version-picker.tsx` 给选中节点提供「算子版本」下拉，只列同类型可用的更新版本（已下线的 `deprecated`/`archived` 行不作为落点，但节点当前钉着的那一行仍显示），切换前按目标版本的 schema 与端口出一份差异预览——会丢弃的参数、缺的必填、消失的端口、连带失效的连线逐条点名，确认后才改写节点与边，且走的是编辑器同一条撤销栈。

## 影像血缘 v3 与深度学习链绑定（`DLCNNTraining 1.1.0` / `DLCNNInference 1.2.0`）

`@1`/`@2` 的 `dimension` 只有 `tabular`、`featureNames` 必填、`fit` 必须是一张特征表，体数据模型换不进去；把 DL 检查点硬塞进这两版只能靠谎报维度。B31 因此新开 `@3`，并把 DL 链的两端绑在一起。

- **信封形态**：`{task:"binary_classification", dimension:"2d", family:<骨干名>, preprocess?, fit:{features:<特征表引用>, split:<划分计划引用>, folds:[{fold, trainedImageIds, trainedCount, testCount}]}, weights:{path, sha256, sizeBytes}}`，其余模型元数据（`checkpointArtifact`、`library`、指标等）继续平铺在同一份清单里。刻意没有 `featureNames`——影像模型的列语义不在特征表里，`fit.features` 只是「这次拟合用的样本全集出自哪张表」的身份证据。
- **校验口径**（Go `validateModelLineage` 的 `@3` 分支）：重读 `fit.features`（feature_table）与 `fit.split`（split_plan），要求划分计划的 `source` 引用与 `fit.features` 逐字符相等；折数 2—10，`folds` 的折号集合必须与计划一致；每折 `trainedImageIds` 与计划 `trainIds` 集合相等、`trainedCount` 与实际长度一致、`testCount` 与计划 `testIds` 长度一致；全部训练样本必须能在来源特征表中找到且标签已知。`@3` 只接受交叉验证方案，留出方案在声明层可以通过，但内容校验会明确拒绝（见「尚未覆盖」）。
- **权重按单份集成载荷核对**：不像 `@2` 那样逐折各一个文件，`@3` 的 `weights` 是合并后的 checkpoint，走同一套路径规则、摘要与大小重读，且 `weights.path` 不得等于清单自身。
- **产出端**：Python `DLCNNTrainingOperatorV11`（`@operator("DLCNNTraining", version="1.1.0")`）在完整交叉验证的合并调用里写 `@3` 信封——`fit.features` 取划分计划的 `source`，没有 `source` 直接报错而不是发一份没有出处的清单；分片调用不产出最终模型，仍按原协议只回分片产物。参数、默认值与 1.0.0 逐项相同，数值口径不变。
- **消费端**：`DLCNNInference 1.2.0` 只是 1.1.0 的声明子类，读取逻辑不变；`@3` 信封平铺了 `checkpointArtifact`，所以推理端沿用的取值路径无需改动。契约**不写 `modelFamily`**：`@3` 清单里的 `family` 是逐节点的骨干名（resnet18、densenet121……），写上家族等于把推理绑定死在某一个骨干上。
- **划分输入一并绑定**：`DLCNNTraining 1.1.0` 的 `split` 输入绑 `radstudio/split-plan@1`，与 `@3` 信封「`fit.features` 必须来自划分计划 `source`」的校验互为前提。当前只有 `DataSplit 1.2.0` 与 `HoldoutSplit 1.2.0` 声明该契约，更早的划分行不声明契约、按精确匹配接不上这行训练节点。
- **注册形态**：迁移 `050_dl_model_image_lineage_ports.sql` 新增两行 available，`runtime` 与 `resource_requirements` 从各自 `1.0.0` 行 `SELECT` 继承（目录文件不带资源列，漏写即静默降配），同一份声明同步进目录文件与 SDK 内嵌副本。`1.0.0` 训练行与 `1.0.0`/`1.1.0` 推理行一个字节不动。
- **闸门与退路**：新图默认拿到 `@3` 两端——表格分类器产出（`@1`/`@2`）接 `DLCNNInference 1.2.0` 判「数据契约或版本不兼容」，DL `1.0.0` 产出（无信封）接它判「上游缺少输入要求的数据契约」；反过来 `@3` 产出接 `ModelApply 1.1.0`/`Report 1.2.0`（`@2`）同样拒绝。钉回 `DLCNNInference 1.1.0`（未绑定）即可沿用旧组合，代价是重新失去血缘闸门。
- **验证**：Go `TestImageLineageBindsTheDeepLearningChain` 钉住两行契约、无 `modelFamily`、四组跨链拒绝与「未绑定推理行仍放行 `@3`」；`TestDLBoundRowsChangeNothingButModelLineage` 逐字段确认新行除血缘声明外与基线行一致；Python `test_packaged_catalog_binds_model_lineage_on_declared_rows` 收口目录副本；共享正反例在 `packages/operator-schema/tests/dl-model-lineage-cases.json`。

## 尚未覆盖

- `@2`/`@3` 检的是「折声明与划分计划互相自洽」，不检「该折权重确实由该折样本训练出来」：契约逐字节重算折载荷摘要、拒绝两折共用同一份字节、要求每折训练集与计划里的 `trainIds` 集合相等，但权重内容仍是 opaque——生产者在折 0 的代码路径里写出一个用全集训练的 `joblib`/checkpoint 并如实标注折 0 的样本列表，本契约无从发现。要收口这一条需要重演训练或记录可核对的拟合日志，属另一族能力。
- `@3` 的 `fit.features` 只是身份证据：DL 训练真正吃的是 `samples` 里的影像，特征表用来证明「这次拟合的样本全集与划分计划同源」，无法证明送进网络张量的像素与表中对应行一致；`@3` 也只实现了交叉验证方案，留出方案在声明层能接上训练行的 `split` 输入，但内容校验会以「仅支持交叉验证」拒绝——若要让留出方案可用，需要另写 `@3` 的留出分支与对应正反例。
- 消费端绑定仍是有意的窄：`ModelApply 1.0.0`、`Report 1.0.0`/`1.1.0`、`DLCNNInference 1.0.0`/`1.1.0` 这 5 处发布行继续不绑，「下游拒绝来历不明的模型」只在画板默认的新行上成立，存量工作流的节点仍指向不判血缘的旧版本。存量迁移没有批量脚本，靠前端「算子版本」选择器逐节点搬。精确 id/version 匹配的另一面也留在账上：绑 `@2` 使 `ModelApply 1.1.0`/`Report 1.2.0` 拒掉四个分类器的 `1.1.0`（整包血缘）与全部 DL 产出，绑 `@3` 的 `DLCNNInference 1.2.0` 反过来拒掉全部表格模型与 DL `1.0.0`/`1.1.0` 产出，而 `@1` 一代目前没有任何绑定消费端接受——收口要么让消费端能声明多版本（需先扩契约语法），要么让生产端收敛到一个血缘形态。绑定本身仍只改判新建运行，已创建的运行按冻结快照照样能跑完不合规的组合。
- 深度学习两行绑定同样只有 `docker` 一种运行态：`DLCNNTraining 1.1.0` 与 `DLCNNInference 1.2.0` 在注册表里都没有 `*-fc` 变体（门禁库 001→050 重放后复核为 0 行），云端优先配置下会静默回退本地 docker；GPU 镜像需重建并重新 `s deploy` 后再做真实 FC 上的分片合并与 checkpoint 重读验收。
- 已绑定的四个表格生产者会重建到云容器上运行，但四个分类器在注册表里只有 `1.0.0`/`1.1.0`/`1.2.0` 各一行 available，`*-fc` 变体为 0 行（本批重放 001→050 后按四类 × 三版本 SQL 复核）：云端优先配置下选到 `1.1.0` 或 `1.2.0` 都静默回退本地 docker，因此真实 FC 上的血缘发布——尤其逐折载荷的落盘与重读——尚未验收。
- `preprocess` 由实际流水线步骤名导出，但契约仍无法证明该变换确实作用于所引用的特征表；`family` 不做格式或魔数验证，`weights.path` 指向的 `joblib` 载荷是否可加载不在本契约内。
- 权重文件本身的内容（层数、张量形状、是否可加载）不在本契约内；影像/掩膜的几何与像素语义仍是下一个内容族。
