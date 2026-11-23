# metrics

- `comparison.csv`：54 行逐特征比对（参考值、计算值、偏差、容差、结论）。
- `comparison-summary.json` / `comparison-report.json`：汇总与报告。
- `by-family.csv`：按特征族的可比/不可比行数与偏差统计。
- `not-comparable-reasons.json`：不可比原因计数。
- `run-summary.json`：运行登记（节点、参数、输入哈希、限制）。

结论只允许 `no_tolerance` 与 `not_comparable`：数字体模未公布容差，**不产出 pass/fail 一致率**。
