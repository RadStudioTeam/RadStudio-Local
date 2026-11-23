"""IBSI 参考值比对算子。

``IBSIReferenceComparison`` 把特征表与 IBSI 官方 2D 参考值逐行对照：

- 输入：``features``（feature_table）、``reference``（table）、``mapping``（table）；
- 输出：``comparison``（逐行对照表）、``summary``（metrics）、``report``（report）。

比对规则预注册、不可配置：有容差的行按 ``|计算值 - 参考值| <= 容差`` 判定；
参考手册未给出容差的行只报告偏差并标记 ``no_tolerance``，不计入一致率分母。
算子不提供「自设阈值」参数。
"""

from __future__ import annotations

import math

from .execution_contract import execution_parameters
from .operator import Operator, operator

REFERENCE_COLUMNS = (
    "code",
    "name",
    "family",
    "dataset",
    "aggregation",
    "value",
    "tolerance",
    "consensus",
)
MAPPING_COLUMNS = ("code", "status", "pyradiomicsClass", "pyradiomicsFeature")

AGGREGATIONS = ("2D", "2D, averaged", "2D, slice-merged")
RECORD_AGGREGATIONS = ("per_slice_mean", "single_record")

CONCLUSION_ORDER = (
    "pass",
    "fail",
    "no_tolerance",
    "not_comparable",
    "feature_missing",
)


def _table_rows(table, label: str) -> list[dict]:
    if not isinstance(table, dict):
        raise ValueError(f"{label} 输入必须是对象，实际为 {type(table).__name__}")  # noqa: TRY004
    rows = table.get("records")
    if not isinstance(rows, list):
        raise ValueError(f"{label} 输入缺少 records 数组")  # noqa: TRY004
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{label} 第 {index + 1} 行不是对象")  # noqa: TRY004
    return rows


def _require_columns(table, label: str, columns: tuple[str, ...]) -> None:
    declared = table.get("columns")
    if declared is None:
        return
    declared_set = {str(name) for name in declared}
    missing = [name for name in columns if name not in declared_set]
    if missing:
        raise ValueError(f"{label} 表缺少列：{missing}")


