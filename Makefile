PY := packages/python-sdk

.PHONY: help sync test lint selfcheck

help:
	@echo "make sync      安装 SDK 开发依赖"
	@echo "make test      运行 Python 测试"
	@echo "make lint      ruff"
	@echo "make selfcheck 运行 IBSI 2D 数字体模自检"

sync:
	cd $(PY) && uv sync --dev

test:
	cd $(PY) && uv run pytest -q

lint:
	cd $(PY) && uv run ruff check .

selfcheck:
	bash reproductions/p1-ibsi-2d/reproduction/run.sh
