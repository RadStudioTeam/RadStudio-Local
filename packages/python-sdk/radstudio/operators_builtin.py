"""内置算子实现：单体 runner 与 serve 模式共用。

11 个算子从 runner.execute() 的分支逻辑抽取为 ``@operator`` 注册类，
输入输出统一为产物引用字符串（``file:<相对路径>#sha256=<hex>;type=<kind>``）。
算法函数（make_split/train/evaluate 等）复用 radstudio.runner 的既有实现，
保证单体回放、复现包与云函数派发产出完全一致的结果。

调用路径：
- 单体 runner：runner.execute 经 _RunnerOperatorContext 桥接调用；
- serve 模式：radstudio.operator_serve 经 operator.invoke 直接调用。

上下文差异：
- DataInput 依赖 ``environment["dataset"]`` 携带数据集元数据（serve 模式由
  执行器注入，见 Task 7 的 worker 编排）；
- Report 的复现包依赖原始数据集文件与 run 级 manifest，仅在单体 runner
  上下文可用（ctx.reproduction_bundle），serve 模式下明确报错。
"""

from __future__ import annotations

import copy
import html
import io
import json
import time

import joblib
import numpy as np
from PIL import Image
from scipy.ndimage import binary_erosion

from .execution_contract import execution_parameters
from .features import FeatureConfig, FeatureExtractor, FeatureFamily
from .operator import Operator, operator, parse_ref
from .operators_tabular_base import TabularClassifierBase
from .runner import (
    apply_pipelines,
    evaluate,
    feature_workbook,
    identity,
    make_holdout,
    make_split,
    read_image,
)


def _artifact_local_path(ctx, ref):
    """把产物引用或相对路径解析为工作区本地文件（进程池需要真实路径）。"""
    try:
        return str(ctx.store.resolve_path(parse_ref(ref)["path"]))
    except ValueError:
        return str(ctx.store.resolve_path(ref))


def _load_ndim(path):
    """加载数据集影像为数组（npy 或 PIL 支持的格式），不限制维度。"""
    if path.suffix.lower() == ".npy":
        return np.load(path, allow_pickle=False)
    with Image.open(path) as image:
        return np.asarray(image)


@operator("ImageFormatConversion", version="1.0.0")
class ImageFormatConversionOperator(Operator):
    """影像格式转换：彩色转二维灰度、ROI 转二维二值掩膜。"""

    def run(self, ctx):
        p = ctx.parameters
        samples = copy.deepcopy(ctx.input_json("samples"))

        def write_gray(value, stem):
            """浮点存 npy，其余存 PNG（布尔转 0/255）；返回写入后的本地路径。"""
            if np.issubdtype(value.dtype, np.floating):
                buffer = io.BytesIO()
                np.save(buffer, value.astype(np.float32))
                return ctx.store.write_local(f"{stem}.npy", buffer.getvalue())
            if value.dtype == np.bool_:
                value = value.astype(np.uint8) * 255
            buffer = io.BytesIO()
            Image.fromarray(value).save(buffer, format="PNG")
            return ctx.store.write_local(f"{stem}.png", buffer.getvalue())

        for i, s in enumerate(samples):
            if p["imageMode"] == "grayscale":
                source = ctx.store.resolve_path(s["imagePath"])
                if source.suffix.lower() != ".npy":
                    with Image.open(source) as image:
                        if image.mode != "L":
                            gray = np.asarray(image.convert("L"))
                            s["imagePath"] = write_gray(gray, f"{i}_image")
            if p["roiMode"] == "binary" and s.get("roiPath"):
                source = ctx.store.resolve_path(s["roiPath"])
                mask = _load_ndim(source)
                if mask.ndim != 2:
                    mask = np.asarray(Image.open(source).convert("L"))
                    s["roiPath"] = write_gray((mask > 0).astype("uint8") * 255, f"{i}_roi")
        return {"samples": ctx.output_json("samples.json", samples, "sample_collection")}


