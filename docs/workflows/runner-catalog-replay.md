# SDK 冻结目录与独立复现

## 当前交付范围

SDK 顺序 runner 接受 `operatorCatalogSnapshot`。提供该字段时，必须验证摘要、规则版本、节点集合、类型和精确版本；损坏或显式 null 均拒绝，不回退读取安装目录。旧请求没有该字段时，在本次执行前捕获安装目录，不将其描述为历史运行的原始目录。

Go 单体运行派发已注入目录并强制验证 `run.catalog.accepted`；节点执行路径也捕获公开目录，用于 Report 审计包。节点 HTTP 调用继续使用已有冻结参数契约。完整调用回执、产物内容验证、分布式执行复现和 FC 验收仍待实施。

## 公共契约

源定义：`packages/operator-schema/operator-catalog-snapshot.schema.json`。运行 `python scripts/sync-operator-schemas.py` 同步到 Go/SDK；`--check` 检查副本及 SDK 安装目录文件与根目录一致。

目录 v1 包含 `source`、固定 `rules`、按节点 ID 索引的 `nodes`、`digest`。每项绑定请求的类型/逻辑版本与公开定义。公开定义不包含 Runtime、地址或鉴权信息。`entrypoint` 仅作元数据，本实现不据此动态导入自定义代码。

摘要为移除 digest 字段后的 radstudio-cjson/1 SHA256；目录限制 16 MiB、深度 96、1—500 个节点。版本必须精确一致，或属于已有封闭 FC 历史映射。SDK 执行选用冻结定义中的实际版本，参数契约绑定同一版本；逻辑版本保留在目录请求字段中，不伪装成实际实现版本。执行前检查目标子图全部实现是否存在，再创建输出目录。

runner 请求通过保留 Decimal 的传输解析器读取，限 32 MiB、深度 128；参数仍接受独立的参数契约预算，不能靠传输预算绕过。科学输出的小数不使用参数的 128 位展开限制。请求、快照和参数验证保留精确数值；sklearn 逻辑回归的 C / max_iter 在调用库的位置转换为其要求的 float / int，不改变已冻结请求。

## 执行与排错

目录验证失败报 `runner.catalog_invalid`，实现不可用报 `operator.version_unavailable`。参数与平台控制沿用 execution-contract 文档中的错误码。单体派发优先使用运行快照，旧运行没有快照时标记 registry-at-dispatch，不能当作历史定义证明。目录接受后产生 `run.catalog.accepted`；输出目录保存 `operator-catalog.json` 和 `execution-provenance.json`，后者记录 running/completed/failed、目录和工作流摘要、实际实现报告及参数回执。当前参数回执仅绑定定义摘要，不能作为完整调用或环境证明。

## 复现包

SDK Report 的 reproduction_bundle 包含本次请求的必要字段、冻结目录、SDK Python 源码、JSON 资源、执行来源、输入哈希和包内文件哈希。输入影像与 ROI 单独提供，不嵌入 ZIP；空 ROI 不读取当前目录。未知请求顶层字段不导出。

在可信且兼容的批准 Python 环境中解压，把原始输入按 input-hashes.json 名称放入独立目录，然后执行：

```sh
python reproduce.py --dataset-dir /path/to/inputs --output /path/to/output
```

入口先检查包内文件哈希，再读取所有输入并核验 SHA256，然后使用包内 SDK 运行。拒绝绝对路径、父目录跳转、Windows 驱动器和逃逸数据目录的链接；输入被修改时在创建输出目录前失败。request.json 不回退使用当前安装的目录。

requirements.lock 是生成时安装版本的观察记录，尚非带发行物哈希的批准环境锁。文件哈希可发现意外损坏，不能防止攻击者同时替换代码和哈希清单；请只执行可信复现包。镜像字段、目录摘要和 SDK 自报身份均不证明真实 FC 环境、隔离、权限或完整科研复现。

Go worker 的既有 Report 审计包不因此成为该独立 SDK 包；真实科学全链路、历史包迁移与 FC 云端复现仍需专门验收。


## 服务端接受门禁

Go 比较目录回执的版本、摘要及来源，先冻结期望值，防止执行器改写传入对象后改写核验目标。JSON 数字按原值读取，不能把接近 1 的小数舍入成协议版本 1。相同回执去重，冲突回执终止执行。目录未确认时的节点开始、节点完成、产物和运行完成事件均拒绝。

回调结束与回执状态读取同步；Execute 返回后到达的事件不再写库。节点或运行报告失败，即使执行器返回 nil 也失败；失败后的开始/完成/产物不再登记。运行成功由 worker 在 Execute 及目录门禁通过后统一写入，SDK 的 run.completed 不直接作为成功依据。

失败码 runner.catalog_unconfirmed 表示镜像/SDK 没有正确确认本次目录，应核对批准镜像与 worker/SDK 版本，不得静默切回无冻结模式。SDK 已报告的具体错误码保留。派发前目录构造失败通过既有未启动失败路径完成终态和计费清算。

Go Report 的 operators.json 现在仅含公开定义，operator-catalog.json 附带公开目录；均不导出内部运行地址或鉴权。该包仍是审计包，不包含可执行 SDK 和依赖发行物，不能单独执行。

验证包括：真实 Go→SDK 跨语言摘要与封闭 FC 历史映射解析（不调用 FC）、不读取安装目录、缺失/重复/冲突/精度异常回执、失败事件配成功退出、失败后产物拦截、迟到事件以及审计地址不导出。真实云执行及完整科研内容验证不由这些测试替代。


SDK 新目录的 artifactExecution=1 依赖 contentContracts=1，端口信封参与参数定义摘要；旧目录不补标记。实际执行版本与端口共同冻结，详见 [执行契约](execution-contract.md)。本扩展不把参数定义回执升级成完整调用或环境证明。
