"""Sequential, artifact-backed workflow runner. stdout is exclusively NDJSON events.

Features are implemented estimates, not independently IBSI-reference validated.
All learned preprocessing is fitted separately inside each patient-grouped fold.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import re
import sys
from pathlib import Path

import joblib
import numpy as np
from openpyxl import Workbook
from openpyxl.styles import Font
from PIL import Image
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.feature_selection import f_classif
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
    roc_curve,
)
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .operator import (
    ArtifactStore,
    OperatorContext,
    OperatorRequest,
    legacy_to_ref,
    parse_ref,
    ref_to_legacy,
    resolve_invocation,
)

# 数据资源工作台读取节点：参数来自输入快照 manifest.config，IR 只存平台绑定字段。
READER_NODE_TYPES = ("ReadImages", "ReadTable", "ReadSingleCell", "ReadDicom", "ReadNifti")


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def identity(table):
    return hashlib.sha256(
        canonical(
            [
                {"imageId": r["imageId"], "patientId": r["patientId"], "label": r["label"]}
                for r in table["records"]
            ]
        )
    ).hexdigest()


def arrays(table):
    rows = table["records"]
    if not rows or not table["featureNames"]:
        raise ValueError("Feature table is empty")
    ids = [r.get("imageId") for r in rows]
    groups = [r.get("patientId") for r in rows]
    if any(not isinstance(v, str) or not v.strip() for v in ids + groups):
        raise ValueError("Every sample must have imageId and patientId")
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate imageId")
    y = np.asarray([r["label"] for r in rows])
    if set(y.tolist()) != {0, 1}:
        raise ValueError("Both binary classes are required")
    patients = {}
    for group, label in zip(groups, y):
        if group in patients and patients[group] != label:
            raise ValueError("Inconsistent labels for patient")
        patients[group] = label
    x = np.asarray([r["values"] for r in rows], dtype=float)
    if x.shape != (len(rows), len(table["featureNames"])) or np.isinf(x).any():
        raise ValueError("Invalid feature dimensions or infinite values")
    return x, y, np.asarray(groups), np.asarray(ids)


def make_split(table, folds=5, random_seed=42):
    x, y, groups, ids = arrays(table)
    folds = int(folds)
    random_seed = int(random_seed)
    if not 2 <= folds <= 10:
        raise ValueError("folds must be between 2 and 10")
    if random_seed < 0 or random_seed > 2**31 - 1:
        raise ValueError("randomSeed must be between 0 and 2^31-1")
    if any(len(set(groups[y == c])) < folds for c in [0, 1]):
        raise ValueError(f"{folds} distinct patients per class required for {folds} folds")
    result = []
    for i, (train_idx, test_idx) in enumerate(
        StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=random_seed).split(
            x, y, groups
        )
    ):
        if len(set(y[train_idx])) != 2 or len(set(y[test_idx])) != 2:
            raise ValueError("Insufficient class support in generated grouped folds")
        result.append(
            {"fold": i, "trainIds": ids[train_idx].tolist(), "testIds": ids[test_idx].tolist()}
        )
    return {
        "method": "StratifiedGroupKFold",
        "folds": result,
        "seed": random_seed,
        "identityHash": identity(table),
    }


def _subset_table(table, keep_ids):
    keep = set(keep_ids)
    return {
        "featureNames": list(table["featureNames"]),
        "records": [row for row in table["records"] if row["imageId"] in keep],
    }


def make_holdout(
    table,
    validation_ratio=0.15,
    test_ratio=0.2,
    random_seed=42,
    stratify=True,
    *,
    allow_empty_validation=False,
):
    """患者级训练/验证/测试划分；返回三张特征表与划分清单。"""
    _x, y, groups, ids = arrays(table)
    validation_ratio = float(validation_ratio)
    test_ratio = float(test_ratio)
    random_seed = int(random_seed)
    if validation_ratio < 0 or test_ratio <= 0 or validation_ratio + test_ratio >= 0.8:
        raise ValueError("validation/test ratios leave an insufficient training set")
    rng = np.random.default_rng(random_seed)
    classes = [0, 1] if stratify else [None]
    assignment = {}
    for cls in classes:
        mask = np.ones(len(y), dtype=bool) if cls is None else y == cls
        class_groups = sorted({str(g) for g in groups[mask]})
        rng.shuffle(class_groups)
        total = len(class_groups)
        test_count = max(1, round(total * test_ratio))
        validation_count = max(1, round(total * validation_ratio)) if validation_ratio > 0 else 0
        train_count = total - test_count - validation_count
        if train_count < 1:
            raise ValueError("Too few patients for the requested holdout ratios")
        for index, group in enumerate(class_groups):
            if index < test_count:
                assignment[group] = "test"
            elif index < test_count + validation_count:
                assignment[group] = "validation"
            else:
                assignment[group] = "train"
    partitions = {
        name: [str(img) for img, group in zip(ids, groups) if assignment[str(group)] == name]
        for name in ("train", "validation", "test")
    }
    if not partitions["train"] or not partitions["test"]:
        raise ValueError("Holdout split produced an empty train or test partition")
    if stratify:
        for name, part_ids in partitions.items():
            if allow_empty_validation and name == "validation" and not part_ids:
                continue
            keep = set(part_ids)
            labels = {int(label) for label, image in zip(y, ids) if str(image) in keep}
            if labels != {0, 1}:
                raise ValueError(f"Holdout partition {name} lacks both classes")
    split = {
        "scheme": "holdout",
        "partitions": partitions,
        "seed": random_seed,
        "stratified": bool(stratify),
        "identityHash": identity(table),
    }
    return {
        "split": split,
        "train": _subset_table(table, partitions["train"]),
        "validation": _subset_table(table, partitions["validation"]),
        "test": _subset_table(table, partitions["test"]),
    }


class FoldFilter(BaseEstimator, TransformerMixin):
    """Learn missingness/variance/correlation exclusively from the training fold."""

    def __init__(self, missingRateThreshold=0.2, varianceThreshold=0.01, correlationThreshold=0.95):
        self.missingRateThreshold = missingRateThreshold
        self.varianceThreshold = varianceThreshold
        self.correlationThreshold = correlationThreshold

    def fit(self, x, y=None):
        x = np.asarray(x, dtype=float)
        keep = np.flatnonzero(
            (np.mean(np.isnan(x), axis=0) <= self.missingRateThreshold)
            & (np.nanvar(x, axis=0) > self.varianceThreshold)
        )
        selected = []
        # Pairwise correlation on jointly observed training values; undefined => no removal.
        for col in keep:
            redundant = False
            for prev in selected:
                valid = np.isfinite(x[:, col]) & np.isfinite(x[:, prev])
                if (
                    valid.sum() > 1
                    and np.std(x[valid, col]) > 0
                    and np.std(x[valid, prev]) > 0
                    and abs(np.corrcoef(x[valid, col], x[valid, prev])[0, 1])
                    >= self.correlationThreshold
                ):
                    redundant = True
                    break
            if not redundant:
                selected.append(int(col))
        if not selected:
            raise ValueError("No features survive fold-local filtering")
        self.selected_ = selected
        return self

    def transform(self, x):
        return np.asarray(x)[:, self.selected_]


def train(
    table,
    split,
    strategy,
    parameters,
    estimator_factory=None,
    estimator_kind=None,
    lineage=None,
    fold_lineage=False,
):
    """折内训练五折集成，输出 OOF 预测与逐折 pipeline。

    estimator_factory: ``parameters`` → sklearn 分类器的工厂函数；
    缺省为逻辑回归（单体 runner 既有行为，保持向后兼容）。
    estimator_kind: 写入模型元数据的分类器类型标记（如 "random_forest"）。
    lineage: ``{"features": 引用, "split": 引用}``；给出时模型元数据额外写入
    radstudio/model@1 的血缘信封（task/dimension/family/preprocess/fit），
    缺席时与已发布的 1.0.0 字节完全一致。
    fold_lineage: 配合 lineage，把 fit 从一份全集声明换成逐折声明
    （radstudio/model@2）；每一折的权重文件由 classifier_outputs 落盘后回填。
    """
    if estimator_factory is None:

        def estimator_factory(p):
            return LogisticRegression(
                C=float(p.get("C", 1.0)),
                max_iter=int(p.get("maxIterations", 1000)),
                solver="lbfgs",
                random_state=42,
            )

    x, y, groups, ids = arrays(table)
    if split.get("identityHash") != identity(table):
        raise ValueError("Feature identity does not match split plan")
    by_id = {v: i for i, v in enumerate(ids)}
    covered = set()
    predictions = []
    pipelines = []
    fold_details = []
    step_names = None
    if len(split["folds"]) < 2:
        raise ValueError("At least two validation folds are required")
    for fold in split["folds"]:
        train_ids, test_ids = fold["trainIds"], fold["testIds"]
        if len(set(train_ids)) != len(train_ids) or len(set(test_ids)) != len(test_ids):
            raise ValueError("Duplicate IDs in split")
        if (
            set(train_ids) & set(test_ids)
            or set(train_ids) | set(test_ids) != set(ids)
            or covered & set(test_ids)
        ):
            raise ValueError("Invalid split partition")
        ti = [by_id[v] for v in train_ids]
        vi = [by_id[v] for v in test_ids]
        if set(groups[ti]) & set(groups[vi]) or len(set(y[ti])) != 2 or len(set(y[vi])) != 2:
            raise ValueError("Patient leakage or insufficient classes in split")
        steps = [
            ("missing", MissingColumns(strategy.get("missingRateThreshold", 0.2))),
            ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
            (
                "filter",
                FoldFilter(
                    missingRateThreshold=1,
                    varianceThreshold=float(strategy.get("varianceThreshold", 0.01)),
                    correlationThreshold=float(strategy.get("correlationThreshold", 0.95)),
                ),
            ),
            ("scale", StandardScaler()),
        ]
        method = strategy.get("method", "variance_correlation")
        if method != "variance_correlation":
            steps.append(
                (
                    "select",
                    FoldFeatureSelector(
                        method=method,
                        top_k=int(strategy.get("univariateTopK", 0) or 0),
                        lasso_alpha=float(strategy.get("lassoAlpha", 0.01)),
                        max_iter=int(strategy.get("lassoMaxIter", 1000)),
                    ),
                )
            )
        if lineage is not None and step_names is None:
            step_names = [name for name, _ in steps]
        steps.append(("classifier", estimator_factory(parameters)))
        pipe = Pipeline(steps)
        pipe.fit(x[ti], y[ti])
        p = pipe.predict_proba(x[vi])[:, 1]
        for idx, prob in zip(vi, p):
            predictions.append(
                {
                    "imageId": str(ids[idx]),
                    "patientId": str(groups[idx]),
                    "label": int(y[idx]),
                    "probability": float(prob),
                    "fold": fold["fold"],
                }
            )
        covered.update(test_ids)
        pipelines.append(pipe)
        fold_details.append(
            {
                "fold": fold["fold"],
                "trainCount": len(ti),
                "testCount": len(vi),
                "selectedFeatureNames": _selected_feature_names(pipe, table["featureNames"]),
            }
        )
    if covered != set(ids):
        raise ValueError("Split does not cover all samples")
    predictions.sort(key=lambda row: by_id[row["imageId"]])
    model = {
        "kind": ("five_fold_ensemble" if len(split["folds"]) == 5 else "cross_validation_ensemble"),
        "featureNames": table["featureNames"],
        "folds": fold_details,
        "foldCount": len(split["folds"]),
        "seed": int(split.get("seed", 42)),
        "evaluation": "out_of_fold",
    }
    if estimator_kind:
        model["estimator"] = estimator_kind
    if lineage is not None:
        model["task"] = "binary_classification"
        model["dimension"] = "tabular"
        model["family"] = estimator_kind or "logistic_regression"
        model["preprocess"] = step_names
        fit = {"features": lineage["features"], "split": lineage["split"]}
        if fold_lineage:
            # 折内血缘逐折交代训练集；各自的权重文件由 classifier_outputs 落盘后回填。
            fit["folds"] = [
                {
                    "fold": fold["fold"],
                    "trainedImageIds": sorted(fold["trainIds"]),
                    "trainedCount": len(fold["trainIds"]),
                }
                for fold in split["folds"]
            ]
        else:
            trained = sorted({sample for fold in split["folds"] for sample in fold["trainIds"]})
            fit["trainedImageIds"] = trained
            fit["trainedCount"] = len(trained)
        model["fit"] = fit
    return {
        "pipelines": pipelines,
        "model": model,
        "predictions": {
            "records": predictions,
            "evaluation": "out_of_fold",
            "identityHash": identity(table),
        },
    }


def model_lineage(ctx):
    """把分类器实际消费的输入引用交给血缘信封。"""
    return {"features": ctx.inputs["features"], "split": ctx.inputs["split"]}


def classifier_outputs(ctx, trained, *, declare_lineage=False, fold_lineage=False):
    """分类器共用输出：折内 pipeline 打包 + 模型元数据 + OOF 预测。

    declare_lineage 只额外写入 weights 摘要，配合 train(lineage=...) 才凑齐
    radstudio/model@1 信封；不给出时产物字节与已发布的 1.0.0 完全一致。
    fold_lineage 再为每一折另写一份只含该折 pipeline 的载荷，并把路径与摘要
    回填进 train(fold_lineage=True) 声明的 fit.folds，凑齐 radstudio/model@2。
    """
    buffer = io.BytesIO()
    joblib.dump(trained["pipelines"], buffer)
    payload = buffer.getvalue()
    reference = ctx.output_bytes("pipelines.joblib", payload, "model")
    trained["model"]["pipelineArtifact"] = reference
    if declare_lineage:
        info = parse_ref(reference)
        trained["model"]["weights"] = {
            "path": info["path"],
            "sha256": info["sha256"],
            "sizeBytes": len(payload),
        }
    if fold_lineage:
        for fold, pipeline in zip(trained["model"]["fit"]["folds"], trained["pipelines"]):
            fold_buffer = io.BytesIO()
            # 单折载荷仍是列表：推理端按列表消费，不必区分整包与折包。
            joblib.dump([pipeline], fold_buffer)
            fold_payload = fold_buffer.getvalue()
            fold_info = parse_ref(
                ctx.output_bytes(
                    f"pipelines.fold{fold['fold']}.joblib",
                    fold_payload,
                    "model",
                )
            )
            fold["weights"] = {
                "path": fold_info["path"],
                "sha256": fold_info["sha256"],
                "sizeBytes": len(fold_payload),
            }
    return {
        "model": ctx.output_json("model.json", trained["model"], "model"),
        "predictions": ctx.output_json(
            "predictions.json", trained["predictions"], "prediction_table"
        ),
    }


class MissingColumns(BaseEstimator, TransformerMixin):
    def __init__(self, threshold=0.2):
        self.threshold = threshold

    def fit(self, x, y=None):
        self.selected_ = np.flatnonzero(np.mean(np.isnan(x), axis=0) <= self.threshold).tolist()
        if not self.selected_:
            raise ValueError("No features survive missingness filtering")
        return self

    def transform(self, x):
        return np.asarray(x)[:, self.selected_]


class FoldFeatureSelector(BaseEstimator, TransformerMixin):
    """折内 univariate/LASSO 选择；仅在新版 FeatureFilter 策略中启用。"""

    def __init__(self, method="univariate", top_k=0, lasso_alpha=0.01, max_iter=1000):
        self.method = method
        self.top_k = top_k
        self.lasso_alpha = lasso_alpha
        self.max_iter = max_iter

    def fit(self, x, y):
        x = np.asarray(x)
        n_features = x.shape[1]
        if self.method == "univariate":
            if n_features == 0:
                raise ValueError("No features available for univariate selection")
            scores, _ = f_classif(x, y)
            scores = np.nan_to_num(scores, nan=-np.inf)
            k = n_features if self.top_k <= 0 else min(self.top_k, n_features)
            self.selected_ = np.argsort(-scores)[:k].tolist()
        elif self.method == "lasso":
            if self.lasso_alpha <= 0:
                raise ValueError("lassoAlpha must be positive")
            classifier = LogisticRegression(
                penalty="l1",
                C=1.0 / self.lasso_alpha,
                solver="liblinear",
                max_iter=self.max_iter,
                random_state=42,
            )
            classifier.fit(x, y)
            coefficients = np.abs(np.asarray(classifier.coef_)).max(axis=0)
            selected = np.flatnonzero(coefficients > 1e-8).tolist()
            self.selected_ = selected or [int(np.argmax(coefficients))].copy()
        else:
            raise ValueError(f"Unsupported feature selection method: {self.method}")
        if not self.selected_:
            raise ValueError("No features survive fold-local selection")
        return self

    def transform(self, x):
        return np.asarray(x)[:, self.selected_]


def _selected_feature_names(pipe, feature_names):
    names = [feature_names[i] for i in pipe.named_steps["missing"].selected_]
    names = [names[i] for i in pipe.named_steps["filter"].selected_]
    selector = pipe.named_steps.get("select")
    if selector is not None:
        names = [names[i] for i in selector.selected_]
    return names


def _downsample(values, max_points=500):
    if len(values) <= max_points:
        return values
    index = np.unique(np.linspace(0, len(values) - 1, max_points).astype(int))
    return [values[i] for i in index]


def _bootstrap_binary_ci(y, p, threshold, n_boot, level, seed):
    if n_boot <= 0 or len(y) < 10:
        return {}
    rng = np.random.default_rng(seed)
    positive = np.flatnonzero(y == 1)
    negative = np.flatnonzero(y == 0)
    if not len(positive) or not len(negative):
        return {}
    collected = {key: [] for key in ("auc", "sensitivity", "specificity", "accuracy", "ppv", "npv")}
    for _ in range(int(n_boot)):
        index = np.concatenate(
            [
                rng.choice(positive, size=len(positive), replace=True),
                rng.choice(negative, size=len(negative), replace=True),
            ]
        )
        yy = y[index]
        pp = p[index]
        pred = pp >= threshold
        tn, fp, fn, tp = confusion_matrix(yy, pred, labels=[0, 1]).ravel()
        values = {
            "auc": roc_auc_score(yy, pp),
            "sensitivity": tp / (tp + fn) if tp + fn else np.nan,
            "specificity": tn / (tn + fp) if tn + fp else np.nan,
            "accuracy": accuracy_score(yy, pred),
            "ppv": tp / (tp + fp) if tp + fp else np.nan,
            "npv": tn / (tn + fn) if tn + fn else np.nan,
        }
        for key, value in values.items():
            if np.isfinite(value):
                collected[key].append(float(value))
    alpha = (1 - float(level)) / 2
    return {
        key: [float(np.quantile(values, alpha)), float(np.quantile(values, 1 - alpha))]
        for key, values in collected.items()
        if values
    }


def _calibration(y, p, bins):
    order = np.argsort(p)
    chunks = np.array_split(order, max(2, min(int(bins), len(order))))
    points = []
    for chunk in chunks:
        if not len(chunk):
            continue
        points.append(
            {
                "predicted": float(np.mean(p[chunk])),
                "observed": float(np.mean(y[chunk])),
                "count": len(chunk),
            }
        )
    logit = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    intercept = None
    slope = None
    if len(set(y.tolist())) == 2:
        try:
            model = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)
            model.fit(logit.reshape(-1, 1), y)
            slope = float(model.coef_[0][0])
            intercept = float(model.intercept_[0])
        except ValueError:
            pass
    return {"bins": points, "intercept": intercept, "slope": slope}


def _decision_curve(y, p):
    n = len(y)
    prevalence = float(np.mean(y))
    thresholds = np.round(np.arange(0.01, 1.0, 0.01), 2)
    model, treat_all = [], []
    for threshold in thresholds:
        pred = p >= threshold
        tp = int(np.sum(pred & (y == 1)))
        fp = int(np.sum(pred & (y == 0)))
        weight = threshold / (1 - threshold)
        model.append(float(tp / n - fp / n * weight))
        treat_all.append(float(prevalence - (1 - prevalence) * weight))
    return {
        "thresholds": thresholds.tolist(),
        "model": model,
        "treatAll": treat_all,
        "treatNone": [0.0 for _ in thresholds],
    }


def _binary_evaluation(y, p, threshold, threshold_mode, n_boot, level, calibration_bins, seed):
    fpr, tpr, roc_thresholds = roc_curve(y, p)
    if threshold_mode == "youden":
        # sklearn's initial +inf threshold is a plotting sentinel, not a
        # deployable decision threshold (and cannot be written as strict JSON).
        candidates = np.flatnonzero(np.isfinite(roc_thresholds))
        index = int(candidates[np.argmax((tpr - fpr)[candidates])])
        threshold = float(roc_thresholds[index])
    pred = p >= threshold
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    precision_curve, recall_curve, pr_thresholds = precision_recall_curve(y, p)
    metrics = {
        "evaluation": None,
        "n": len(y),
        "positives": int(np.sum(y == 1)),
        "negatives": int(np.sum(y == 0)),
        "threshold": float(threshold),
        "auc": float(roc_auc_score(y, p)),
        "prAuc": float(average_precision_score(y, p)),
        "accuracy": float(accuracy_score(y, pred)),
        "precision": float(precision_score(y, pred, zero_division=0)),
        "recall": float(recall_score(y, pred, zero_division=0)),
        "f1": float(f1_score(y, pred, zero_division=0)),
        "sensitivity": float(tp / (tp + fn)) if tp + fn else 0.0,
        "specificity": float(tn / (tn + fp)) if tn + fp else 0.0,
        "ppv": float(tp / (tp + fp)) if tp + fp else 0.0,
        "npv": float(tn / (tn + fn)) if tn + fn else 0.0,
        "brier": float(brier_score_loss(y, p)),
        "confusion": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }
    metrics["ci"] = _bootstrap_binary_ci(y, p, threshold, n_boot, level, seed)
    calibration = _calibration(y, p, calibration_bins)
    calibration["brier"] = metrics["brier"]
    return {
        "metrics": metrics,
        "roc": {
            "fpr": _downsample(fpr.tolist()),
            "tpr": _downsample(tpr.tolist()),
            "thresholds": _downsample(
                [float(v) if np.isfinite(v) else None for v in roc_thresholds]
            ),
            "auc": metrics["auc"],
        },
        "pr": {
            "precision": _downsample(precision_curve.tolist()),
            "recall": _downsample(recall_curve.tolist()),
            "thresholds": _downsample(pr_thresholds.tolist()),
            "averagePrecision": metrics["prAuc"],
        },
        "calibration": calibration,
        "decisionCurve": _decision_curve(y, p),
        "confusion": {
            "matrix": [[int(tn), int(fp)], [int(fn), int(tp)]],
            "labels": [0, 1],
        },
    }


def _multiclass_evaluation(rows, labels):
    y = np.array([labels.index(row["label"]) for row in rows])
    probabilities = np.array([row.get("classProbabilities") for row in rows], dtype=float)
    if probabilities.ndim != 2 or probabilities.shape != (len(rows), len(labels)):
        raise ValueError("Multiclass predictions require classProbabilities for every class")
    predicted = np.argmax(probabilities, axis=1)
    matrix = confusion_matrix(y, predicted, labels=list(range(len(labels))))
    metrics = {
        "evaluation": None,
        "n": len(y),
        "classes": [float(label) if isinstance(label, (int, float)) else label for label in labels],
        "accuracy": float(accuracy_score(y, predicted)),
        "balancedAccuracy": float(balanced_accuracy_score(y, predicted)),
        "precisionMacro": float(precision_score(y, predicted, average="macro", zero_division=0)),
        "recallMacro": float(recall_score(y, predicted, average="macro", zero_division=0)),
        "f1Macro": float(f1_score(y, predicted, average="macro", zero_division=0)),
        "precisionMicro": float(precision_score(y, predicted, average="micro", zero_division=0)),
        "recallMicro": float(recall_score(y, predicted, average="micro", zero_division=0)),
        "f1Micro": float(f1_score(y, predicted, average="micro", zero_division=0)),
    }
    try:
        metrics["aucOvrMacro"] = float(
            roc_auc_score(y, probabilities, multi_class="ovr", average="macro")
        )
    except ValueError:
        metrics["aucOvrMacro"] = None
    roc_curves, pr_curves = [], []
    for index, label in enumerate(labels):
        binary = (y == index).astype(int)
        fpr, tpr, _ = roc_curve(binary, probabilities[:, index])
        precision, recall, _ = precision_recall_curve(binary, probabilities[:, index])
        roc_curves.append(
            {
                "class": float(label) if isinstance(label, (int, float)) else label,
                "fpr": _downsample(fpr.tolist()),
                "tpr": _downsample(tpr.tolist()),
                "auc": float(roc_auc_score(binary, probabilities[:, index])),
            }
        )
        pr_curves.append(
            {
                "class": float(label) if isinstance(label, (int, float)) else label,
                "precision": _downsample(precision.tolist()),
                "recall": _downsample(recall.tolist()),
                "averagePrecision": float(average_precision_score(binary, probabilities[:, index])),
            }
        )
    return {
        "metrics": metrics,
        "roc": {"curves": roc_curves},
        "pr": {"curves": pr_curves},
        "calibration": {"bins": [], "intercept": None, "slope": None},
        "decisionCurve": {"thresholds": [], "model": [], "treatAll": [], "treatNone": []},
        "confusion": {"matrix": matrix.astype(int).tolist(), "labels": metrics["classes"]},
    }


def evaluate(
    predictions,
    threshold=0.5,
    threshold_mode="fixed",
    bootstrap=2000,
    ci_level=0.95,
    calibration_bins=10,
    seed=42,
):
    evaluation = predictions.get("evaluation")
    allowed = {"out_of_fold", "external", "validation", "inference", "holdout"}
    if evaluation not in allowed:
        raise ValueError("Evaluation requires OOF, external, validation or inference predictions")
    rows = predictions["records"]
    if not rows:
        raise ValueError("Prediction table is empty")
    if any(row.get("label") is None for row in rows):
        raise ValueError("Evaluation requires labels on every prediction")
    labels = sorted({row["label"] for row in rows})
    if len(labels) > 2:
        result = _multiclass_evaluation(rows, labels)
    else:
        y = np.array([int(row["label"]) for row in rows])
        p = np.array([float(row["probability"]) for row in rows])
        if not len(y) or len(set(y.tolist())) != 2:
            raise ValueError("Both binary classes are required")
        result = _binary_evaluation(
            y,
            p,
            float(threshold),
            str(threshold_mode),
            int(bootstrap),
            float(ci_level),
            int(calibration_bins),
            int(seed),
        )
    result["metrics"]["evaluation"] = evaluation
    return result


def apply_pipelines(table, model, pipelines):
    """把折集成 pipeline 应用到外部特征表，输出 external prediction_table。"""
    model_names = list(model.get("featureNames") or [])
    if not model_names:
        raise ValueError("Model metadata lacks featureNames")
    table_names = list(table.get("featureNames") or [])
    index = {name: i for i, name in enumerate(table_names)}
    missing = [name for name in model_names if name not in index]
    if missing:
        raise ValueError("External feature table is missing model features: " + ", ".join(missing))
    if not pipelines:
        raise ValueError("Model artifact contains no pipelines")
    rows = table["records"]
    x = np.asarray(
        [[row["values"][index[name]] for name in model_names] for row in rows], dtype=float
    )
    probabilities = np.zeros(len(rows), dtype=float)
    for pipe in pipelines:
        probabilities += pipe.predict_proba(x)[:, 1]
    probabilities /= len(pipelines)
    records = [
        {
            "imageId": str(row["imageId"]),
            "patientId": str(row["patientId"]),
            "label": int(row["label"]),
            "probability": float(probability),
            "fold": None,
        }
        for row, probability in zip(rows, probabilities)
    ]
    return {
        "records": records,
        "evaluation": "external",
        "folds": len(pipelines),
    }


def feature_workbook(names, records):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "features"
    sheet.append(["imageId", "patientId", "label", *names])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in records:
        sheet.append([row["imageId"], row["patientId"], row["label"], *row["values"]])
    sheet.freeze_panes = "A2"
    import io

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def read_image(path):
    path = Path(path)
    if path.suffix.lower() == ".npy":
        value = np.load(path, allow_pickle=False)
    else:
        with Image.open(path) as image:
            value = np.asarray(image)
    if value.ndim != 2:
        raise ValueError("Only grayscale 2D images supported")
    return value.astype(float)


def catalog_path():
    from importlib.resources import files

    return files("radstudio").joinpath("operators.json")


def validate_graph(ir, target=None, catalog_snapshot=None):
    from .catalog_snapshot import resolve_request_catalog
    from .execution_contract import require_logical_parameters
    from .parameter_validation import require_valid_parameters
    from .port_contract import PortContractError, port_contract_error

    if ir.get("schemaVersion") != "2":
        raise ValueError("Workflow schemaVersion must be 2")
    snapshot = resolve_request_catalog(
        (
            {"ir": ir}
            if catalog_snapshot is None
            else {"ir": ir, "operatorCatalogSnapshot": catalog_snapshot}
        ),
        catalog_path,
    )
    catalog = {nid: entry["definition"] for nid, entry in snapshot["nodes"].items()}
    nodes = {n["id"]: n for n in ir["nodes"]}
    if len(nodes) != len(ir["nodes"]) or not nodes:
        raise ValueError("Duplicate node IDs or empty graph")
    incoming = {n: [] for n in nodes}
    for n in nodes.values():
        if not re.fullmatch("[a-zA-Z][a-zA-Z0-9_]*", n["id"]):
            raise ValueError("Invalid node ID")
        c = catalog.get(n["id"])
        if c is None:
            raise ValueError("Unknown operator or version")
        # 读取节点的参数来自输入快照 manifest.config（服务端已校验），IR 只存
        # 平台绑定字段（dataCollectionId/readerConfigId），因此不在此校验声明参数。
        if n["type"] not in READER_NODE_TYPES:
            require_valid_parameters(n["parameters"], c["parametersSchema"] or {})
            require_logical_parameters(
                n["type"], n["parameters"], {"inputs": c.get("inputs"), "outputs": c.get("outputs")}
            )
    for e in ir["edges"]:
        if e["source"] not in nodes or e["target"] not in nodes:
            raise ValueError("Dangling edge")
        source_node = nodes[e["source"]]
        target_node = nodes[e["target"]]
        source = {p["name"]: p for p in (catalog[source_node["id"]]["outputs"] or [])}
        target_ports = {p["name"]: p for p in (catalog[target_node["id"]]["inputs"] or [])}
        if e["sourceOutput"] not in source or e["targetInput"] not in target_ports:
            raise PortContractError("连接引用了不存在的端口", e)
        mismatch = port_contract_error(source[e["sourceOutput"]], target_ports[e["targetInput"]])
        if mismatch:
            raise PortContractError(mismatch, e)
        if any(p["targetInput"] == e["targetInput"] for p in incoming[e["target"]]):
            raise ValueError("Multiple connections to input")
        incoming[e["target"]].append(e)
    for nid, n in nodes.items():
        connected = {e["targetInput"] for e in incoming[nid]}
        definition = catalog[nid]
        if any(
            p.get("required", False) and p["name"] not in connected
            for p in (definition["inputs"] or [])
        ):
            raise ValueError("Missing required input on " + nid)
    order = []
    visiting = set()
    visited = set()

    def visit(nid):
        if nid in visiting:
            raise ValueError("Cycle detected")
        if nid in visited:
            return
        visiting.add(nid)
        for e in incoming[nid]:
            visit(e["source"])
        visiting.remove(nid)
        visited.add(nid)
        order.append(nid)

    if target:
        if target not in nodes:
            raise ValueError("Unknown target node")
        visit(target)
    else:
        for nid in nodes:
            visit(nid)
    return nodes, incoming, order


class Context:
    def __init__(self, request, output, emit):
        self.request = request
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.emit = emit
        self.manifest = []
        self.implementations = {}
        self.execution_receipts = {}
        self.artifact_checks = {}
        self._pending = None
        self._pending_node = None
        self.log = []

    def begin_node(self, nid):
        if self._pending is not None:
            raise ValueError("Another node publication is active")
        self._pending_node = nid
        self._pending = []

    def pending_metadata(self):
        return [
            {
                "storagePath": entry["path"],
                "type": entry["artifactType"],
                "fileHash": entry["fileHash"],
                "fileSize": entry["fileSize"],
            }
            for entry in (self._pending or [])
        ]

    def commit_node(self):
        entries = self._pending or []
        self._pending = None
        self._pending_node = None
        self.manifest.extend(entries)
        (self.output / "manifest.json").write_bytes(canonical(self.manifest))
        for entry in entries:
            self.emit("artifact.created", entry)

    def rollback_node(self):
        self._pending = None
        self._pending_node = None

    def _record_artifact(self, nid, name, path, kind, content):
        from .artifact_boundary import hash_file

        digest, size = hash_file(path)
        entry = {
            "nodeId": nid,
            "name": name,
            "path": path.relative_to(self.output.resolve()).as_posix(),
            "artifactType": kind,
            "contentType": content,
            "fileHash": digest,
            "fileSize": size,
        }
        if self._pending is not None:
            if nid != self._pending_node:
                raise ValueError("Node cannot register another node's artifacts")
            self._pending = [prior for prior in self._pending if prior["path"] != entry["path"]]
            self._pending.append(entry)
        else:
            self.manifest.append(entry)
            (self.output / "manifest.json").write_bytes(canonical(self.manifest))
            self.emit("artifact.created", entry)
        return {"path": entry["path"], "sha256": digest, "type": kind}

    def artifact(self, nid, name, value, kind, content="application/json", suffix=".json"):
        from .artifact_boundary import validate_kind, workspace_path

        validate_kind(kind)
        path = workspace_path(self.output, nid + "/" + name + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else canonical(value))
        return self._record_artifact(nid, name, path, kind, content)

    def read(self, ref):
        from .artifact_boundary import workspace_path

        info = parse_ref(legacy_to_ref(ref))
        path = workspace_path(self.output, info["path"], must_exist=True)
        if hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
            raise ValueError("Artifact hash mismatch")
        return json.loads(path.read_text(encoding="utf-8"))


# 产物文件扩展名 → manifest contentType（保持单体 runner 既有布局与事件兼容）
_CONTENT_TYPES = {
    ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".npz": "application/octet-stream",
    ".joblib": "application/octet-stream",
    ".html": "text/html",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".zip": "application/zip",
}


def _split_artifact_name(name):
    """拆分产物文件名为（manifest 名称, 扩展名）；无扩展名时按 JSON 处理。"""
    stem, dot, ext = name.rpartition(".")
    if not stem or not dot or "/" in ext:
        return name, ".json"
    return stem, "." + ext


class _RunnerArtifactStore(ArtifactStore):
    """单体 runner 的产物存储适配器。

    读取以 run 输出目录为根解析字符串引用；写入委托给 runner Context 的
    artifact()，保持 {节点目录}/{产物名} 布局、manifest 与事件流不变。
    """

    def __init__(self, context, nid):
        super().__init__(context.output)
        self._context = context
        self._nid = nid

    def read_bytes(self, ref):
        return super().read_bytes(ref)

    def write_bytes(self, name, data, kind):
        stem, ext = _split_artifact_name(name)
        ref = self._context.artifact(
            self._nid,
            stem,
            data,
            kind,
            _CONTENT_TYPES.get(ext, "application/octet-stream"),
            ext,
        )
        return legacy_to_ref(ref)

    def write_local(self, name, data):
        from .artifact_boundary import workspace_path

        path = workspace_path(self.root / self._nid, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def register(self, name, kind):
        from .artifact_boundary import validate_kind, workspace_path

        validate_kind(kind)
        path = workspace_path(self.root / self._nid, name, must_exist=True)
        self._context._record_artifact(
            self._nid, name, path, kind, _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        )


class _RunnerOperatorContext(OperatorContext):
    """单体 runner 的算子上下文：框架字符串引用 ↔ runner Context 桥接。

    产物写出经 runner Context 的 artifact() 落盘，保证 NDJSON 事件流、
    manifest 与复现包和迁移前完全一致；reproduction_bundle 供 Report
    算子在单体上下文生成复现包。
    """

    def __init__(self, request, context, nid):
        super().__init__(request, _RunnerArtifactStore(context, nid))
        self._context = context
        self._nid = nid

    def output_bytes(self, name, data, kind):
        stem, ext = _split_artifact_name(name)
        ref = self._context.artifact(
            self._nid,
            stem,
            data,
            kind,
            _CONTENT_TYPES.get(ext, "application/octet-stream"),
            ext,
        )
        return legacy_to_ref(ref)

    def reproduction_bundle(self):
        """生成复现包产物引用（仅单体 runner 上下文可用）。"""
        ref = self._context.artifact(
            self._nid,
            "reproduction",
            reproduction(self._context),
            "archive",
            "application/zip",
            ".zip",
        )
        return legacy_to_ref(ref)


def _load_builtin_operators():
    # 导入即注册算子（复现包内同样生效）；operators_dl 模块级不依赖 torch，
    # 调用 DL 算子时才检查 [dl] extra；operators_sc 模块级不依赖 scanpy（[sc] extra）；
    # operators_seg 模块级只依赖 numpy/PIL，torch/MONAI 在运行期才校验；
    # operators_readers（数据资源工作台读取节点）模块级只依赖 numpy，scipy/anndata 运行期校验；
    # operators_volumes（T9 体数据处理节点）模块级只依赖 numpy
    from . import (  # noqa: F401
        operators_bench,
        operators_builtin,
        operators_dl,
        operators_figures,
        operators_hybrid,
        operators_ibsi,
        operators_prep,
        operators_radiomics,
        operators_readers,
        operators_sc,
        operators_seg,
        operators_volumes,
    )


def execute(c, n, inputs):
    """经统一算子注册中心执行节点，返回 {端口名: dict 产物引用}。

    单体 runner 与 serve 模式共用 operators_builtin 中的算子实现；
    此处仅做引用格式转换（dict ↔ 字符串）与事件流桥接。
    """
    _load_builtin_operators()

    definition = c.request["operatorCatalogSnapshot"]["nodes"][n["id"]]["definition"]
    execution_version = definition["version"]
    # 读取节点：manifest 由执行端暂存到 /input，environment 只带引用；
    # 有效参数取 manifest.config（IR 参数只存平台绑定字段）。
    environment = {}
    parameters = n["parameters"]
    snapshot_input = (c.request.get("snapshotInputs") or {}).get(n["id"])
    if snapshot_input:
        from .operators_readers import load_environment_manifest

        environment["inputManifestRef"] = snapshot_input.get("manifestPath")
        environment["snapshot"] = snapshot_input
        environment["workspaceRoot"] = "/input"
        manifest = load_environment_manifest(environment, "/input")
        parameters = dict(manifest.get("config") or {})
    request = OperatorRequest(
        requestId=f"{c.request.get('runId', 'run')}-{n['id']}",
        runId=str(c.request.get("runId", "")),
        nodeId=n["id"],
        operatorType=n["type"],
        parameters=parameters,
        inputs={key: legacy_to_ref(value) for key, value in inputs.items()},
        operatorVersion=execution_version,
        environment=environment,
    )
    from .execution_contract import (
        freeze_invocation_contract,
        freeze_parameter_contract,
        invocation_artifact_ports,
        validate_execution_contract,
    )

    request.parameterContract = freeze_parameter_contract(
        n["type"],
        execution_version,
        definition["parametersSchema"] or {},
        artifact_ports={
            "version": 1,
            "contentContracts": 1,
            "inputs": definition["inputs"],
            "outputs": definition["outputs"],
        }
        if c.request["operatorCatalogSnapshot"]["rules"].get("artifactExecution") == 1
        else None,
    )
    if c.request["operatorCatalogSnapshot"]["rules"].get("invocationExecution") == 1:
        request.invocationContract = freeze_invocation_contract(request)
    receipt = validate_execution_contract(request)
    invocation_artifact_ports(request)
    from .artifact_boundary import ArtifactBoundaryError, validate_inputs, validate_outputs

    many_ports = [
        port["name"] for port in definition["inputs"] if port.get("cardinality") == "many"
    ]
    if many_ports:
        raise ArtifactBoundaryError(
            "operator.input_invalid",
            "单体 runner 不支持多值端口：" + ", ".join(sorted(many_ports)),
            {"ports": many_ports},
        )
    verify_content = c.request["operatorCatalogSnapshot"]["rules"].get("contentContracts") == 1
    validate_inputs(request.inputs, c.output, definition["inputs"], verify_content=verify_content)

    ctx = _RunnerOperatorContext(request, c, n["id"])
    operator_class, implementation = resolve_invocation(n["type"], execution_version, True)
    outputs = operator_class().run(ctx)
    c.artifact_checks[n["id"]] = validate_outputs(
        outputs,
        c.pending_metadata(),
        c.output,
        request.inputs,
        definition["outputs"],
        verify_content=verify_content,
    )
    c.implementations[n["id"]] = implementation
    c.execution_receipts[n["id"]] = receipt
    return {key: ref_to_legacy(value) for key, value in outputs.items()}


def reproduction(c):
    from .reproduction import build_reproduction

    return build_reproduction(c, catalog_path)


def _persist_execution_provenance(context, status, error=None):
    from .json_contract import encode_contract_json

    snapshot = context.request["operatorCatalogSnapshot"]
    value = {
        "version": 1,
        "status": status,
        "catalogDigest": snapshot["digest"],
        "workflowDigest": hashlib.sha256(
            encode_contract_json(context.request["ir"], canonical=True)
        ).hexdigest(),
        "catalogSource": snapshot["source"],
        "implementationReports": context.implementations,
        "parameterReceipts": context.execution_receipts,
        "artifactChecks": context.artifact_checks,
    }
    if error is not None:
        value["errorCode"] = getattr(error, "code", "runner.execution_failed")
    (context.output / "execution-provenance.json").write_bytes(encode_contract_json(value))


def run(request, output, emit=None):
    emit = emit or (
        lambda t, p: print(json.dumps({"eventType": t, "payload": p}, allow_nan=False), flush=True)
    )
    from .catalog_snapshot import resolve_request_catalog
    from .json_contract import encode_contract_json

    request = copy.deepcopy(request)
    request["operatorCatalogSnapshot"] = resolve_request_catalog(request, catalog_path)
    nodes, incoming, order = validate_graph(
        request["ir"], request.get("targetNodeId"), request["operatorCatalogSnapshot"]
    )
    _load_builtin_operators()
    for node_id in order:
        node = nodes[node_id]
        resolve_invocation(
            node["type"],
            request["operatorCatalogSnapshot"]["nodes"][node_id]["definition"]["version"],
            True,
        )
    emit(
        "run.catalog.accepted",
        {
            "version": 1,
            "digest": request["operatorCatalogSnapshot"]["digest"],
            "source": request["operatorCatalogSnapshot"]["source"],
        },
    )
    context = Context(request, output, emit)
    (context.output / "operator-catalog.json").write_bytes(
        encode_contract_json(request["operatorCatalogSnapshot"])
    )
    results = {}
    _persist_execution_provenance(context, "running")
    for nid in order:
        context.begin_node(nid)
        emit("node.started", {"nodeId": nid, "attempt": 1})
        try:
            inputs = {
                e["targetInput"]: results[e["source"]][e["sourceOutput"]] for e in incoming[nid]
            }
            results[nid] = execute(context, nodes[nid], inputs)
            context.commit_node()
            _persist_execution_provenance(context, "running")
            emit("node.completed", {"nodeId": nid, "outputs": results[nid]})
        except Exception as exc:
            context.rollback_node()
            _persist_execution_provenance(context, "failed", exc)
            emit(
                "node.failed",
                {
                    "nodeId": nid,
                    "errorCode": getattr(exc, "code", "runner.execution_failed"),
                    "errorMessage": str(exc),
                    "details": getattr(exc, "details", {}),
                },
            )
            raise
    _persist_execution_provenance(context, "completed")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        from .catalog_snapshot import load_runner_request

        run(load_runner_request(Path(args.request).read_bytes()), args.output)
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "eventType": "run.failed",
                    "payload": {
                        "errorCode": getattr(exc, "code", "runner.execution_failed"),
                        "errorMessage": str(exc),
                        "details": getattr(exc, "details", {}),
                    },
                }
            ),
            flush=True,
        )
        return 1
    print(json.dumps({"eventType": "run.completed", "payload": {}}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
