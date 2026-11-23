# 受控参数契约 radstudio-parameters/1

## 当前接入范围

本检查点接入 Go 工作流校验、管理员注册默认值校验、worker 对待执行节点的冻结定义校验，以及 Python 单体 runner 的图校验。Schema 唯一源为 `packages/operator-schema/parameter-schema.schema.json`；API embed 和 SDK 目录中的副本由一致性测试比对。共同夹具位于 `packages/operator-schema/tests/parameter-cases.json`。

Python SDK 已有受控校验实现并通过同一参数/数值夹具。B8 已补 [HTTP 冻结参数与控制分离](execution-contract.md)，B7 已补 [精确版本选择与身份自报](execution-version-resolution.md)；代码/环境不可变绑定及完整调用回执仍未完成，不能据此宣称全部执行入口一致或自定义算子已开放。

## 受支持的定义

支持 object、array、string、number、integer、boolean，以及 properties、required、布尔 additionalProperties、items、enum、minimum、maximum、exclusiveMinimum、exclusiveMaximum、minItems、maxItems、uniqueItems 和 title/description/default 注解。未知关键字、`$ref`、pattern、format、条件与组合 Schema、union type 被明确拒绝。平台不会加载远程或本地引用。

minimum/maximum 包含边界；exclusiveMinimum/exclusiveMaximum 不包含边界。例如 testRatio 声明必须大于 0，填写 0 会在工作流校验和派发前被拒绝。整数 1 与 JSON 数值 1.0 相等；字符串 "1"、布尔 true 不会被转换为数字。uniqueItems 按 JSON 数值/结构语义判断，不通过字符串化规避重复。

required 必须存在；default 为注解，不隐式补齐参数。additionalProperties=false 拒绝未知参数。调用不会改写用户参数或 Schema。空 Schema 与开放对象保留 legacy 行为；未来新清单将另加严格发布规则，不把旧定义可读等同于新清单发布资格。

Schema 与参数各不超过 256 KiB、64 层。数字遵守当前 `json_contract.go` 的受限十进制规则，包括 JSONB 展开及小数尺度不超过 128 位、原始表示不超过 256 字符和指数绝对值不超过 308；超出时明确失败，不截断或转成浮点数。影像、表格和模型应经产物传递。编译缓存最多 64 份、源 Schema 总计不超过 2 MiB；缓存不保留调用方可变定义。

## 诊断与运行行为

诊断最多 20 条，区分 `parameter.invalid`（参数值）、`parameter.schema_invalid`（定义）、`parameter.json_invalid`（JSON/预算）。核心使用 JSON Pointer，工作流接口转换为 `parameters.field`、`parameters.list[0]`，带节点 ID；定义错误定位 `parametersSchema`。阈值中文提示保留精确数值，明确说明必须大于、小于或等于多少。

worker 使用已经解析的冻结定义，在暂存文件和注入内部并发/分片控制前校验逻辑参数。失败写入 `node.failed`，`errorCode=node.parameters_invalid`，附带字段与修复原因，且不调用节点执行器。已完成节点续跑沿用终态，不因为增加检查而重复计算。测试夹具补齐合法目录默认值，显式错误参数保持，生产代码不借此自动注入默认值。

冻结定义的校验视图现在使用精确深拷贝，取消之前的普通 JSON 浮点转换。运行与快照边界保留 json.Number；B6 已补服务器请求、注册 PG、草稿/版本、分享/模板与广场导入的精确数值回归；浏览器原生 Number、AI 参数转换及所有外部入口仍需单独验收，边界见 [服务器 JSON 数值精度](json-precision.md)。

## 兼容与交付门禁

过去被忽略的边界现在可能阻断旧工作流。用户应在草稿中修改参数并重新校验；参数规范本身不受支持时，作者应发布受支持的新版本，不能原地修改已发布定义绕过约束。API 与 worker 应同步更新，无数据库迁移。

后续预检规则版本、冻结执行规则、Python SDK 和 HTTP 服务必须使用相同契约及共同夹具。当前分支尚未接入旧分支的预检确认缓存，不能引用旧分支的 sample-profile 版本作为当前交付。真实阿里云 FC、批准环境、自定义隔离及完整文档仍以总方案为强制门禁。


## Python 开发接口与验证

```python
from radstudio.parameter_validation import (
    ParameterValidationError,
    require_valid_parameters,
    validate_parameter_values,
)

schema = {"type": "number", "exclusiveMinimum": 0}
issues = validate_parameter_values(0, schema)
# [{"code": "parameter.invalid", "severity": "error",
#   "fieldPath": "", "message": "数值必须大于 0"}]
require_valid_parameters(0.2, schema)  # 成功不返回错误；失败抛 ParameterValidationError
```

校验结果为 JSON Pointer 定位的列表，不修改调用参数；异常的 `issues` 保留同样的结构。Python 内部使用 Decimal 处理受限精确数值，布尔值不充当整数。单体 runner 在运行图校验时调用同一入口，参数错误不再只给出底层 JSON Schema 英文异常。

`json_contract.py` 严格拒绝重复键、非法 Unicode、非有限数字和超限深度/大小。数值预算与 Go 同步，包含 JSONB 展开限制。21 项共同数值夹具位于 `packages/operator-schema/tests/number-cases.json`，覆盖负零、大整数、小数、指数和超限拒绝；与 29 项参数夹具共同回归。

本批验证：当前分支 Python SDK 全量 183 项通过（105.29s），整个 SDK/测试 Ruff check 通过；变更文件格式检查与参数定向 57 项通过。Go 全量独立 PG/Redis 回归 12.232s，vet 通过。测试读取已有 Python 虚拟环境的依赖，PYTHONPATH 显式指向当前实施分支，未改主工作区依赖。
