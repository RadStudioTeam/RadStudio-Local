# 在仓库根执行；需要 uv 与 Python 3.12。
param(
  [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path,
  [string]$RunDir = ''
)
$sdk = Join-Path $RepoRoot 'packages/python-sdk'
Push-Location $sdk
uv sync --frozen --no-dev
Pop-Location
if (-not $RunDir) {
  $RunDir = Join-Path $RepoRoot 'runs/p1-ibsi-rerun'
}
& (Join-Path $sdk '.venv/Scripts/python.exe') `
  (Join-Path $RepoRoot 'scripts/run_ibsi_phantom_selfcheck.py') `
  --run-id p1-ibsi-rerun --run-dir $RunDir