class _SampleValidationOperator(Operator):
    """质控/ROI 校验共享实现：逐样本校验影像与掩膜对齐、标签与非空。"""

    # ROIValidation 语义：遇错即失败，且可生成叠加预览图
    fail_fast = False
    preview_overlay = False

    def run(self, ctx):
        p = ctx.parameters
        passed = []
        issues = []
        for i, s in enumerate(ctx.input_json("samples")):
            try:
                a = read_image(ctx.store.resolve_path(s["imagePath"]))
                mask = read_image(ctx.store.resolve_path(s["roiPath"]))
                if (
                    a.shape != mask.shape
                    or not np.any(mask > 0)
                    or not np.isfinite(a).all()
                    or not np.isfinite(mask).all()
                ):
                    raise ValueError("Invalid ROI alignment, empty ROI, or nonfinite image")
                if (
                    s["label"] is None
                    or not np.any(a)
                    or s.get("qcStatus") in ("blocked", "excluded")
                ):
                    raise ValueError("Missing binary label, empty image, or failed QC")
                passed.append(s)
                if self.preview_overlay and p["previewOverlay"]:
                    normalized = np.clip(
                        (a - a.min()) / max(float(np.ptp(a)), 1e-12) * 255, 0, 255
                    ).astype("uint8")
                    rgb = np.repeat(normalized[:, :, None], 3, axis=2)
                    boundary = (mask > 0) & ~binary_erosion(mask > 0)
                    rgb[boundary] = [255, 40, 40]
                    buffer = io.BytesIO()
                    Image.fromarray(rgb).save(buffer, format="PNG")
                    ctx.output_bytes(f"overlay_{i}.png", buffer.getvalue(), "image")
            except (ValueError, OSError) as exc:
                issues.append(
                    {"imageId": s["imageId"], "patientId": s["patientId"], "reason": str(exc)}
                )
                if self.fail_fast or not p["excludeFailed"]:
                    raise ValueError(f"Sample {s['imageId']}: {exc}") from exc
        if not passed:
            raise ValueError("No samples pass quality checks")
        return {
            "samples": ctx.output_json("samples.json", passed, "sample_collection"),
            "report": ctx.output_json(
                "report.json",
                {"passed": len(passed), "excluded": issues, "identityPreserved": True},
                "report",
            ),
        }


@operator("QualityControl", version="1.0.0")
class QualityControlOperator(_SampleValidationOperator):
    """数据质控：可选剔除失败样本。"""


@operator("ROIValidation", version="1.0.0")
class ROIValidationOperator(_SampleValidationOperator):
    """ROI 校验：遇错即失败，并可生成叠加预览图。"""

    fail_fast = True
    preview_overlay = True


@operator("Preprocessing2D", version="1.0.0")
class Preprocessing2DOperator(Operator):
    """2D 预处理：固定宽度离散化（ROI 最小值为锚点，ROI 外为零）。"""

    def run(self, ctx):
        p = ctx.parameters
        samples = copy.deepcopy(ctx.input_json("samples"))
        for i, s in enumerate(samples):
            a = read_image(ctx.store.resolve_path(s["imagePath"]))
            mask = read_image(ctx.store.resolve_path(s["roiPath"])) > 0
            if a.shape != mask.shape or not mask.any() or not np.isfinite(a).all():
                raise ValueError("Invalid preprocessing input")
            bins = np.zeros(a.shape, dtype=np.int32)
            bins[mask] = np.floor((a[mask] - a[mask].min()) / p["binWidth"]).astype(np.int32) + 1
            buffer = io.BytesIO()
            np.savez_compressed(buffer, image=a, mask=mask, discretized=bins)
            s["preprocessed"] = ctx.output_bytes(f"sample_{i}.npz", buffer.getvalue(), "array")
        return {"samples": ctx.output_json("samples.json", samples, "sample_collection")}


def _extract_preprocessed_sample(job):
    """进程池任务：读取单样本 NPZ 并计算特征（参数均可 pickle）。

    PyRadiomics 用原始图像自行离散化，忽略 Preprocessing2D 的分箱结果；
    custom 引擎使用预离散化数组，保证与单体路径一致。
    """
    path, engine, families, image_types, bin_width = job
    with np.load(path, allow_pickle=False) as z:
        image, mask = z["image"], z["mask"]
        discretized = z["discretized"] if engine == "custom" else None
    if engine == "pyradiomics":
        from .pyradiomics_backend import extract as pyradiomics_extract

        return pyradiomics_extract(image, mask, families, image_types, bin_width)
    extractor = FeatureExtractor(
        FeatureConfig(enabled_families=[FeatureFamily(f) for f in families])
    )
    return extractor.extract(image, mask, discretized).to_dict()


class _FeatureCheckpoint:
    """特征提取断点：经预签名 URL 读写已完成样本，重试只补未完成部分。

    断点写入是旁路能力——网络错误一律忽略，继续本地计算；worker 在节点
    成功后删除断点对象，失败/超时重试时再用。
    """

    def __init__(self, get_url: str = "", put_url: str = ""):
        self.get_url = get_url or ""
        self.put_url = put_url or ""

    def load(self) -> dict:
        if not self.get_url:
            return {}
        try:
            import httpx

            resp = httpx.get(self.get_url, timeout=30.0, follow_redirects=True)
            if resp.status_code != 200:
                return {}
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 - 断点读取失败按无断点处理
            return {}

    def save(self, names, records) -> None:
        if not self.put_url:
            return
        try:
            import httpx

            httpx.put(
                self.put_url,
                json={"featureNames": names, "records": records},
                timeout=120.0,
            )
        except Exception as exc:  # noqa: BLE001 - 断点写入失败不影响执行
            print(f"feature checkpoint save failed: {exc}")


