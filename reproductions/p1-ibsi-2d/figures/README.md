# figures

- `p1-ibsi-2d-by-family.svg`：图 1，按特征族的相对偏差均值与最大值。
- `p1-ibsi-2d-per-feature.svg`：图 2，35 个可比特征的逐特征相对偏差。
- `source/by-family.csv`：图 1 源数据（运行产物 `07-comparison_by-family.csv`）。
- `source/by-feature.csv`：图 2 源数据（由 `07-comparison.csv` 抽取非空相对偏差行）。

两张图都只展示偏差，不包含容差判定；生成脚本 `scripts/build_ibsi_2d_reproduction_package.py`。
