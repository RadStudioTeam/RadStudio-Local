> **本地版说明**：本文改编自 RadStudio 云端版文档；涉及网页工作台、组织、计费、云函数等步骤在本地版不适用。本地操作以仓库 README、examples/ 与 docs/quickstart.md 为准。


# 算子契约与错误码参考

本页是 自定义算子开发 的配套参考，收录写算子时会反复查阅的内容：契约字段与规则、端口类型全集、参数 JSON Schema 可用关键字、执行上下文 API 成员表、依赖白名单，以及三组常见的错误码与中文含义。

> **怎么用这一页**
>
开发时不需要手抄这些结构：算子工作台的契约编辑器会按本页规则生成并校验内容。本页用于**看懂提示、核对字段、排查错误**。可直接运行的完整代码见 自定义算子示例。

## 1. 契约字段一览

| 字段 | 必填 | 规则 | 说明 |
| --- | --- | --- | --- |
| `type` | 是 | 字母开头，仅含字母 / 数字 / 下划线，长度 2–64 | 算子类型，运行时唯一标识 |
| `version` | 是 | `x.y.z`，可带 `-` 后缀（如 `1.0.0-beta.1`） | 同一组织内「类型 + 版本」唯一 |
| `name` | 是 | 1–100 字符 | 显示名称 |
| `category` | 是 | `data` / `features` / `model` / `results` 四选一 | 工作流面板分组 |
| `description` | 否 | ≤2000 字符 | 算法说明与注意事项 |
| `parametersSchema` | 否 | 顶层 `type` 必须是 `object` | 参数定义，见第 3 节 |
| `defaults` | 否 | 与 Schema 属性名对应 | 参数默认值 |
| `inputs` | 是 | 每项含 `name` / `type` / `required`，可含 `cardinality`、`contract` | 输入端口，见第 2 节 |
| `outputs` | 是 | 至少 1 项 | 输出端口 |
| `entrypoint` | 发布时必填 | `模块名:类名`，≤255 字符 | 例如 `brier_operator:BrierScore` |
| `dependencies` | 否 | ≤20 项，每项 ≤200 字符，不含空格与引号 | 声明用到的预置依赖 |
| `requiresGpu` | 否 | 布尔值，默认 `false` | 声明需要 GPU 运行时；勾选后调试与正式运行都路由到 GPU 环境，不回退 CPU |

## 2. 端口类型全集

端口 `type` 必须以字母开头，仅含字母 / 数字 / 下划线，最长 64 字符。平台支持的端口类型如下（与内置算子完全一致，见 算子目录）：

| 端口类型 | 承载内容 | 典型用法 |
| --- | --- | --- |
| `sample_collection` | 样本集合：图像、ROI、标签的清单 | 自定义算子最常见的输入，调试时可直接由数据集版本提供 |
| `feature_table` | 特征表：特征名列表 + 逐样本记录 | 读特征做统计、筛选、建模前置处理 |
| `prediction_table` | 预测表：逐样本的标签与概率 | 评价指标类算子的输入 |
| `model` | 训练产物 / 模型对象 | 加载模型做推理 |
| `split_plan` | 划分方案：训练 / 验证 / 测试的归属 | 复现实验、按划分重跑 |
| `metrics` | 指标与结论数值 | 自定义指标算子的输出 |
| `table` | 通用二维表 | 中间结果、自定义汇总表 |
| `chart` | 单张图表数据 | 自定义可视化 |
| `figure_bundle` | 图片 / 图表集合（打包产物） | 输出自己绘制的图 |
| `report` | 结构化报告文本 | 输出结论文档 |
| `archive` | 文件归档包 | 导出可下载的结果包 |
| `filter_strategy` | 筛选 / 质控策略 | 与内置质控算子衔接 |

端口的其余字段：

- **`required`**：布尔值。为 `true` 时上游必须连线（或调试时必须能提供该输入）。
- **`cardinality`**：仅**输入端口**可填，取值 `""`（默认，单个）、`"one"`、`"many"`。`many` 表示接收上游同一端口的全部产物，代码里用 `ctx.input_set()` / `ctx.input_set_json()` 读取，返回按声明顺序排列的列表。
- **`contract`**：可选的数据内容契约约束，用于要求上游数据满足特定结构。包含 `id`、`version`（必填）、`task`、`dimension`（`""` / `2d` / `3d` / `tabular`）、`modelFamily`。一般只在需要严格衔接内置算子时才填写；填错会导致连线被拒（提示「数据契约或版本不兼容，请使用显式适配算子」）。