def _feature_outputs(ctx, names, records, engine, families, image_types, extra_report=None):
    ctx.output_bytes("features_xlsx.xlsx", feature_workbook(names, records), "feature_table_xlsx")
    report = {
        "status": "not_reference_validated",
        "validated": False,
        "engine": engine,
        "families": families,
        "imageTypes": image_types,
        "note": (
            "Extracted with PyRadiomics (GitHub commit 8ed57938); feature names "
            "follow the PyRadiomics convention. Pixel units; no spatial resampling; "
            "no IBSI reference baseline was supplied."
            if engine == "pyradiomics"
            else "No IBSI reference baseline was supplied. Pixel units; no spatial resampling."
        ),
    }
    if extra_report:
        report.update(extra_report)
    return {
        "features": ctx.output_json(
            "features.json", {"featureNames": names, "records": records}, "feature_table"
        ),
        "report": ctx.output_json("report.json", report, "report"),
    }


@operator("IBSI2DFeatureExtraction", version="1.0.0")
class IBSI2DFeatureExtractionOperator(Operator):
    """2D 特征提取：custom 引擎或 PyRadiomics，输出特征表与报告。

    长节点优化（参数由 worker 从管理后台配置注入，节点参数优先）：
    - workers：单分片进程数（ProcessPoolExecutor，spawn 上下文）；
    - checkpointEvery：每完成 N 个样本把记录写入预签名断点对象；
    - progressSeconds：进度上报节流间隔，经 node.progress 到状态对象；
    - mergeParts：worker 分片扇出后的合并调用（输入 part0..partN）。

    features_xlsx 为非端口产物（operators.json 契约未暴露），仅供前端下载。
    """

    def run(self, ctx):
        p = execution_parameters(ctx)
        merge_parts = int(p.get("mergeParts") or 0)
        engine = p.get("engine", "custom")
        families = list(p["featureFamilies"])
        image_types = list(p.get("imageTypes", ["original"]))
        if merge_parts:
            return self._merge(ctx, merge_parts, engine, families, image_types)
        bin_width = p.get("binWidth", 25)
        # worker 执行期由管理后台配置注入 workers；直接调用（单体/测试）默认单进程。
        workers = max(1, int(p.get("workers") or 1))
        checkpoint_every = max(1, int(p.get("checkpointEvery") or 25))
        progress_seconds = max(1.0, float(p.get("progressSeconds") or 30))
        samples = ctx.input_json("samples")
        total = len(samples)
        for s in samples:
            if "preprocessed" not in s:
                raise ValueError("Preprocessed sample required")

        transfer = ctx.request.transfer or {}
        checkpoint = _FeatureCheckpoint(
            transfer.get("checkpointGetUrl", ""), transfer.get("checkpointPutUrl", "")
        )
        saved = checkpoint.load()
        names = saved.get("featureNames") or None
        raw_records = {}
        for entry in saved.get("records") or []:
            try:
                raw_records[int(entry["index"])] = entry["record"]
            except (KeyError, TypeError, ValueError):
                continue
        if raw_records:
            print(f"resuming feature extraction from checkpoint: {len(raw_records)}/{total}")

        def record_for(sample, values):
            nonlocal names
            if names is None:
                names = sorted(values)
            if sorted(values) != names:
                raise ValueError("Feature schema mismatch")
            return {
                "imageId": sample["imageId"],
                "patientId": sample["patientId"],
                "label": sample["label"],
                "values": [float(values[k]) if np.isfinite(values[k]) else None for k in names],
            }

        pending = [(i, s) for i, s in enumerate(samples) if i not in raw_records]
        done = len(raw_records)
        last_report = 0.0
        started = time.monotonic()

        def report(force=False):
            nonlocal last_report
            now = time.monotonic()
            if not force and now - last_report < progress_seconds:
                return
            last_report = now
            elapsed = max(now - started, 1e-6)
            rate = done / elapsed if done else 0.0
            remaining = (total - done) / rate if rate > 0 else 0.0
            percent = 100.0 * done / total if total else 100.0
            ctx.progress(
                percent,
                f"{done}/{total} samples" + (f", ~{remaining / 60:.0f} min left" if rate else ""),
            )

        report(force=True)

        def record_done(index, values):
            nonlocal done
            raw_records[index] = record_for(samples[index], values)
            done += 1
            if done % checkpoint_every == 0 or done == total:
                checkpoint.save(
                    names,
                    [{"index": i, "record": raw_records[i]} for i in sorted(raw_records)],
                )
            ctx.check_cancelled()
            report()

        if workers <= 1 or len(pending) <= 1:
            for index, sample in pending:
                ctx.check_cancelled()
                values = _extract_preprocessed_sample(
                    (
                        _artifact_local_path(ctx, sample["preprocessed"]),
                        engine,
                        families,
                        image_types,
                        bin_width,
                    )
                )
                record_done(index, values)
        else:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor, as_completed

            jobs = {
                index: (
                    _artifact_local_path(ctx, sample["preprocessed"]),
                    engine,
                    families,
                    image_types,
                    bin_width,
                )
                for index, sample in pending
            }
            pool = ProcessPoolExecutor(
                max_workers=min(workers, len(jobs)),
                mp_context=multiprocessing.get_context("spawn"),
            )
            try:
                futures = {
                    pool.submit(_extract_preprocessed_sample, job): i for i, job in jobs.items()
                }
                for future in as_completed(futures):
                    record_done(futures[future], future.result())
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                pool.shutdown(wait=True)

        records = [raw_records[i] for i in range(total)]
        return _feature_outputs(
            ctx,
            names or [],
            records,
            engine,
            families,
            image_types,
            {"shards": int(p.get("shardCount") or 1)},
        )

    def _merge(self, ctx, parts, engine, families, image_types):
        names = None
        records = []
        for i in range(parts):
            part = ctx.input_json(f"part{i}")
            part_names = list(part.get("featureNames") or [])
            if names is None:
                names = part_names
            elif part_names != names:
                raise ValueError("Feature schema mismatch")
            records.extend(part.get("records") or [])
        return _feature_outputs(
            ctx, names or [], records, engine, families, image_types, {"shards": parts}
        )


