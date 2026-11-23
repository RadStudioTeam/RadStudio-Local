# ResearchBrierScore 1.0.0

完整可运行的自定义算子开发样例，消费平台分类器的 prediction_table，计算二分类 Brier 分数：`mean((probability - label)^2)`。正类固定为 1，逐图像等权，无拟合、阈值优化或附加依赖（使用已有 SDK）。输出不证明患者独立性；多图像患者研究须另行确定权重。

## 本地打包与复跑

在仓库根目录使用已有 SDK 环境：

```powershell
& packages/python-sdk/.venv/Scripts/python.exe examples/custom-operators/brier-score/verify.py .data/research/brier-score
```

产出可上传 ZIP 与 verification.json，含源码/包哈希、Python 与 sklearn 版本、确定性结果引用。脚本通过真实 `serve.py --once` 请求/响应与产物包传输运行两次，对照手算 0.0925 和 sklearn，并验证空表、重复样本、非法概率/标签及错误版本拒绝。仅使用合成数据。

## 提交、评审与接入

1. 在「自定义算子」创建草稿，按 submission.json 填写类型、版本、端口、入口及参数；上传生成的 ZIP，提交评审。无额外依赖，SDK 基础环境版本由平台固定。
2. 平台评审通过后状态为 pending_runtime。此状态不能执行，不应改成 available 来跳过接入。
3. 平台将代码放入批准的 SDK 镜像，使用本目录 Dockerfile 构建；SDK_IMAGE 必须使用确定的镜像 digest。记录源码包哈希、SDK 镜像 digest、最终镜像 digest 与依赖清单。
4. Docker 调用入口沿用 `--once --in ... --out ...`；FC 使用同一镜像的 HTTP 模式、8080 端口和 /invoke，配置独立调用令牌 RADSTUDIO_OPERATOR_TOKEN。镜像必须实际导入 brier_operator；仅填写 entrypoint 不会自动加载代码。
5. 通过管理端现有运行时配置绑定批准镜像/端点；验收请求身份、输入输出哈希、资源限额、超时、网络/文件隔离、无凭据泄露及重复请求行为后才启用。FC 首次开放必须完成真实隔离与执行验收。
6. 在分类模板的任一模型 predictions 输出后连接 ResearchBrierScore@1.0.0。保留原 Evaluation，核对两者 Brier 分数。

## 版本与回放

类型和版本固定为 ResearchBrierScore@1.0.0。修改计算或输入语义时发布新版本；既有工作流继续固定旧版本。复跑必须使用相同输入、源码包及镜像 digest；不要把 -fc 当成自动存在的实现别名。

平台审核、运行时绑定与 FC 验收不由离线脚本自动操作。离线结果不得表述为已上线或已通过真实队列验证。该样例是标准统计量的实现验证，不是某篇临床论文结果复现。

## 在 RadStudio Local 中使用

开源版不依赖云端平台。用本仓 runner 镜像作为 SDK 基础镜像即可本地构建与验证：

```bash
# 在仓库根执行
docker build -t radstudio-sdk-base -f packages/python-sdk/Dockerfile.runner .
docker build -t brier-score \
  --build-arg SDK_IMAGE=radstudio-sdk-base \
  examples/custom-operators/brier-score
```

离线验证脚本使用本地 SDK 环境（需先 `cd packages/python-sdk && uv sync`）：

```bash
python examples/custom-operators/brier-score/verify.py <workdir>
```

上文提到的「平台评审/运行时绑定」属于云端平台流程；本地使用只需 Docker 镜像与上面的验证脚本。
