# 工作流文档

本目录是 Workflow IR、算子快照、产物边界与复现的执行契约集合。未列为已验收的能力不代表已经可用；部分契约原为闭源服务端实现记录，文中出现的服务端路径仅作历史沿革，开源仓不含该目录。

## 已实现说明

- [版本目录与精确解析（含服务端升级预检与编辑器入口）](operator-version-resolution.md)
- [数据预检：同步快检与异步深度预检（持久化作业、画像缓存、漂移失效与编辑器轮询/证据面板）](data-preflight.md)
- [特征表后置质量评估（运行后统计缺失值/常量特征并在运行详情展示）](feature-quality.md)
- [端口语义契约](contracts/README.md)
- [受控参数契约与当前接入范围](parameter-contract.md)
- [服务器 JSON 数值精度边界](json-precision.md)
- [版本归档](operator-archive.md)
- [已发布定义不可变](operator-immutability.md)
- [资源授权矩阵](resource-authorization.md)
- [运行创建与重试契约](run-creation-contract.md)
- [多数据源运行：绑定派生、执行隔离与受限能力](multi-dataset.md)
- [模型比较组：入组校验、指标聚合与缺失成员](run-groups.md)
- [多值端口与合并算子：协议 3、能力协商与 FeatureTableJoin](multi-value-ports.md)
- [外部验证：ModelApply 派发前的来源核验与证据](external-validation.md)
- [运行级算子快照与当前边界](run-snapshots.md)
- [精确执行版本与实现报告](execution-version-resolution.md)
- [冻结参数与平台控制契约](execution-contract.md)
- [冻结执行规则与运行策略](run-execution-policy.md)
- [SDK 冻结目录与独立复现](runner-catalog-replay.md)
- [产物边界与发布顺序](artifact-boundary.md)
- [分片来源与合并证据](shard-sources.md)
- [分片失败归因与取消边界](shard-diagnostics.md)
- [分片输入的写隔离](input-isolation.md)
- [输入间接引用闭包](input-reference-closure.md)

## 待交付

使用教程、Python 开发指南、清单与契约参考、SDK、云执行操作、诊断、迁移和测试架构文档按方案第 16 节逐阶段完成。CPU 自定义算子已自助开放；真实阿里云 FC 验收、GPU 与跨组织上架仍需通过门禁。
