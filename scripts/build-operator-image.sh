#!/usr/bin/env sh
# 构建算子云函数镜像（serve 模式入口，Dockerfile.operator）。
# 可选推送到镜像仓库；镜像随后部署到任意 Serverless 容器平台，
# 通用步骤见脚本头部「平台部署速览」。
#
# 用法：
#   scripts/build-operator-image.sh [--tag <tag>] [--registry <registry>] [--image <完整镜像名>]
#     --tag <tag>       镜像标签（默认 dev）
#     --registry <reg>  镜像仓库前缀（如 registry.cn-hangzhou.aliyuncs.com/my-ns），
#                       提供时构建后自动 docker push（镜像名拼为 <reg>/radstudio-operator:<tag>）
#     --image <name>    完整镜像名（含 tag），仓库路径非上述形状时直接指定；提供时构建后自动 push
#
# 平台部署速览（镜像推送到仓库后，三选一；部署架构详见 docs/cloud-deployment.md）：
#   - 阿里云 FC 容器模式（推荐）：改 packages/python-sdk/s.yaml 里的镜像地址与地域后
#     `RADSTUDIO_OPERATOR_TOKEN=<token> s deploy`；或控制台创建「自定义容器」函数，镜像地址填
#     推送结果，端口 8080，环境变量 RADSTUDIO_OPERATOR_TOKEN=<token>，HTTP 触发器路径 /invoke
#   - 腾讯云 SCF 容器镜像：云函数控制台创建「镜像部署」函数，同一镜像与端口，
#     环境变量同上，触发器 URL 路径设为 /invoke
#   - Google Cloud Run：gcloud run deploy radstudio-<operator> --image <image> \
#     --set-env-vars RADSTUDIO_OPERATOR_TOKEN=<token> --port 8080 --allow-unauthenticated
#     （认证建议改为平台侧服务账号 + Bearer token 双层）
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root_dir"

tag=dev
registry=""
image_override=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) tag=${2:?--tag requires a value}; shift 2 ;;
    --registry) registry=${2:?--registry requires a value}; shift 2 ;;
    --image) image_override=${2:?--image requires a value}; shift 2 ;;
    *) echo "未知参数: $1（用法: build-operator-image.sh [--tag <tag>] [--registry <registry>] [--image <完整镜像名>]）" >&2; exit 2 ;;
  esac
done

# --image 优先：仓库路径非 <prefix>/radstudio-operator 形状时（如 ACR 的 radstudio/radstudio）直接指定全名。
if [ -n "$image_override" ]; then
  image=$image_override
elif [ -n "$registry" ]; then
  image="$registry/radstudio-operator:$tag"
else
  image="radstudio-operator:$tag"
fi

docker build --provenance=false -f packages/python-sdk/Dockerfile.operator -t "$image" .
if [ -n "$registry" ] || [ -n "$image_override" ]; then
  docker push "$image"
fi
printf '算子镜像: %s\n' "$image"