@operator("FeatureFilter", version="1.0.0")
class FeatureFilterOperator(Operator):
    """折内特征筛选：将筛选策略参数固化为产物，供训练节点消费。"""

    def run(self, ctx):
        return {"strategy": ctx.output_json("strategy.json", ctx.parameters, "filter_strategy")}


@operator("FeatureFilter", version="1.1.0")
class FeatureFilterOperatorV11(FeatureFilterOperator):
    """折内特征筛选：支持方差/相关性、单变量和 LASSO 策略。"""


@operator("DataSplit", version="1.0.0")
class DataSplitOperator(Operator):
    """分组交叉验证：患者分组五折划分（seed 42）。"""

    def run(self, ctx):
        return {
            "split": ctx.output_json(
                "split.json", make_split(ctx.input_json("features")), "split_plan"
            )
        }


@operator("DataSplit", version="1.1.0")
class DataSplitOperatorV11(Operator):
    """分组交叉验证：折数与随机种子可配置。"""

    def run(self, ctx):
        return {
            "split": ctx.output_json(
                "split.json",
                make_split(
                    ctx.input_json("features"),
                    folds=ctx.parameters["folds"],
                    random_seed=ctx.parameters["randomSeed"],
                ),
                "split_plan",
            )
        }


@operator("HoldoutSplit", version="1.0.0")
class HoldoutSplitOperator(Operator):
    """患者级训练/验证/测试划分，输出三张特征表。"""

    def run(self, ctx):
        result = make_holdout(
            ctx.input_json("features"),
            validation_ratio=ctx.parameters["validationRatio"],
            test_ratio=ctx.parameters["testRatio"],
            random_seed=ctx.parameters["randomSeed"],
            stratify=ctx.parameters["stratify"],
        )
        outputs = {
            "split": ctx.output_json("split.json", result["split"], "split_plan"),
        }
        for name in ("train", "validation", "test"):
            outputs[name] = ctx.output_json(f"{name}-features.json", result[name], "feature_table")
        return outputs


@operator("ModelApply", version="1.0.0")
class ModelApplyOperator(Operator):
    """把折集成模型应用到外部特征表，输出 external 预测。"""

    def run(self, ctx):
        model = ctx.input_json("model")
        pipeline_ref = model.get("pipelineArtifact")
        if not pipeline_ref:
            raise ValueError("Input model lacks pipelineArtifact (not a tabular model)")
        pipelines = joblib.load(io.BytesIO(ctx.store.read_bytes(pipeline_ref)))
        predictions = apply_pipelines(ctx.input_json("features"), model, pipelines)
        return {"predictions": ctx.output_json("predictions.json", predictions, "prediction_table")}


@operator("LogisticRegression", version="1.0.0")
class LogisticRegressionOperator(TabularClassifierBase):
    """逻辑回归：折内训练五折集成，输出 OOF 预测与模型。"""
    # 子类保持 estimator_factory=None、estimator_kind=None，train() 走默认 LR。

    def run(self, ctx):
        return self._run_v100(ctx)


@operator("Evaluation", version="1.0.0")
class EvaluationOperator(Operator):
    """OOF 模型评估：指标、ROC 与混淆矩阵。"""

    def run(self, ctx):
        result = evaluate(ctx.input_json("predictions"), ctx.parameters["threshold"])
        return {
            key: ctx.output_json(
                f"{key}.json", result[key], "metrics" if key == "metrics" else "chart"
            )
            for key in ("metrics", "roc", "confusion")
        }