## 3. 参数 JSON Schema 规则

参数用 JSON Schema 的**受限子集**描述，平台据此在工作流节点面板上渲染输入控件。

可用关键字：

| 关键字 | 适用范围 | 规则 |
| --- | --- | --- |
| `type` | 全部 | 只能是 `object` / `array` / `string` / `number` / `integer` / `boolean` |
| `title` | 全部 | 1–160 字符，面板上的字段名 |
| `description` | 全部 | 1–2048 字符，字段旁的提示 |
| `properties` | `object` | 最多 64 个属性，属性本身继续用同一套规则 |
| `required` | `object` | 最多 64 个名字，每个 1–80 字符，不可重复 |
| `additionalProperties` | `object` | 布尔值，**建议固定写 `false`** |
| `items` | `array` | 数组元素定义，递归同一套规则 |
| `minimum` / `maximum` | 数值 | 取值闭区间 |
| `exclusiveMinimum` / `exclusiveMaximum` | 数值 | 取值开区间 |
| `minItems` / `maxItems` | `array` | 0–1000000 |
| `uniqueItems` | `array` | 布尔值 |
| `enum` | 全部 | 1–128 个候选值，不可重复；渲染为下拉选择 |
| `default` | 全部 | 默认值，建议与契约顶层 `defaults` 保持一致 |

约定与限制：

- **顶层必须是 `object`**，否则报 `参数 schema 顶层 type 必须为 object`。
- **`additionalProperties` 必须显式设为 `false`**：这是平台内置算子的统一写法，能确保面板与运行时收到的参数完全一致，避免出现「面板上没显示却被接受」的字段。
- 不支持的关键字（例如 `$ref`、`allOf`、`oneOf`、`pattern`）请改写为上面的关键字组合，或直接去掉。
- 数值的有效范围受平台数值规范约束（极大 / 极小值会按上限处理），参数请使用常规范围。

示例：

```json
{
  "type": "object",
  "title": "算子参数",
  "additionalProperties": false,
  "properties": {
    "minN": { "type": "integer", "title": "最小样本量", "minimum": 1, "maximum": 100000, "default": 30 },
    "metric": { "type": "string", "title": "评价指标", "enum": ["auc", "accuracy", "f1"], "default": "auc" },
    "bootstrap": { "type": "boolean", "title": "计算置信区间", "default": false }
  },
  "required": ["minN"]
}
```

## 4. 执行上下文 API

算子实现用 SDK 的注册装饰器登记，并在 `run(self, ctx)` 中完成计算：

```python
from radstudio.operator import Operator, operator

@operator("sample_size_gate", version="1.0.0")
class SampleSizeGate(Operator):
    def run(self, ctx):
        rows = ctx.input_json("samples")          # sample_collection：样本清单（列表）
        gate = {"n": len(rows), "pass": len(rows) >= ctx.parameters["minN"]}
        ref = ctx.output_json("gate.json", gate, "metrics")
        return {"metrics": ref}                   # 键 = 契约里的输出端口名
```

要点：

- `@operator(类型, version=版本)` 的参数必须与契约中的 `type` / `version` **完全一致**。
- 类继承 `Operator`，实现 `run(self, ctx)`，**返回值是「输出端口名 → 产物引用」的字典**，键必须覆盖契约里所有必需输出端口。
- 产物用 `ctx.output_json(...)` / `ctx.output_bytes(...)` 写出，把它们的返回值作为字典的值返回。

### 4.1 `ctx`（执行上下文）成员

