# 精确执行版本与实现报告

## 已接入的行为

节点指定的版本必须存在于执行环境，缺失时返回 `operator.version_unavailable`，不选择较新版本。Python HTTP serve 在下载/物化输入之前解析版本，失败会写入终态。SDK 直接 invoke 的空字符串、布尔值、数值和列表版本也不会被误认为“未指定版本”。

新平台请求使用冻结执行定义的版本。节点逻辑版本和执行版本可以不同：例如节点保存 `DataInput@1.0.0`，云优先配置在创建快照时选择 `1.0.0-fc`；实际请求发送 `1.0.0-fc`，复现请求记录同时保存原 `operatorVersion` 和 `executionVersion`。续跑核对的是同一冻结执行版本，不能拿逻辑版本代替执行绑定。

## 封闭历史映射

唯一来源为 `packages/operator-schema/legacy-fc-aliases.json`。只有表中明确列出的历史部署版本可映射到实现版本，例如 `DataInput@1.0.0-fc` → `DataInput@1.0.0`。环境真正注册完整 `1.0.0-fc` 时，完整精确版本优先；映射目标缺失时失败，不继续寻找最新版，也不对任意名称去掉后缀。

SDK 的普通 invoke 默认不启用历史映射，serve 入口启用这份封闭清单。映射不是用户提交的参数；修改清单须同步源码、Go embed 与 SDK 副本并通过共同回归。映射只解释历史命名，不证明镜像、函数版本或代码可信。

```powershell
python scripts/sync-operator-schemas.py --check
```

同步脚本覆盖当前已实现的参数 Schema、映射清单、请求与结果 Schema；不引用尚未交付的清单发布功能。省略 `--check` 会从唯一来源生成 Go/SDK 副本。

## 请求与结果

请求 requestId/runId/nodeId/operatorType 必须是非空字符串。HTTP operatorVersion 仅可缺失/null（历史兼容）或非空字符串。未指定版本仍可走历史 `legacy-latest`，会显式标记，不能作为可复现或云资格证明。新平台运行始终指定冻结版本。

结果 `implementation` 包含 operatorType、requestedVersion、resolvedVersion、selection；selection 为 exact、legacy-fc-alias、legacy-latest。旧无版本调用的 requestedVersion 为空字符串。字段见唯一来源的 `operator-request.schema.json` 与 `operator-result.schema.json`。

worker 对实际调用逐次检查报告，包括分片和合并；类型、请求版本、解析版本或选择规则不匹配时返回 `operator.implementation_mismatch`，不登记本次节点结果产物。完成事件保存 implementation 及 `implementationProvenance=reported-v1`。旧执行端完全缺少报告时标为 `unverified-legacy`，不伪造实现身份。

## 续跑与历史保护

完成事件的身份会随复现请求记录保留，续跑先对冻结执行版本核验再使用输出。标为 reported-v1 却缺少报告、报告为 null/错误形状、未知或互相矛盾的来源标记，均不能静默降级为旧未核验状态。

新尝试、失败、取消及最新损坏的完成记录会清除旧完成状态与身份。内存事件也深拷贝写入和读取，调用方不能通过保留指针篡改历史报告；不可 JSON 编码的事件在追加前拒绝。

内存原生输出 map 与 PG JSON 输出 map 使用相同解析逻辑；任何不完整的输出元数据不会被当成空输出成功记录。当前续跑的本地文件判定仍以文件存在性为基础，尚未替代后续内容哈希、端口内容与执行回执验证。

## 错误处理与发布边界

- `operator.version_unavailable`：环境缺少指定实现或历史映射目标。核对批准的执行版本/部署镜像，或在草稿中明确升级并重新校验，不自动替换版本。
- `operator.type_unavailable`：历史无版本请求未找到该算子类型。
- `operator.implementation_mismatch`：报告与冻结请求不符。检查混用的执行镜像、映射清单和 worker 版本；不能使用本次产物作为科研结果。

API/worker 与 SDK 执行镜像需同步交付，无数据库迁移。healthz 的 implementationSelection=exact-v1 只用于识别新入口。当前没有更新生产 FC 镜像或函数；真实阿里云 FC 执行与隔离仍是强制门禁。

执行报告是进程自报的一致性证据，不是代码签名或部署证明。不可变镜像/批准依赖、B8 已补 [冻结参数 HTTP 校验与内部控制分离](execution-contract.md)；端口实际内容、完整调用/分片回执及 FC 资格继续按总方案交付。CPU 自定义算子已自助开放；真实阿里云 FC 验收、GPU 与跨组织上架仍需通过门禁。回滚不得把旧环境无报告或会丢弃版本的行为解释为已核验执行。

## 验证方式

SDK 测试覆盖精确旧版本、完整标签优先、封闭映射及目标缺失、未知版本在下载前失败、旧无版本调用、非法直接调用、终态上报、UTF-8 --once 子进程。Go 测试覆盖身份不符不登记产物、来源标记、完成事件、真实冻结云变体派发和不重复执行的续跑、损坏身份拒绝及历史引用隔离。

Go↔Python HTTP 桥接使用真实本地 HTTP 客户端和 SDK 服务，合成算子验证精确版本、历史映射和缺失版本；这是本地互操作测试，不能算真实 FC 验收。显式指定 `RADSTUDIO_SDK_PYTHON` 执行 `go test -run '^TestSDKVersionBridge$' -count=1`；未指定时本地用例跳过，不计验收。CI 的 SDK 作业新增显式 Python 路径及桥接步骤；当前仅验证本地执行，没有推送或运行远端 CI。

本检查点最终验证：Go 全量独立 PG/Redis 与实际 SDK HTTP 桥接 15.607s、vet 通过；Python SDK 全量 198 项（170.35s）通过；Ruff、变更文件格式及四份契约/八份副本检查通过。