@operator("Evaluation", version="1.1.0")
class EvaluationOperatorV11(Operator):
    """OOF/外部验证评估：置信区间、校准、决策曲线与 PR 曲线。"""

    def run(self, ctx):
        p = ctx.parameters
        result = evaluate(
            ctx.input_json("predictions"),
            threshold=p["threshold"],
            threshold_mode=p.get("thresholdMode", "fixed"),
            bootstrap=p.get("bootstrap", 2000),
            ci_level=p.get("ciLevel", 0.95),
            calibration_bins=p.get("calibrationBins", 10),
            seed=p.get("seed", 42),
        )
        return {
            key: ctx.output_json(f"{key}.json", value, "metrics" if key == "metrics" else "chart")
            for key, value in result.items()
        }


@operator("Report", version="1.0.0")
class ReportOperator(Operator):
    """报告与复现包：汇总指标生成 HTML 报告；复现包仅单体 runner 上下文可用。"""

    def run(self, ctx):
        data = {
            key: ctx.input_json(key)
            for key in ("metrics", "model", "report", "roc", "confusion")
            if key in ctx.inputs
        }
        body = '<!doctype html><html lang="en"><meta charset="utf-8"><title>RadStudio OOF report</title><body><h1>Patient-grouped five-fold evaluation</h1><p>Seed 42. Out-of-fold predictions. Not external validation. Feature reference validation not performed.</p>'
        for key in ["metrics", "model", "report"]:
            if key in data:
                body += (
                    "<h2>"
                    + html.escape(key)
                    + "</h2><pre>"
                    + html.escape(json.dumps(data[key], indent=2))
                    + "</pre>"
                )
        if "roc" in data:
            points = " ".join(
                f"{40 + 400 * x:.2f},{440 - 400 * y:.2f}"
                for x, y in zip(data["roc"]["fpr"], data["roc"]["tpr"])
            )
            body += (
                '<h2>OOF ROC</h2><svg viewBox="0 0 480 480" width="480" role="img" aria-label="ROC curve"><path d="M40 40V440H440" fill="none" stroke="black"/><polyline points="'
                + points
                + '" fill="none" stroke="blue"/></svg>'
            )
        if "confusion" in data:
            body += (
                "<h2>Confusion matrix (rows true, columns predicted)</h2><pre>"
                + html.escape(json.dumps(data["confusion"]))
                + "</pre>"
            )
        outputs = {
            "report": ctx.output_bytes("report.html", (body + "</body></html>").encode(), "report")
        }
        reproduction_bundle = getattr(ctx, "reproduction_bundle", None)
        if not callable(reproduction_bundle):
            raise TypeError(
                "Report reproduction bundle requires the monolithic runner context "
                "(dataset files are unavailable in serve mode)"
            )
        outputs["reproduction"] = reproduction_bundle()
        return outputs


@operator("ValidateFeatureTable", version="1.0.0")
class ValidateFeatureTableOperator(Operator):
    """Explicit structural validation adapter; never changes scientific values."""

    def run(self, ctx):
        from .artifact_boundary import ArtifactBoundaryError
        from .content_contract import ContentContractError, validate_content_bytes

        reference = ctx.inputs["features"]
        try:
            validate_content_bytes(
                ctx.store.read_bytes(reference),
                "feature_table",
                {"id": "radstudio/feature-table", "version": "1"},
            )
        except ContentContractError as error:
            raise ArtifactBoundaryError(
                "operator.input_invalid", str(error), error.details
            ) from error
        return {"features": reference}


