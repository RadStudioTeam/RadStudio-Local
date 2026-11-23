#!/usr/bin/env bash
# 在仓库根执行；需要 uv 与 Python 3.12。
set -euo pipefail
REPO_ROOT="${1:-$(cd "$(dirname "$0")/../../.." && pwd)}"
RUN_DIR="${2:-$REPO_ROOT/runs/p1-ibsi-rerun}"
cd "$REPO_ROOT/packages/python-sdk"
uv sync --frozen --no-dev
cd "$REPO_ROOT"
packages/python-sdk/.venv/bin/python scripts/run_ibsi_phantom_selfcheck.py \
  --run-id p1-ibsi-rerun --run-dir "$RUN_DIR"
