# 自定义算子开发（本地版）

本页说明如何在 RadStudio Local 里实现、调试并运行一个自定义算子。云端网页工作台的流程不适用于本地版；本地版用 Python + 统一契约 + Docker 镜像。

## 1. 算子的最小构成

一个算子由三部分组成：

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| 契约 | 算子目录中的 `submission.json` / 参数 schema | 类型、版本、参数、输入/输出端口 |
| 实现 | Python 类 + `@operator` 注册 | 消费 `OperatorRequest`，返回 `OperatorResult` |
| 运行 | `operator_serve` 或本地 runner | HTTP 模式（FC/容器）或 `--once` 单次调用 |

最小示例见 [`examples/custom-operators/brier-score/`](../../examples/custom-operators/brier-score/README.md)：它消费预测表、计算 Brier 分数，并带有离线校验脚本。

## 2. 契约与错误码

- 参数 schema：`packages/operator-schema/parameter-schema.schema.json`
- 请求/结果：`packages/operator-schema/operator-request.schema.json`、`operator-result.schema.json`
- 执行契约：`docs/workflows/execution-contract.md`
- 错误码：用 `operator.output_invalid` 表示输出不合契约，`user_dependency_blocked` 表示依赖不在白名单；不要把异常吞掉后返回空结果。

## 3. 实现骨架

```python
from radstudio.operator import Operator, OperatorContext, OperatorRequest, OperatorResult, operator

@operator("MyScore", "1.0.0")
class MyScore(Operator):
    def invoke(self, request: OperatorRequest, ctx: OperatorContext) -> OperatorResult:
        # request.parameters / request.inputs 均为已校验值
        # 用 ctx.artifact(...) 登记产物，不要直接写绝对路径
        ...
```

要求：

- 不读取契约之外的文件；不发起未声明的网络请求。
- 数值结果写入产物，日志走 stdout（NDJSON 事件），不要混入业务输出。
- 随机过程必须固定种子，并把种子写入产物元数据。

## 4. 本地调试

```bash
# 单次调用：--in 请求 JSON，--out 结果 JSON
python -m radstudio.operator_serve --once --in request.json --out result.json

# HTTP 模式（与云端一致的 /invoke 契约）
python -m radstudio.operator_serve --host 127.0.0.1 --port 8080
```

`request.json` 的结构见 `packages/operator-schema/operator-request.schema.json`；示例算子目录里也给了可直接运行的请求。

## 5. 打包与在本地 runner 中使用

```bash
# 用本仓 runner 镜像作为 SDK 基础镜像
docker build -t radstudio-sdk-base -f packages/python-sdk/Dockerfile.runner .
docker build -t my-operator \
  --build-arg SDK_IMAGE=radstudio-sdk-base \
  path/to/my-operator
```

随后在工作流的节点定义里把该算子指向你的镜像与入口；本地 runner 通过 `--request` 驱动执行。构建产物与 `request.json` 一起归档，保证可复现。

## 6. 发布到云端（可选）

云端版接受同一套契约：在算子工作台填写契约、粘贴代码、调试通过后发布到组织内。开源版与云端共享 `operator-schema`，因此本地调试通过的算子可以较平滑地迁移；发布门禁（依赖白名单、资源声明、内容契约）由云端执行。

## 7. 常见坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `operator.output_invalid` | 产物字段/类型与契约不符 | 对照 `artifact-content-contracts.json` |
| `user_dependency_blocked` | import 了镜像里不存在的包 | 只用声明过的依赖，或自建镜像 |
| 结果在本地与云端不一致 | 依赖版本/随机种子/浮点精度 | 固定 lock 与种子，记录环境 |
| 大文件超时/超限 | 把大数组塞进结果 JSON | 写产物并用 artifact 引用 |

> 禁止在算子代码、测试或示例中读取患者数据或硬编码院内路径。