| 成员 | 签名 / 类型 | 说明 |
| --- | --- | --- |
| `parameters` | 字典 | 本次运行生效的参数（已合并默认值并通过契约校验） |
| `inputs` | 字典 | 输入端口名 → 产物引用；单值端口在此 |
| `input_bytes(name)` | → `bytes` | 读取某个输入端口的原始字节；端口缺失时报 `Missing required input: <端口名>` |
| `input_json(name)` | → 任意 | 读取并解析输入端口的 JSON；产物不存在报 `Artifact not found in workspace: <名称>`，哈希不符报 `Artifact hash mismatch: <名称>` |
| `input_set(name)` | → `list[bytes]` | 读取 `cardinality: "many"` 端口的全部产物；缺失报 `Missing required input set: <端口名>` |
| `input_set_json(name)` | → `list` | 同上，逐项解析为 JSON |
| `output_bytes(name, data, kind)` | → 产物引用 | 写出字节产物。`name` 为产物文件名（如 `figures.zip`），`kind` 为端口类型标识 |
| `output_json(name, value, kind)` | → 产物引用 | 写出 JSON 产物，序列化方式与平台一致，推荐用于结构化结果 |
| `progress(percent, message="")` | — | 上报进度百分比与说明，运行详情页日志区可见 |
| `check_cancelled()` | — | 协作式取消检查点；用户取消后在此抛出取消异常，长任务请**在样本边界周期性调用** |
| `store` | 产物存储 | 需要直接操作工作目录内的产物文件时使用：`read_bytes` / `read_json` / `write_bytes` / `write_json` / `write_local` / `resolve_path` / `register` / `artifacts` |
| `request` | 运行请求 | 本次运行的完整请求（类型与版本、节点标识、执行控制项等），一般只读；`request.nodeId` 常用于日志 |
| `emit(event, payload)` | — | 上报自定义事件；通常用 `progress()` 即可 |

### 4.2 异常与返回

| 情况 | 表现 |
| --- | --- |
| 算法本身出错 | 直接 `raise ValueError("中文原因")`，运行失败，日志里给出该原因 |
| 用户取消 | `ctx.check_cancelled()` 抛出 `OperatorCancelled`，运行状态为「已取消」 |
| 请求了不存在的算子类型 / 版本 | `operator.type_unavailable` / `operator.version_unavailable`，提示此执行环境未提供该算子 |
| 返回的端口不完整或多出 | 见第 5 节 `operator.output_invalid` 系列提示 |

## 5. 产物与文件规则

平台对算子读写产物设有边界，写代码时遵循三条即可：

1. **所有输出必须通过 `ctx.output_*` 提交**，不要手写产物引用字符串，也不要返回工作目录之外的路径。
2. **产物文件名使用 POSIX 相对路径**（如 `artifacts/metrics.json`），不能包含 `..`、绝对路径、反斜杠、符号链接，也不能使用平台保留的文件名。
3. **产物类型标识**必须与端口类型一致：以字母开头，可含字母 / 数字 / `.` `_` `/` `+` `@` `:` `-`，最长 128 字符。

常见的相关错误码：

| 错误码 | 中文含义 |
| --- | --- |
| `artifact.path_invalid` | 产物必须使用有界的 POSIX 相对路径 / 产物路径包含越界、非规范或保留名称 / 产物路径不能包含符号链接 / 产物越过工作区边界 |
| `artifact.type_invalid` | 产物物理类型标识无效 |
| `artifact.ref_invalid` | 产物引用不是有界文本 / 产物引用格式或 SHA-256 无效 |
| `artifact.file_missing` | 产物文件不存在或不是普通文件 |
| `operator.input_invalid` | 输入缺少必需端口 / 输入包含未声明端口 / 输入产物哈希不匹配 / 输入物理类型与声明不一致 / 输入集合只能用于多值端口 |
| `operator.output_invalid` | 输出缺少必需端口 / 输出包含未声明端口 / 重复产物声明 / 产物哈希或大小与实际文件不一致 / 输出未登记为产物，也不是显式输入透传 |

> **调试时最容易触发的一条**
>
`输出缺少必需端口：<端口名>` —— `run()` 返回的字典必须覆盖契约里每一个必需输出端口，名字要和端口 `name` 一字不差。

## 6. 依赖白名单

平台预置以下第三方库，**只能使用这些，不能自行安装其他依赖**：

### 6.1 CPU 运行时

