#!/usr/bin/env sh
# Build the runner image and pin its immutable digest in .env so the API and
# worker can launch experiment containers. Idempotent: rewrites the existing
# RADSTUDIO_RUNNER_IMAGE line in place and creates .env when absent.
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root_dir"

image=radstudio-runner:dev
# UV_DEFAULT_INDEX（环境变量，可选）：GitHub/PyPI 直连不稳时传国内镜像
docker build --provenance=false \
    ${UV_DEFAULT_INDEX:+--build-arg UV_DEFAULT_INDEX="$UV_DEFAULT_INDEX"} \
    -f packages/python-sdk/Dockerfile.runner -t "$image" .
digest=$(docker inspect --format '{{.Id}}' "$image")

env_file=.env
if [ -f "$env_file" ] && grep -q '^RADSTUDIO_RUNNER_IMAGE=' "$env_file"; then
	tmp=$(mktemp)
	grep -v '^RADSTUDIO_RUNNER_IMAGE=' "$env_file" >"$tmp"
	printf 'RADSTUDIO_RUNNER_IMAGE=%s\n' "$digest" >>"$tmp"
	mv "$tmp" "$env_file"
else
	printf 'RADSTUDIO_RUNNER_IMAGE=%s\n' "$digest" >>"$env_file"
fi

printf 'RADSTUDIO_RUNNER_IMAGE=%s\n' "$digest"