@operator("FeatureTableJoin", version="1.0.0")
class FeatureTableJoinOperator(Operator):
    """按稳定键合并多张特征表；显式报告保留、丢弃、缺失与重复，禁止按行号拼接。"""

    JOIN_KEYS = ("imageId",)
    JOINS = ("inner", "left")
    CONFLICTS = ("error", "prefix")
    PARAMETERS = ("joinKey", "join", "conflict")

    def run(self, ctx):
        from .artifact_boundary import ArtifactBoundaryError

        parameters = ctx.parameters or {}
        unknown = sorted(set(parameters) - set(self.PARAMETERS))
        join_key = parameters.get("joinKey", "imageId")
        join = parameters.get("join", "inner")
        conflict = parameters.get("conflict", "error")
        if unknown:
            raise ArtifactBoundaryError(
                "operator.parameters_invalid",
                "合并参数包含未声明字段：" + ", ".join(unknown),
                {"parameters": unknown},
            )
        if join_key not in self.JOIN_KEYS:
            raise ArtifactBoundaryError(
                "operator.parameters_invalid",
                "当前只支持按 imageId 这一稳定键合并，不能按行号对齐",
                {"joinKey": join_key},
            )
        if join not in self.JOINS:
            raise ArtifactBoundaryError(
                "operator.parameters_invalid",
                "连接方式必须是 inner 或 left：" + str(join),
                {"join": join},
            )
        if conflict not in self.CONFLICTS:
            raise ArtifactBoundaryError(
                "operator.parameters_invalid",
                "列名冲突策略必须是 error 或 prefix：" + str(conflict),
                {"conflict": conflict},
            )

        tables = []
        for index, data in enumerate(ctx.input_set("features"), start=1):
            try:
                value = json.loads(data)
            except (TypeError, ValueError, UnicodeError) as error:
                raise ArtifactBoundaryError(
                    "operator.input_invalid",
                    "输入 " + str(index) + " 不是受支持的特征表JSON：" + str(error),
                    {"inputIndex": index},
                ) from error
            names = value.get("featureNames") if isinstance(value, dict) else None
            records = value.get("records") if isinstance(value, dict) else None
            if (
                not isinstance(names, list)
                or not names
                or not all(isinstance(name, str) and name for name in names)
                or len(set(names)) != len(names)
                or not isinstance(records, list)
            ):
                raise ArtifactBoundaryError(
                    "operator.input_invalid",
                    "输入 " + str(index) + " 缺少唯一的 featureNames 或 records 列表",
                    {"inputIndex": index},
                )
            seen = {}
            for row in records:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("imageId"), str)
                    or not row["imageId"]
                ):
                    raise ArtifactBoundaryError(
                        "operator.input_invalid",
                        "输入 " + str(index) + " 的样本缺少 imageId",
                        {"inputIndex": index},
                    )
                values = row.get("values")
                if not isinstance(values, list) or len(values) != len(names):
                    raise ArtifactBoundaryError(
                        "operator.input_invalid",
                        "输入 " + str(index) + " 的特征值数量与 featureNames 不一致",
                        {"inputIndex": index, "imageId": row["imageId"]},
                    )
                seen[row["imageId"]] = seen.get(row["imageId"], 0) + 1
            duplicates = sum(count - 1 for count in seen.values() if count > 1)
            if duplicates:
                raise ArtifactBoundaryError(
                    "operator.input_invalid",
                    "输入 " + str(index) + " 有 " + str(duplicates) + " 行重复 imageId，无法按稳定键合并",
                    {"inputIndex": index, "duplicates": duplicates},
                )
            tables.append(value)

        all_names = set()
        for table_value in tables:
            all_names.update(table_value["featureNames"])
        owners, used, columns, renamed = {}, set(), [], []
        for index, table_value in enumerate(tables, start=1):
            for position, name in enumerate(table_value["featureNames"]):
                output_name = name
                if name in used:
                    if conflict == "error":
                        raise ArtifactBoundaryError(
                            "operator.input_invalid",
                            "列名冲突：" + name + "（输入 " + str(owners[name])
                            + " 与输入 " + str(index) + "），请先重命名或改用前缀策略",
                            {"column": name, "inputs": [owners[name], index]},
                        )
                    output_name = "t" + str(index) + "__" + name
                    if output_name in used or output_name in all_names:
                        raise ArtifactBoundaryError(
                            "operator.input_invalid",
                            "列名冲突：前缀重命名后的 " + output_name + " 仍与其他输入列重名",
                            {"column": output_name, "inputs": [owners[name], index]},
                        )
                    renamed.append({"input": index, "from": name, "to": output_name})
                else:
                    owners[name] = index
                used.add(output_name)
                columns.append({"input": index, "position": position, "name": output_name})

        by_key = [{row["imageId"]: row for row in value["records"]} for value in tables]
        anchor_keys = list(by_key[0])
        anchor = set(anchor_keys)
        if join == "inner":
            keys = [key for key in anchor_keys if all(key in rows for rows in by_key)]
        else:
            keys = anchor_keys

        def resolve(key, field, label):
            value, source = None, None
            for index, rows in enumerate(by_key, start=1):
                row = rows.get(key)
                if row is None or row.get(field) is None:
                    continue
                if value is None:
                    value, source = row[field], index
                elif value != row[field]:
                    raise ArtifactBoundaryError(
                        "operator.input_invalid",
                        "同一 imageId 的" + label + "不一致：" + key
                        + "（输入 " + str(source) + " 与输入 " + str(index) + "）",
                        {"imageId": key, "field": field, "inputs": [source, index]},
                    )
            return value

        records = []
        for key in keys:
            values = []
            for column in columns:
                row = by_key[column["input"] - 1].get(key)
                values.append(None if row is None else row["values"][column["position"]])
            records.append(
                {
                    "imageId": key,
                    "patientId": resolve(key, "patientId", "患者身份"),
                    "label": resolve(key, "label", "标签"),
                    "values": values,
                }
            )

        kept = set(keys)
        inputs = []
        for index, rows in enumerate(by_key, start=1):
            current = set(rows)
            inputs.append(
                {
                    "index": index,
                    "records": len(tables[index - 1]["records"]),
                    "uniqueKeys": len(current),
                    "columns": len(tables[index - 1]["featureNames"]),
                    "kept": len(current & kept),
                    "dropped": len(current - kept),
                    "missing": len(anchor - current),
                }
            )
        report = {
            "rules": "feature-table-join/1",
            "joinKey": join_key,
            "join": join,
            "conflict": conflict,
            "kept": len(keys),
            "dropped": sum(item["dropped"] for item in inputs),
            "missing": sum(item["missing"] for item in inputs),
            "duplicates": 0,
            "featureNames": [column["name"] for column in columns],
            "renamed": renamed,
            "inputs": inputs,
        }
        return {
            "features": ctx.output_json(
                "features.json",
                {"featureNames": report["featureNames"], "records": records},
                "feature_table",
            ),
            "report": ctx.output_json("join-report.json", report, "report"),
        }