| 声明写法 | 导入名 | 用途 |
| --- | --- | --- |
| `numpy` | `numpy` | 数值计算 |
| `pandas` | `pandas` | 表格处理 |
| `scipy` | `scipy` | 科学计算 |
| `scikit-learn` | `sklearn` | 机器学习与评价指标 |
| `pillow` | `PIL` | 图像读写与绘制 |
| `matplotlib` | `matplotlib` | 绘图 |
| `openpyxl` | `openpyxl` | Excel 读写 |
| `pyradiomics` | `radiomics` | 影像组学特征 |

### 6.2 GPU 运行时追加

契约勾选「需要 GPU」（`requiresGpu: true`）后，在上表基础上追加：

| 声明写法 | 导入名 | 用途 |
| --- | --- | --- |
| `torch` | `torch` | 深度学习框架 |
| `torchvision` | `torchvision` | 视觉模型与图像变换 |
| `monai` | `monai` | 医学影像深度学习 |

GPU 运行环境需由平台为组织开通；未开通时，勾选「需要 GPU」的运行会明确提示运行时不可用，不会回退到 CPU。

规则：

- 声明 `dependencies` 只用于发布前提示与检查，写**发行名**即可，如 `scikit-learn`；平台自动识别对应的导入名（`sklearn`）。
- 声明项不能含空格或引号（`numpy >= 1.24` 会被判为 `依赖项不合法`，`numpy>=1.24` 可以通过），最多 20 项。
- Python 标准库默认可用；**不要**在代码里安装或下载依赖，也不要联网取数据。
- 白名单之外的库会在运行时导入阶段被拒绝，看到 `依赖 X 不在平台预置白名单内，可用依赖：…` 时，请改写算法或换用上面的库。
- 导入 `torch` / `torchvision` / `monai` 但没有勾选「需要 GPU」时，静态检查报 `operator.gpu_required`。

## 7. 错误码与中文含义

### 7.1 契约校验（新建 / 保存草稿 / 发布前）

| 中文提示 | 处理办法 |
| --- | --- |
| 算子类型须以字母开头，仅含字母/数字/下划线，长度 2–64 | 改类型名，例如 `sample_size_gate` |
| 版本号须为 x.y.z（可带 -后缀） | 用 `1.0.0` 这样的三段式版本号 |
| category 必须为 data/features/model/results 之一 | 重新选择分类 |
| 算子名称必填且不超过 100 字符 | 缩短名称 |
| 描述不超过 2000 字符 | 精简描述 |
| 参数 schema 顶层 type 必须为 object | 补上 `"type": "object"` |
| 输入端口名 "x" 不合法：以字母开头，仅含字母/数字/下划线 | 端口名不能用中划线或数字开头 |
| 输入端口名重复：samples / 输出端口名重复：metrics | 同一算子内端口名唯一 |
| 输入 samples 的数据类型 "png" 不合法 | 端口类型必须取自第 2 节列表 |
| 依赖不超过 20 项 / 依赖项不合法："numpy >= 1" | 去掉空格与约束，或减少声明 |
| 提交前须填写算子入口（如 my_operator:MyOperator） | 填 `模块名:类名` |
| 提交前须上传实现代码包 | 保存并上传代码 |
| 提交前须至少定义一个输出端口 | 至少声明一个输出端口 |
| 版本 my_op/1.0.0 已注册 | 同组织内同类型同版本已存在，请另存新版本 |
| 算子类型 my_op 与已有算子冲突，请改用新的类型名 | 该类型名已被占用，换一个更具描述性的名字 |
| 算子类型 my_op 已被其他组织注册，请改用新的类型名 | 类型名先到先得，请改名 |

### 7.2 发布门禁与代码装载