def _number(value, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是数值：{value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} 不是有限数值：{value!r}")
    return number


def _aggregate_feature_values(features: dict, record_aggregation: str) -> tuple[dict, int]:
    names = features.get("featureNames")
    records = features.get("records")
    if not isinstance(names, list) or not names:
        raise ValueError("特征表缺少 featureNames")
    if not isinstance(records, list) or not records:
        raise ValueError("特征表没有样本记录")
    if record_aggregation == "single_record" and len(records) != 1:
        raise ValueError(
            f"recordAggregation=single_record 要求特征表恰好 1 条记录，实际 {len(records)} 条"
        )
    width = len(names)
    for index, record in enumerate(records):
        values = record.get("values")
        if not isinstance(values, list) or len(values) != width:
            raise ValueError(
                f"特征表第 {index + 1} 条记录的 values 数量与 featureNames 不一致"
            )

    aggregated: dict[str, float | None] = {}
    for column, name in enumerate(names):
        series = [
            _number(record["values"][column], f"特征 {name}")
            for record in records
        ]
        numeric = [value for value in series if value is not None]
        aggregated[str(name)] = (sum(numeric) / len(numeric)) if numeric else None
    return aggregated, len(records)


def _mapping_index(rows: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("code") or "").strip()
        if not code:
            raise ValueError("映射表存在空 code")
        if code in index:
            raise ValueError(f"映射表 code {code!r} 重复")
        index[code] = row
    return index


@operator("IBSIReferenceComparison", version="1.0.0")
class IBSIReferenceComparisonOperator(Operator):
    """IBSI 2D 参考值逐行比对：有容差判 pass/fail，无容差只报偏差。"""

    def run(self, ctx):
        params = execution_parameters(ctx)
        dataset = str(params.get("dataset") or "dig. phantom")
        aggregation = str(params.get("aggregation") or "2D")
        record_aggregation = str(params.get("recordAggregation") or "per_slice_mean")
        image_type = str(params.get("imageType") or "original")

        if aggregation not in AGGREGATIONS:
            raise ValueError(f"aggregation 不支持：{aggregation!r}")
        if record_aggregation not in RECORD_AGGREGATIONS:
            raise ValueError(f"recordAggregation 不支持：{record_aggregation!r}")
        if record_aggregation == "per_slice_mean" and aggregation != "2D":
            raise ValueError(
                "recordAggregation=per_slice_mean 只能用于 aggregation=2D：IBSI 的 "
                "2D, averaged 与 2D, slice-merged 需要矩阵级聚合，"
                "不能由逐层特征值求均值重建"
            )

        features = ctx.input_json("features")
        computed, record_count = _aggregate_feature_values(features, record_aggregation)

        reference_table = ctx.input_json("reference")
        _require_columns(reference_table, "reference", REFERENCE_COLUMNS)
        reference_rows = _table_rows(reference_table, "reference")

        mapping_table = ctx.input_json("mapping")
        _require_columns(mapping_table, "mapping", MAPPING_COLUMNS)
        mapping = _mapping_index(_table_rows(mapping_table, "mapping"))

        selected = [
            row
            for row in reference_rows
            if str(row.get("dataset") or "") == dataset
            and str(row.get("aggregation") or "") == aggregation
        ]
        if not selected:
            available = sorted(
                {
                    f"{row.get('dataset')} / {row.get('aggregation')}"
                    for row in reference_rows
                }
            )
            raise ValueError(
                f"参考表没有 dataset={dataset!r} 且 aggregation={aggregation!r} 的行；"
                f"可用组合：{available}"
            )

        rows = []
        for reference_row in selected:
            code = str(reference_row.get("code") or "").strip()
            if not code:
                raise ValueError("参考表存在空 code")
            reference_value = _number(
                reference_row.get("value"), f"参考值 {code}"
            )
            if reference_value is None:
                raise ValueError(f"参考值 {code} 缺少数值")
            tolerance = _number(reference_row.get("tolerance"), f"容差 {code}")

            entry = {
                "ibsiCode": code,
                "ibsiName": reference_row.get("name"),
                "family": reference_row.get("family"),
                "dataset": dataset,
                "aggregation": aggregation,
                "referenceValue": reference_value,
                "tolerance": tolerance,
                "consensus": reference_row.get("consensus"),
                "pyradiomicsKey": None,
                "computedValue": None,
                "absoluteDeviation": None,
                "relativeDeviation": None,
                "conclusion": None,
                "note": None,
            }

            mapped = mapping.get(code)
            if mapped is None:
                entry["note"] = "参考值代码不在映射表中"
                entry["conclusion"] = "not_comparable"
                rows.append(entry)
                continue
            status = str(mapped.get("status") or "")
            if status == "missing":
                entry["note"] = str(mapped.get("note") or "PyRadiomics 无对应实现")
                entry["conclusion"] = "not_comparable"
                rows.append(entry)
                continue

            class_name = str(mapped.get("pyradiomicsClass") or "")
            feature_name = str(mapped.get("pyradiomicsFeature") or "")
            if not class_name or not feature_name:
                raise ValueError(f"映射表 {code!r} 缺少 pyradiomicsClass/pyradiomicsFeature")
            key = f"{image_type}_{class_name}_{feature_name}"
            entry["pyradiomicsKey"] = key
            computed_value = computed.get(key)
            if computed_value is None:
                entry["note"] = "特征表中找不到该键"
                entry["conclusion"] = "feature_missing"
                rows.append(entry)
                continue

            deviation = abs(computed_value - reference_value)
            entry["computedValue"] = computed_value
            entry["absoluteDeviation"] = deviation
            entry["relativeDeviation"] = (
                deviation / abs(reference_value) if reference_value != 0 else None
            )
            if tolerance is None:
                entry["conclusion"] = "no_tolerance"
                entry["note"] = "参考手册未给出容差，只报告偏差，不计入一致率"
            else:
                entry["conclusion"] = "pass" if deviation <= tolerance else "fail"
                entry["note"] = None
            rows.append(entry)

        columns = [
            "ibsiCode",
            "ibsiName",
            "family",
            "dataset",
            "aggregation",
            "referenceValue",
            "tolerance",
            "consensus",
            "pyradiomicsKey",
            "computedValue",
            "absoluteDeviation",
            "relativeDeviation",
            "conclusion",
            "note",
        ]

        counts = {name: 0 for name in CONCLUSION_ORDER}
        by_family: dict[str, dict[str, int]] = {}
        for row in rows:
            counts[row["conclusion"]] += 1
            family = str(row.get("family") or "")
            family_counts = by_family.setdefault(
                family, {name: 0 for name in CONCLUSION_ORDER}
            )
            family_counts[row["conclusion"]] += 1

        compared = counts["pass"] + counts["fail"]
        summary = {
            "dataset": dataset,
            "aggregation": aggregation,
            "recordAggregation": record_aggregation,
            "imageType": image_type,
            "featureRecords": record_count,
            "referenceRows": len(rows),
            "conclusionCounts": counts,
            "passRate": (counts["pass"] / compared) if compared else None,
            "passRateDenominator": compared,
            "byFamily": by_family,
            "tolerancePolicy": (
                "有容差行：|computed - reference| <= tolerance 判 pass；"
                "无容差行：只报告偏差，标记 no_tolerance，不计入一致率。"
            ),
            "noSelfInventedThresholds": True,
        }

        report = {
            "status": "compared",
            "validated": False,
            "selfAttestation": True,
            "note": (
                "PyRadiomics 是 IBSI 的参与软件之一，本结果是参与软件对照官方参考值，"
                "不是独立第三方验证；对外只能表述为 IBSI 2D 子集的一致性检查。"
            ),
            "aggregationCaveat": (
                "recordAggregation=per_slice_mean 仅在 aggregation=2D 时有效：IBSI 的 "
                "2D, averaged 与 2D, slice-merged 需要矩阵级聚合，"
                "逐层特征均值不能重建这两个口径。"
            ),
            "mappedFeatures": sum(
                1 for row in rows if row["conclusion"] != "not_comparable"
            ),
            "uncomparableFeatures": sorted(
                {row["ibsiCode"] for row in rows if row["conclusion"] == "not_comparable"}
            ),
            "missingFeatureKeys": sorted(
                {row["ibsiCode"] for row in rows if row["conclusion"] == "feature_missing"}
            ),
        }

        return {
            "comparison": ctx.output_json(
                "comparison.json", {"columns": columns, "records": rows}, "table"
            ),
            "summary": ctx.output_json("summary.json", summary, "metrics"),
            "report": ctx.output_json("report.json", report, "report"),
        }