@operator("DataSplit", version="1.2.0")
class DataSplitOperatorV12(Operator):
    def run(self, ctx):
        from .content_contract import source_identity

        table = ctx.input_json("features")
        value = make_split(
            table, folds=ctx.parameters["folds"], random_seed=ctx.parameters["randomSeed"]
        )
        value["source"] = ctx.inputs["features"]
        value["identityHash"] = source_identity(table)
        return {"split": ctx.output_json("split.json", value, "split_plan")}


@operator("HoldoutSplit", version="1.2.0")
class HoldoutSplitOperatorV12(Operator):
    def run(self, ctx):
        from .content_contract import source_identity

        table = ctx.input_json("features")
        result = make_holdout(
            table,
            validation_ratio=ctx.parameters["validationRatio"],
            test_ratio=ctx.parameters["testRatio"],
            random_seed=ctx.parameters["randomSeed"],
            stratify=ctx.parameters["stratify"],
            allow_empty_validation=True,
        )
        result["split"]["source"] = ctx.inputs["features"]
        result["split"]["identityHash"] = source_identity(table)
        outputs = {"split": ctx.output_json("split.json", result["split"], "split_plan")}
        for name in ("train", "validation", "test"):
            outputs[name] = ctx.output_json(f"{name}-features.json", result[name], "feature_table")
        return outputs


# ---------------------------------------------------------------------------
# 样本集合契约声明版本：与 1.0.0 共用实现，只在注册表里把 samples 输出绑定
# radstudio/sample-collection@1。1.0.0 行端口进语义摘要、不可就地改写，
# 因此绑定必须随新小版本交付（形态同 DataSplit 1.2.0）。
# ---------------------------------------------------------------------------


@operator("ImageFormatConversion", version="1.1.0")
class ImageFormatConversionOperatorV11(ImageFormatConversionOperator):
    """影像格式转换：绑定样本集合契约的声明版本，科学计算与 1.0.0 相同。"""


@operator("QualityControl", version="1.1.0")
class QualityControlOperatorV11(QualityControlOperator):
    """数据质控：绑定样本集合契约的声明版本；report 输出仍无契约。"""


@operator("ROIValidation", version="1.1.0")
class ROIValidationOperatorV11(ROIValidationOperator):
    """ROI 校验：绑定样本集合契约的声明版本；report 输出仍无契约。"""


@operator("Preprocessing2D", version="1.1.0")
class Preprocessing2DOperatorV11(Preprocessing2DOperator):
    """2D 预处理：绑定样本集合契约的声明版本，科学计算与 1.0.0 相同。"""


# ---------------------------------------------------------------------------
# 表格模型契约声明版本：model 输出绑定 radstudio/model@1、predictions 绑定
# radstudio/prediction-table@1。血缘信封（task/dimension/family/fit/weights）
# 是要新增的产出，因此这一版与样本声明版不同，run 不再是同一个函数对象；
# 1.0.0 的产物字节保持原样，历史复现与固定版本的运行不受影响。
# ---------------------------------------------------------------------------


@operator("LogisticRegression", version="1.1.0")
class LogisticRegressionOperatorV11(LogisticRegressionOperator):
    """逻辑回归：输出声明模型血缘与 OOF 预测契约的声明版本。"""

    def run(self, ctx):
        return self._run_v110(ctx)


# radstudio/model@2 折内血缘：fit 不再给一份全集声明，而是逐折各自声明训练集与
# 自己的权重载荷。集成包 pipelines.joblib 与 pipelineArtifact 原样保留，所以读模型
# 的 ModelApply / Report 1.0.0 不必跟着改版——消费端后来的 1.1.0 / 1.2.0 只是把
# 「接受哪代血缘」写进声明，算的仍是同一份集成载荷。


@operator("LogisticRegression", version="1.2.0")
class LogisticRegressionOperatorV12(LogisticRegressionOperatorV11):
    """逻辑回归：逐折声明模型血缘（radstudio/model@2）。"""

    def run(self, ctx):
        return self._run_v120(ctx)


