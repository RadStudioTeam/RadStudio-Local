"""IBSI 2D 数字体模自检：逐层特征提取 + 参考值偏差比对。

Task 1.3（论文复现计划）：按
``ReadImages → ImageFormatConversion → Preprocessing2D → IBSI2DFeatureExtraction
→ ReadTable（参考值 / 映射）→ IBSIReferenceComparison`` 跑通数字体模。

本轮口径（用户已确认）：只跑数字体模、只产出逐特征偏差与按族汇总，不产出
pass/fail 一致率——IBSI 手册数字体模 136 行参考值没有公布容差。算子内置容差
规则，因此运行结果里不应出现 pass/fail。

局部执行说明：本轮用本地 Python SDK 运行同一套算子实现（与 P4 一致），不经过
容器 worker，所以 run-summary 记录 Python / PyRadiomics / git 版本而非镜像摘要。

用法：
    python scripts/run_ibsi_phantom_selfcheck.py
    # 干净环境重跑：输出到独立目录，避免覆盖正式运行产物
    python scripts/run_ibsi_phantom_selfcheck.py \
        --run-id p1-ibsi-clean-rerun --run-dir /tmp/p1-ibsi-clean
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python-sdk"))

import radstudio.operators_builtin  # noqa: E402,F401  注册内置算子
import radstudio.operators_ibsi  # noqa: E402,F401  注册 IBSI 比对算子
import radstudio.operators_readers  # noqa: E402,F401  注册读取算子
from radstudio.operator import ArtifactStore, OperatorRequest, invoke  # noqa: E402

DEFAULT_RUN_ID = "p1-ibsi-phantom-selfcheck"
DEFAULT_RUN_DIR = (
    ROOT / "runs" / DEFAULT_RUN_ID
)
# 由 main() 按命令行参数覆盖。
RUN_ID = DEFAULT_RUN_ID
RUN_DIR = DEFAULT_RUN_DIR
WORKSPACE = RUN_DIR / ".workspace"
COMPLIANCE = ROOT / "packages" / "compliance" / "ibsi-2d"
SLICES = COMPLIANCE / "slices"

DATASET = "dig. phantom"
AGGREGATION = "2D"
RECORD_AGGREGATION = "per_slice_mean"
IMAGE_TYPE = "original"
BIN_WIDTH = 1
FAMILIES = ["glszm", "gldm", "ngtdm"]

OPERATOR_VERSIONS = {
    "ReadImages": "1.0.0",
    "ImageFormatConversion": "1.1.0",
    "Preprocessing2D": "1.1.0",
    "IBSI2DFeatureExtraction": "1.0.0",
    "ReadTable": "1.0.0",
    "IBSIReferenceComparison": "1.0.0",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def images_manifest() -> dict:
    index = json.loads((SLICES / "index.json").read_text(encoding="utf-8"))
    files: dict[str, dict] = {}
    rows = []
    for entry in index["slices"]:
        z = entry["sliceIndex"]
        image_rel = entry["image"]["path"]
        mask_rel = entry["mask"]["path"]
        image_path = ROOT / image_rel
        mask_path = ROOT / mask_rel
        if sha256(image_path) != entry["image"]["sha256"]:
            raise SystemExit(f"第 {z} 层图像哈希与 slices/index.json 不符")
        if sha256(mask_path) != entry["mask"]["sha256"]:
            raise SystemExit(f"第 {z} 层掩膜哈希与 slices/index.json 不符")
        files[f"image-z{z}"] = {
            "path": str(image_path),
            "sha256": entry["image"]["sha256"],
            "sizeBytes": image_path.stat().st_size,
        }
        files[f"mask-z{z}"] = {
            "path": str(mask_path),
            "sha256": entry["mask"]["sha256"],
            "sizeBytes": mask_path.stat().st_size,
        }
        rows.append(
            {
                "rowId": f"phantom-slice-{z}",
                "assets": [
                    {"role": "image", "fileId": f"image-z{z}"},
                    {"role": "roi", "fileId": f"mask-z{z}"},
                ],
            }
        )
    return {
        "schemaVersion": "1",
        "config": {"imageRole": "image", "roiRole": "roi", "requireRoi": True},
        "files": files,
        "rows": rows,
    }


def table_manifest(path: Path, row_id: str) -> dict:
    return {
        "schemaVersion": "1",
        "config": {},
        "files": {
            "table-file": {
                "path": str(path),
                "sha256": sha256(path),
                "sizeBytes": path.stat().st_size,
            }
        },
        "rows": [
            {
                "rowId": row_id,
                "assets": [{"role": "table", "fileId": "table-file"}],
            }
        ],
    }


def main() -> None:
    global RUN_ID, RUN_DIR, WORKSPACE
    parser = argparse.ArgumentParser(description="IBSI 2D 数字体模自检")
    parser.add_argument(
        "--run-id",
        default=DEFAULT_RUN_ID,
        help=f"运行 ID（默认 {DEFAULT_RUN_ID}）",
    )
    parser.add_argument(
        "--run-dir",
        default="",
        help="产物目录；省略时写入 runs/<run-id>",
    )
    args = parser.parse_args()
    RUN_ID = args.run_id
    RUN_DIR = Path(args.run_dir).resolve() if args.run_dir else DEFAULT_RUN_DIR
    WORKSPACE = RUN_DIR / ".workspace"

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(WORKSPACE)

    reference_csv = COMPLIANCE / "reference-values.csv"
    mapping_csv = COMPLIANCE / "feature-mapping.csv"
    for path in (reference_csv, mapping_csv, SLICES / "index.json"):
        if not path.is_file():
            raise SystemExit(f"缺少输入文件：{path}")

    nodes: list[dict] = []

    def run_node(
        node_id: str,
        operator_type: str,
        parameters: dict,
        inputs: dict | None = None,
        environment: dict | None = None,
    ) -> dict:
        request = OperatorRequest(
            requestId=f"{RUN_ID}:{node_id}",
            runId=RUN_ID,
            nodeId=node_id,
            operatorType=operator_type,
            operatorVersion=OPERATOR_VERSIONS[operator_type],
            parameters=parameters,
            inputs=inputs or {},
            environment=environment,
        )
        started = time.perf_counter()
        result = invoke(request, store)
        duration_ms = round((time.perf_counter() - started) * 1000)
        record = {
            "nodeId": node_id,
            "operatorType": operator_type,
            "operatorVersion": request.operatorVersion,
            "parameters": parameters,
            "status": result.status,
            "durationMs": duration_ms,
            "outputs": result.outputs,
            "metrics": result.metrics,
        }
        nodes.append(record)
        if result.status != "completed":
            raise SystemExit(
                f"{node_id} 执行失败：{json.dumps(result.error.to_dict() if result.error else {}, ensure_ascii=False)}"
            )
        print(f"{node_id}: {result.status} ({duration_ms} ms)")
        return result.outputs

    # 1. 读取逐层图像与掩膜
    read_outputs = run_node(
        "01-read-images", "ReadImages", {}, environment={"inputManifest": images_manifest()}
    )
    samples_ref = read_outputs["samples"]
    samples = store.read_json(samples_ref)
    # 2. 灰度 / 二值口径（PNG 已是 L 模式，本步只做角色校验并补齐契约）
    conversion_outputs = run_node(
        "02-format-conversion",
        "ImageFormatConversion",
        {"imageMode": "grayscale", "roiMode": "binary"},
        {"samples": samples_ref},
    )
    converted_samples = store.read_json(conversion_outputs["samples"])

    # 3. 固定宽度离散化（PyRadiomics 引擎会忽略预离散化，仅保持链路一致）
    preprocessing_outputs = run_node(
        "03-preprocessing2d",
        "Preprocessing2D",
        {"binWidth": BIN_WIDTH},
        {"samples": conversion_outputs["samples"]},
    )
    preprocessed_samples = store.read_json(preprocessing_outputs["samples"])

    # 4. 逐层特征提取
    feature_outputs = run_node(
        "04-ibsi-features",
        "IBSI2DFeatureExtraction",
        {
            "engine": "pyradiomics",
            "featureFamilies": FAMILIES,
            "imageTypes": [IMAGE_TYPE],
            "binWidth": BIN_WIDTH,
        },
        {"samples": preprocessing_outputs["samples"]},
    )
    feature_table = store.read_json(feature_outputs["features"])

    # 5./6. 参考值与映射表
    reference_outputs = run_node(
        "05-read-reference",
        "ReadTable",
        {},
        environment={"inputManifest": table_manifest(reference_csv, "ibsi-reference-values")},
    )
    # 本地单工作区下两个 ReadTable 节点都写 table.json，后一个会覆盖前一个的
    # 同名文件；平台按节点隔离工作区不会冲突。先把参考表复制成独立文件名，
    # 保持内容与产物哈希不变，再交给比对节点。
    reference_table = store.read_json(reference_outputs["records"])
    reference_ref = store.write_json(
        "reference-values-table.json",
        reference_table,
        "table",
    )
    mapping_outputs = run_node(
        "06-read-mapping",
        "ReadTable",
        {},
        environment={"inputManifest": table_manifest(mapping_csv, "ibsi-feature-mapping")},
    )
    mapping_table = store.read_json(mapping_outputs["records"])

    # 7. 参考值比对（逐层均值）
    comparison_outputs = run_node(
        "07-ibsi-comparison",
        "IBSIReferenceComparison",
        {
            "dataset": DATASET,
            "aggregation": AGGREGATION,
            "recordAggregation": RECORD_AGGREGATION,
            "imageType": IMAGE_TYPE,
        },
        {
            "features": feature_outputs["features"],
            "reference": reference_ref,
            "mapping": mapping_outputs["records"],
        },
    )

    comparison = store.read_json(comparison_outputs["comparison"])
    summary = store.read_json(comparison_outputs["summary"])
    report = store.read_json(comparison_outputs["report"])

    # -- 落盘：特征表、逐特征比对表、汇总与报告 --
    (RUN_DIR / "01-read-images_samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (RUN_DIR / "02-format-conversion_samples.json").write_text(
        json.dumps(converted_samples, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (RUN_DIR / "03-preprocessing2d_samples.json").write_text(
        json.dumps(preprocessed_samples, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (RUN_DIR / "04-ibsi-features_features.json").write_text(
        json.dumps(feature_table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (RUN_DIR / "05-reference-table.json").write_text(
        json.dumps(reference_table, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (RUN_DIR / "06-mapping-table.json").write_text(
        json.dumps(mapping_table, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    rows = comparison["records"]
    columns = comparison["columns"]
    with (RUN_DIR / "07-comparison.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in columns})

    (RUN_DIR / "07-comparison_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (RUN_DIR / "07-comparison_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    family_deviation: dict[str, dict] = {}
    not_comparable_reasons: dict[str, int] = {}
    mapping_index = {str(row.get("code") or ""): row for row in mapping_table["records"]}
    for row in rows:
        family = str(row.get("family") or "")
        bucket = family_deviation.setdefault(
            family, {"comparable": 0, "notComparable": 0, "relative": [], "absolute": []}
        )
        if row.get("conclusion") in ("pass", "fail", "no_tolerance"):
            bucket["comparable"] += 1
            if row.get("relativeDeviation") is not None:
                bucket["relative"].append(float(row["relativeDeviation"]))
            if row.get("absoluteDeviation") is not None:
                bucket["absolute"].append(float(row["absoluteDeviation"]))
        else:
            bucket["notComparable"] += 1
            note = str((mapping_index.get(str(row.get("ibsiCode"))) or {}).get("note") or "")
            key = note or str(row.get("note") or "未分类")
            not_comparable_reasons[key] = not_comparable_reasons.get(key, 0) + 1

    def _stats(values: list[float]) -> dict:
        if not values:
            return {"mean": None, "max": None}
        return {"mean": sum(values) / len(values), "max": max(values)}

    by_family_deviation = {
        family: {
            "comparableRows": bucket["comparable"],
            "notComparableRows": bucket["notComparable"],
            "meanRelativeDeviation": _stats(bucket["relative"])["mean"],
            "maxRelativeDeviation": _stats(bucket["relative"])["max"],
            "meanAbsoluteDeviation": _stats(bucket["absolute"])["mean"],
            "maxAbsoluteDeviation": _stats(bucket["absolute"])["max"],
        }
        for family, bucket in family_deviation.items()
    }
    with (RUN_DIR / "07-comparison_by-family.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        columns = [
            "family",
            "comparableRows",
            "notComparableRows",
            "meanRelativeDeviation",
            "maxRelativeDeviation",
            "meanAbsoluteDeviation",
            "maxAbsoluteDeviation",
        ]
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for family, stats in by_family_deviation.items():
            writer.writerow({"family": family, **stats})
    (RUN_DIR / "07-comparison_reasons.json").write_text(
        json.dumps(not_comparable_reasons, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    counts = summary.get("conclusionCounts") or {}
    no_tolerance = int(counts.get("no_tolerance") or 0)
    not_comparable = int(counts.get("not_comparable") or 0)
    pass_count = int(counts.get("pass") or 0)
    fail_count = int(counts.get("fail") or 0)
    if pass_count or fail_count:
        raise SystemExit(
            "数字体模参考值没有容差，不应出现 pass/fail；请检查参考表 tolerance 列"
        )
    if no_tolerance + not_comparable != len(rows):
        raise SystemExit(
            f"结论计数与逐特征行数不一致：{no_tolerance}+{not_comparable} != {len(rows)}"
        )
    if not summary.get("noSelfInventedThresholds"):
        raise SystemExit("summary 未声明 noSelfInventedThresholds")

    pyradiomics_version = ""
    try:
        import radiomics

        pyradiomics_version = str(getattr(radiomics, "__version__", ""))
    except ImportError:
        pyradiomics_version = "unavailable"

    run_summary = {
        "runId": RUN_ID,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "spec": "reproductions/p1-ibsi-2d",
        "tasks": ["1.1.3", "1.2.3", "1.2.4", "1.3.1", "1.3.2", "1.3.3", "1.4.1"],
        "executionEnvironment": {
            "kind": "local-python-sdk",
            "note": (
                "本轮通过本地 Python SDK 运行与平台相同的算子实现；未使用容器 worker，"
                "因此没有镜像摘要。RadStudio 平台运行留待 worker 镜像包含新算子后再做。"
            ),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "gitCommit": git("rev-parse", "HEAD"),
            "gitDirty": bool(git("status", "--porcelain")),
        },
        "pyradiomics": {
            "version": pyradiomics_version,
            "commit": "8ed579383b44806651c463d5e691f3b2b57522ab",
            "requirement": (
                "pyradiomics @ https://github.com/AIM-Harvard/pyradiomics/archive/"
                "8ed579383b44806651c463d5e691f3b2b57522ab.tar.gz"
            ),
        },
        "operatorVersions": OPERATOR_VERSIONS,
        "parameters": {
            "dataset": DATASET,
            "aggregation": AGGREGATION,
            "recordAggregation": RECORD_AGGREGATION,
            "imageType": IMAGE_TYPE,
            "binWidth": BIN_WIDTH,
            "featureFamilies": FAMILIES,
            "randomSeed": None,
        },
        "inputs": {
            "phantom": {
                "path": "packages/compliance/ibsi-2d/phantom.nii.gz",
                "sha256": sha256(COMPLIANCE / "phantom.nii.gz"),
            },
            "mask": {
                "path": "packages/compliance/ibsi-2d/mask.nii.gz",
                "sha256": sha256(COMPLIANCE / "mask.nii.gz"),
            },
            "slicesIndex": {
                "path": "packages/compliance/ibsi-2d/slices/index.json",
                "sha256": sha256(SLICES / "index.json"),
            },
            "referenceValues": {
                "path": "packages/compliance/ibsi-2d/reference-values.csv",
                "sha256": sha256(reference_csv),
            },
            "featureMapping": {
                "path": "packages/compliance/ibsi-2d/feature-mapping.csv",
                "sha256": sha256(mapping_csv),
            },
        },
        "featureTable": {
            "records": len(feature_table["records"]),
            "featureCount": len(feature_table["featureNames"]),
            "imageIds": [record.get("imageId") for record in feature_table["records"]],
        },
        "nodes": nodes,
        "outcome": {
            "referenceRowsSelected": len(rows),
            "noTolerance": no_tolerance,
            "notComparable": not_comparable,
            "pass": pass_count,
            "fail": fail_count,
            "byFamily": summary.get("byFamily"),
            "byFamilyDeviation": by_family_deviation,
            "notComparableReasons": not_comparable_reasons,
        },
        "limitations": [
            "数字体模参考值没有公布容差，本轮只报告偏差；不计算一致率、不判定 pass/fail。",
            "只覆盖 2D 数字体模；GLDZM 整族、4 个 PyRadiomics 弃用方法与 NGLDM 能量特征无法比对。",
            "GLCM / GLRLM 在数字体模只有 2D, averaged 与 2D, slice-merged 参考值，需要矩阵级跨层聚合，本轮未计算。",
            "PyRadiomics 是 IBSI 参与软件之一，本结果只用于链路一致性检查，不作为独立第三方验证。",
        ],
    }
    (RUN_DIR / "run-summary.json").write_text(
        json.dumps(run_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"比对完成：{len(rows)} 行，no_tolerance={no_tolerance}，"
        f"not_comparable={not_comparable}，pass={pass_count}，fail={fail_count}"
    )
    print(f"产物目录：{RUN_DIR}")


if __name__ == "__main__":
    main()