| 提示 / 错误码 | 中文含义 | 处理办法 |
| --- | --- | --- |
| `实现包超过 16MB 上限` | 代码包太大 | 删除数据文件、图片等不该进代码包的产物 |
| 实现包必须为 zip 格式 | 上传的不是 zip | 用工作台保存 / 打包，或本地压缩为 zip |
| 实现包不能为空 | 空包 | 至少保存一个 `.py` 文件 |
| `user_entrypoint_invalid`：算子入口必须是 module:Class 形式 | 入口写法不对 | 写成 `main:MyOperator`；不支持函数入口 |
| `user_entrypoint_invalid`：算子入口模块名无效 / 算子入口类名无效 | 模块名或类名不是合法标识符 | 与文件名、类名严格一致，注意大小写 |
| `code_archive_invalid`：用户代码包必须是 zip 或 tar.gz | 包格式不受支持 | 只上传 zip |
| `code_hash_mismatch`：用户代码包摘要不匹配 | 包内容与登记指纹不一致 | 在工作台重新保存一次代码 |
| `code_too_large`：用户代码包条目数 N 超过上限 | 文件数过多 | 精简为一个或少数几个源文件 |
| `code_too_large`：用户代码包单文件 / 解压总量超过上限 | 单文件或总大小超限 | 拆分代码，数据放数据集 |
| `code_path_unsafe`：用户代码包条目路径非法 / 是绝对路径 / 越出目标目录 / 不得包含符号链接 | 包内有不安全路径 | 只在根目录放普通 `.py` 文件，不要带绝对路径与链接 |
| `user_dependency_blocked`：依赖 X 不在平台预置白名单内，可用依赖：… | 导入了不支持的库 | 换用第 6 节的库，或自行实现 |
| `operator.gpu_required`：导入 X 需要 GPU 运行时 | 代码导入了 `torch` / `torchvision` / `monai`，但契约没有勾选「需要 GPU」 | 在契约勾选「需要 GPU」后重跑门禁 |
| 文件名与平台模块或标准库重名 | 你的 `.py` 文件名占用了标准库名字 | 改名，例如不要叫 `json.py`、`os.py`、`radstudio.py` |

### 7.3 调试与运行

| 提示 / 错误码 | 中文含义 | 处理办法 |
| --- | --- | --- |
| `Missing required input: predictions` | 调试选的数据集没有覆盖该必需输入端口 | 换一个数据集版本，或先跑上游算子 |
| `Missing required input set: samples` | 多值端口没有收到任何输入 | 检查连线与 `cardinality` 设置 |
| `Artifact not found in workspace: x` | 想读的产物不在本次工作目录里 | 只读契约声明的输入端口 |
| `Artifact hash mismatch: x` | 产物内容与记录不一致 | 重新运行，不要手工改写产物文件 |
| `user_timeout`：运行时间超过平台上限，已终止本次运行 | 超时候被停止 | 调试时用「前 N 条样本」，或优化算法 |
| `operator.cancelled`：运行被取消 | 你主动点了取消 | 无需处理；长任务请加 `ctx.check_cancelled()` |
| `operator.version_unavailable`：此执行环境未提供算子版本 X@1.0.0 | 工作流引用的版本未在本组织发布 | 确认该版本已发布，或改用已发布版本 |
| `operator.type_unavailable`：此执行环境未提供算子 X | 类型名不存在或已下架 | 检查算子是否已发布、是否被下架 |
| `operator.failed` | 算子代码抛出异常 | 看日志里的 Python 异常与你的 `raise` 原因 |
| 同组织下已存在相同类型与版本的提交 | 重复创建 | 直接编辑已有草稿，或另存新版本 |
| 只有提交人可以编辑草稿 / 该算子版本已注册，不可修改 | 权限或不可变限制 | 由创建人编辑；已发布版本只能另存新版本 |

## 8. 状态取值

算子工作台与接口中的状态含义：

| 状态 | 含义 |
| --- | --- |
| `draft` | 草稿，可反复修改，仅本组织可见 |
| `ready` | 自动门禁全部通过，等待你发布 |
| `published` | 已发布，本组织工作流可用，内容不可修改 |
| `rejected` | 已被平台管理员下架（附原因），内容同样不可修改；可删除这条记录，或另存为新版本后重新发布（已发布过的版本号会保留在历史中，不能再次使用） |
| `submitted` / `approved` | 早期评审流程的历史状态，现已不再产生，仅在历史数据中出现 |

已发布版本在工作流目录中的可见性状态：`available`（可用）、`deprecated`（已弃用，仍可用但不推荐）、`archived`（已归档，不可用于新运行）。

## 9. 相关文档

- 自定义算子开发：完整开发流程与平台限制清单。
- 自定义算子示例：三个可直接复制的算子。
- 算子目录：内置算子的端口类型与参数。
- 数据接口参考：自定义算子相关接口。
- 核心概念：算子、契约与不可变版本。
- 常见问题：快捷答疑。