# ---------------------------------------------------------------------------
# 消费端绑定：ModelApply 的 model 输入声明 radstudio/model@2，把「接受哪代模型血缘」
# 变成建图阶段的闸门。本算子读的还是 pipelineArtifact 与 featureNames，@1 与 @2 把这些
# 键放在同一位置，因此 run 不必重写、应用结果与 1.0.0 逐字节相同；契约里不写 modelFamily，
# 任一分类器的 @2 产出都接得上。1.0.0 行保持未绑定：历史工作流与没有表格信封的深度学习
# 模型仍有一条能跑的通路。
# ---------------------------------------------------------------------------


@operator("ModelApply", version="1.1.0")
class ModelApplyOperatorV11(ModelApplyOperator):
    """模型外部应用：model 输入绑定 radstudio/model@2 的声明版本，计算与 1.0.0 相同。"""


# ---------------------------------------------------------------------------
# 固定划分：论文复现必须沿用数据集自带的官方 train/validation/test 划分，
# 随机分组交叉验证会把官方 test 集拆进训练折，评估口径就不再等价。
# 这一版按样本字段的取值切分，输出单折（fold=0）并带上折内验证集 ID。
# 输出不绑 radstudio/split-plan@1：该契约要求 source 指向特征表并按特征表
# 身份摘要校验，而影像分类链的划分来源是样本集，本算子无特征表可引用。
# 下游 DLCNNTraining 1.2.0 按 identityHash 与 fold 结构自行核对。
# ---------------------------------------------------------------------------


def _partition_values(parameters):
    """三个分区的取值集合；逐个校验非空、无重复、互不相交。"""
    groups = {}
    for name in ("trainValues", "validationValues", "testValues"):
        raw = parameters.get(name) or []
        values = [str(value) for value in raw]
        if any(not value for value in values):
            raise ValueError(f"{name} must not contain empty values")
        if len(values) != len(set(values)):
            raise ValueError(f"{name} contains duplicate values")
        groups[name] = values
    if not groups["trainValues"] or not groups["testValues"]:
        raise ValueError("FixedSplit requires non-empty trainValues and testValues")
    for left, right in (
        ("trainValues", "validationValues"),
        ("trainValues", "testValues"),
        ("validationValues", "testValues"),
    ):
        overlap = set(groups[left]) & set(groups[right])
        if overlap:
            raise ValueError(
                f"{left} and {right} overlap: " + ", ".join(sorted(overlap))
            )
    return groups


@operator("FixedSplit", version="1.0.0")
class FixedSplitOperator(Operator):
    """固定划分：按数据集自带的划分列切分，输出单折训练/验证/测试。"""

    def run(self, ctx):
        p = ctx.parameters
        field = str(p.get("partitionField") or "").strip()
        if not field:
            raise ValueError("partitionField must name a sample field")
        groups = _partition_values(p)

        samples = ctx.input_json("samples")
        if not samples:
            raise ValueError("FixedSplit requires samples")
        seen = set()
        for sample in samples:
            if not sample.get("imageId") or sample["imageId"] in seen:
                raise ValueError("FixedSplit requires unique non-empty imageId per sample")
            seen.add(sample["imageId"])
        assignment = {}
        for sample in samples:
            raw = sample.get(field)
            value = "" if raw is None else str(raw)
            target = None
            for partition, key in (
                ("train", "trainValues"),
                ("validation", "validationValues"),
                ("test", "testValues"),
            ):
                if value in groups[key]:
                    target = partition
                    break
            if target is None:
                raise ValueError(
                    f"Sample {sample.get('imageId')} has partition value {value!r} "
                    f"in field {field!r}, which is not covered by trainValues/"
                    "validationValues/testValues"
                )
            assignment[sample["imageId"]] = target

        partitions = {
            "train": [s["imageId"] for s in samples if assignment[s["imageId"]] == "train"],
            "validation": [
                s["imageId"] for s in samples if assignment[s["imageId"]] == "validation"
            ],
            "test": [s["imageId"] for s in samples if assignment[s["imageId"]] == "test"],
        }
        if not partitions["train"] or not partitions["test"]:
            raise ValueError("FixedSplit produced an empty train or test partition")

        fold = {
            "fold": 0,
            "trainIds": partitions["train"],
            "testIds": partitions["test"],
            "validationIds": partitions["validation"],
        }
        value = {
            "method": "FixedPartition",
            "scheme": "fixed",
            "partitionField": field,
            "partitions": {
                name: list(groups[name]) for name in ("trainValues", "validationValues", "testValues")
            },
            "counts": {name: len(ids) for name, ids in partitions.items()},
            "folds": [fold],
            "seed": None,
            "identityHash": identity(
                {
                    "records": [
                        {
                            "imageId": s["imageId"],
                            "patientId": s["patientId"],
                            "label": s.get("label"),
                        }
                        for s in samples
                    ]
                }
            ),
        }
        return {"split": ctx.output_json("split.json", value, "split_plan")}



