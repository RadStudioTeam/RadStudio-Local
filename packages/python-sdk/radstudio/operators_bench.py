"""基准对比算子：概率温度校准、双模型融合与配对模型基准汇总。

三个算子都只消费 prediction_table，不重新训练模型：
- ``TemperatureScaling`` 用单参数温度 T 把过度自信的概率拉回校准状态；
- ``EnsembleFusion`` 按 imageId 对齐两个模型，做均值/乘积/求和/投票融合；
- ``BenchmarkSummary`` 在配对测试集上逐模型算指标，并用 DeLong 与 McNemar
  检验比较两个模型，同时输出决策曲线分析（DCA）的净获益。

模块顶层只依赖 numpy/scipy/标准库；scikit-learn 只在需要 AUC 的函数内延迟导入，
保证 serve 镜像缺少 sklearn 时注册本身不受影响。
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.stats import chi2, norm

from .operator import Operator, operator

_EPS = 1e-6
_FUSION_METHODS = ("mean", "product", "sum", "vote")


def _clip_probabilities(values):
    return np.clip(np.asarray(values, dtype=float), _EPS, 1.0 - _EPS)


def _logit(values):
    clipped = _clip_probabilities(values)
    return np.log(clipped) - np.log1p(-clipped)


def _sigmoid(values):
    # tanh 形式在 |z| 很大时也不会溢出。
    return 0.5 * (1.0 + np.tanh(0.5 * np.asarray(values, dtype=float)))


def _nll(logits, labels, temperature):
    scaled = np.asarray(logits, dtype=float) / float(temperature)
    return float(np.mean(np.logaddexp(0.0, scaled) - labels * scaled))


def _brier(labels, probabilities):
    return float(np.mean((np.asarray(probabilities, dtype=float) - labels) ** 2))


def _auc(labels, scores):
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(labels, scores))


def _bin_indices(values, bins):
    return np.minimum((np.asarray(values, dtype=float) * bins).astype(int), bins - 1)


def _reliability(values, labels, bins):
    """等宽分箱的可靠性与 ECE（空箱不参与加权）。"""
    values = np.asarray(values, dtype=float)
    labels = np.asarray(labels, dtype=float)
    indices = _bin_indices(values, bins)
    edges = np.linspace(0.0, 1.0, bins + 1)
    rows = []
    ece = 0.0
    for index in range(bins):
        mask = indices == index
        count = int(mask.sum())
        row = {
            "binLower": float(edges[index]),
            "binUpper": float(edges[index + 1]),
            "count": count,
        }
        if count:
            mean_predicted = float(values[mask].mean())
            observed = float(labels[mask].mean())
            ece += count / values.size * abs(mean_predicted - observed)
            row["meanPredicted"] = mean_predicted
            row["observed"] = observed
        else:
            row["meanPredicted"] = None
            row["observed"] = None
        rows.append(row)
    return float(ece), rows


def _ece(labels, probabilities, bins=10):
    return _reliability(probabilities, labels, bins)[0]


def _records(table, name):
    if not isinstance(table, dict) or not isinstance(table.get("records"), list):
        raise TypeError(f"{name} 不是 prediction_table：缺少 records")
    return table["records"]


def _indexed(rows, name):
    index = {}
    for row in rows:
        image_id = row.get("imageId")
        if not isinstance(image_id, str) or not image_id.strip():
            raise ValueError(f"{name} 存在缺少 imageId 的记录")
        if image_id in index:
            raise ValueError(f"{name} 存在重复 imageId：{image_id}")
        index[image_id] = row
    return index


def _missing_message(name, ids):
    shown = ", ".join(sorted(ids)[:5])
    suffix = "" if len(ids) <= 5 else f" 等 {len(ids)} 个"
    return f"{name} 缺少样本 imageId：{shown}{suffix}"


def _output_evaluation(table):
    evaluation = table.get("evaluation")
    return evaluation if isinstance(evaluation, str) and evaluation else "external"


@operator("TemperatureScaling", version="1.0.0")
class TemperatureScalingOperator(Operator):
    """温度缩放：在 [0.05, 20] 上最小化二分类 NLL，输出校准后概率与校准前后指标。"""

    def run(self, ctx):
        params = ctx.parameters
        bins = int(params.get("calibrationBins", 10))
        if not 2 <= bins <= 50:
            raise ValueError("TemperatureScaling 的 calibrationBins 必须在 [2, 50] 之间")
        seed = int(params.get("seed", 42))

        table = ctx.input_json("predictions")
        rows = _records(table, "predictions")
        if not rows:
            raise ValueError("TemperatureScaling 需要非空的 predictions")

        _indexed(rows, "predictions")
        missing = [row.get("imageId") for row in rows if row.get("label") is None]
        if missing:
            raise ValueError(
                "TemperatureScaling 记录缺失标签 label：" + ", ".join(map(str, missing[:5]))
            )
        if any(row.get("probability") is None for row in rows):
            raise ValueError("TemperatureScaling 记录缺失 probability")

        labels = np.asarray([int(row["label"]) for row in rows], dtype=float)
        if set(np.unique(labels).tolist()) != {0.0, 1.0}:
            raise ValueError("TemperatureScaling 需要两个类别（二分类标签 0 与 1 必须同时存在）")

        probabilities = _clip_probabilities([float(row["probability"]) for row in rows])
        logits = _logit(probabilities)
        before_nll = _nll(logits, labels, 1.0)

        optimized = minimize_scalar(
            lambda temperature: _nll(logits, labels, temperature),
            bounds=(0.05, 20.0),
            method="bounded",
            options={"xatol": 1e-8},
        )
        temperature = float(optimized.x)
        # 优化器极少数情况下会落在比 T=1 略差的点；保证「校准后不劣于校准前」。
        if not np.isfinite(temperature) or before_nll <= _nll(logits, labels, temperature):
            temperature = 1.0

        calibrated = _sigmoid(logits / temperature)
        ece_before, _ = _reliability(probabilities, labels, bins)
        ece_after, _ = _reliability(calibrated, labels, bins)
        brier_before = _brier(labels, probabilities)
        brier_after = _brier(labels, calibrated)
        after_nll = _nll(logits, labels, temperature)

        # 图表按校准前概率分箱，同一批样本同时给出校准前/后的平均预测，方便画可靠度曲线。
        indices = _bin_indices(probabilities, bins)
        edges = np.linspace(0.0, 1.0, bins + 1)
        chart_bins = []
        for index in range(bins):
            mask = indices == index
            count = int(mask.sum())
            if count:
                chart_bins.append(
                    {
                        "binLower": float(edges[index]),
                        "binUpper": float(edges[index + 1]),
                        "count": count,
                        "meanPredictedBefore": float(probabilities[mask].mean()),
                        "meanPredictedAfter": float(calibrated[mask].mean()),
                        "observed": float(labels[mask].mean()),
                    }
                )
            else:
                chart_bins.append(
                    {
                        "binLower": float(edges[index]),
                        "binUpper": float(edges[index + 1]),
                        "count": 0,
                        "meanPredictedBefore": None,
                        "meanPredictedAfter": None,
                        "observed": None,
                    }
                )

        calibrated_table = dict(table)
        calibrated_table["evaluation"] = _output_evaluation(table)
        calibrated_table["records"] = [
            {**row, "calibratedProbability": float(value)} for row, value in zip(rows, calibrated)
        ]
        calibrated_table["calibration"] = {
            "method": "temperature_scaling",
            "temperature": temperature,
            "calibrationBins": bins,
            "seed": seed,
            "eceBefore": ece_before,
            "eceAfter": ece_after,
        }

        metrics = {
            "evaluation": table.get("evaluation"),
            "n": len(rows),
            "positives": int(labels.sum()),
            "negatives": int(len(labels) - labels.sum()),
            "temperature": temperature,
            "calibrationBins": bins,
            "seed": seed,
            "eceBefore": ece_before,
            "eceAfter": ece_after,
            "brierBefore": brier_before,
            "brierAfter": brier_after,
            "nllBefore": before_nll,
            "nllAfter": after_nll,
        }
        calibration = {"bins": chart_bins, "temperature": temperature}

        return {
            "predictions": ctx.output_json(
                "calibrated-predictions.json", calibrated_table, "prediction_table"
            ),
            "metrics": ctx.output_json("temperature-metrics.json", metrics, "metrics"),
            "calibration": ctx.output_json("temperature-calibration.json", calibration, "chart"),
        }


def _class_probabilities(rows):
    return all(isinstance(row.get("classProbabilities"), (list, tuple)) for row in rows)


def _probability_vector(rows, name):
    values = []
    for row in rows:
        probability = row.get("probability")
        if probability is None:
            raise ValueError(f"{name} 记录缺失 probability：{row.get('imageId')}")
        values.append(float(probability))
    return np.asarray(values, dtype=float)


def _class_matrix(rows, name, width=None):
    matrix = []
    for row in rows:
        vector = row.get("classProbabilities")
        if not isinstance(vector, (list, tuple)) or not vector:
            raise ValueError(f"{name} 记录的 classProbabilities 无效：{row.get('imageId')}")
        if width is not None and len(vector) != width:
            raise ValueError(
                f"{name} 记录的 classProbabilities 类别数与另一模型不一致：{row.get('imageId')}"
            )
        matrix.append([float(value) for value in vector])
    return np.asarray(matrix, dtype=float)


@operator("EnsembleFusion", version="1.0.0")
class EnsembleFusionOperator(Operator):
    """两模型融合：按 imageId 对齐后做均值/乘积/求和/投票，输出融合后的 prediction_table。"""

    def run(self, ctx):
        params = ctx.parameters
        method = str(params.get("fusionMethod", "mean"))
        if method not in _FUSION_METHODS:
            raise ValueError("EnsembleFusion 的 fusionMethod 必须是 mean/product/sum/vote")
        threshold = float(params.get("threshold", 0.5))
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("EnsembleFusion 的 threshold 必须在 [0, 1] 之间")

        table_a = ctx.input_json("predictionsA")
        table_b = ctx.input_json("predictionsB")
        rows_a = _records(table_a, "predictionsA")
        rows_b = _records(table_b, "predictionsB")
        if not rows_a:
            raise ValueError("EnsembleFusion 需要非空的 predictionsA")
        if not rows_b:
            raise ValueError("EnsembleFusion 需要非空的 predictionsB")

        index_a = _indexed(rows_a, "predictionsA")
        index_b = _indexed(rows_b, "predictionsB")
        missing_a = set(index_b) - set(index_a)
        if missing_a:
            raise ValueError(_missing_message("predictionsA", missing_a))
        missing_b = set(index_a) - set(index_b)
        if missing_b:
            raise ValueError(_missing_message("predictionsB", missing_b))

        ordered_b = [index_b[row["imageId"]] for row in rows_a]
        mismatched = [
            f"{row['imageId']}(A={row.get('label')},B={other.get('label')})"
            for row, other in zip(rows_a, ordered_b)
            if row.get("label") != other.get("label")
        ]
        if mismatched:
            raise ValueError(
                "predictionsA 与 predictionsB 的 label 不一致：" + ", ".join(mismatched[:5])
            )

        classes_a, classes_b = _class_probabilities(rows_a), _class_probabilities(rows_b)
        if classes_a != classes_b:
            raise ValueError(
                "EnsembleFusion 多分类融合要求两侧都提供 classProbabilities（不能一侧二分类一侧多分类）"
            )

        fused_records = []
        if classes_a:
            matrix_a = _class_matrix(rows_a, "predictionsA")
            matrix_b = _class_matrix(rows_b, "predictionsB", width=matrix_a.shape[1])
            if matrix_a.shape[0] != len(rows_a) or matrix_b.shape[0] != len(ordered_b):
                raise ValueError("EnsembleFusion 的 classProbabilities 行数与 records 不一致")
            mean_matrix = (matrix_a + matrix_b) / 2.0
            if method == "product":
                product = matrix_a * matrix_b
                totals = product.sum(axis=1, keepdims=True)
                fused_matrix = np.divide(
                    product,
                    totals,
                    out=np.full_like(product, 1.0 / matrix_a.shape[1]),
                    where=totals > 0,
                )
            elif method == "sum":
                summed = matrix_a + matrix_b
                totals = summed.sum(axis=1, keepdims=True)
                fused_matrix = np.divide(
                    summed,
                    totals,
                    out=np.full_like(summed, 1.0 / matrix_a.shape[1]),
                    where=totals > 0,
                )
            else:
                fused_matrix = mean_matrix
            argmax_a = matrix_a.argmax(axis=1)
            argmax_b = matrix_b.argmax(axis=1)
            if method == "vote":
                mean_argmax = mean_matrix.argmax(axis=1)
                predicted = np.where(argmax_a == argmax_b, argmax_a, mean_argmax)
            else:
                predicted = fused_matrix.argmax(axis=1)
            agreement = float(np.mean(argmax_a == argmax_b))
            for position, row in enumerate(rows_a):
                fused_records.append(
                    {
                        "imageId": row["imageId"],
                        "patientId": row.get("patientId"),
                        "label": row.get("label"),
                        "probability": float(fused_matrix[position, predicted[position]]),
                        "classProbabilities": [float(value) for value in fused_matrix[position]],
                        "predictedClass": int(predicted[position]),
                        "fusedBy": method,
                        "fold": None,
                    }
                )
        else:
            probability_a = _probability_vector(rows_a, "predictionsA")
            probability_b = _probability_vector(ordered_b, "predictionsB")
            mean_probability = (probability_a + probability_b) / 2.0
            if method == "mean":
                fused_probability = mean_probability
            elif method == "product":
                fused_probability = probability_a * probability_b
            elif method == "sum":
                # 概率求和：保留两模型证据的叠加强度，按规格不归一化。
                fused_probability = probability_a + probability_b
            else:
                vote_a = probability_a >= threshold
                vote_b = probability_b >= threshold
                tie_break = mean_probability >= threshold
                fused_probability = np.where(
                    vote_a == vote_b, vote_a.astype(float), tie_break.astype(float)
                )
            vote_probability = mean_probability if method == "vote" else fused_probability
            for position, row in enumerate(rows_a):
                fused_records.append(
                    {
                        "imageId": row["imageId"],
                        "patientId": row.get("patientId"),
                        "label": row.get("label"),
                        "probability": float(vote_probability[position]),
                        "fusedBy": method,
                        "fold": None,
                    }
                )
            agreement = float(np.mean((probability_a >= threshold) == (probability_b >= threshold)))

        fused_table = {**table_a, "records": fused_records}
        fused_table["evaluation"] = _output_evaluation(table_a)
        fused_table["fusedModels"] = 2

        limitations = ["仅支持两模型两两融合；三模型及以上需拆成多次两两融合"]
        if method == "product":
            limitations.append("product 为概率乘积，未重新归一化，宜作排序参考而非校准概率")
        if method == "sum":
            limitations.append(
                "sum 为概率直接相加：二分类不归一化，融合概率可能超过 1（多分类逐元素求和后已归一化）"
            )
        if method == "vote":
            limitations.append("vote 在平票时用均值概率按阈值判定，结果依赖 threshold 的选择")

        report = {
            "count": len(fused_records),
            "fusionMethod": method,
            "threshold": threshold,
            "agreement": agreement,
            "missing": [],
            "limitations": limitations,
        }

        return {
            "predictions": ctx.output_json(
                "fused-predictions.json", fused_table, "prediction_table"
            ),
            "report": ctx.output_json("fusion-report.json", report, "report"),
        }


def _midrank(values):
    """平均秩（ties 取中点），DeLong 的 placement value 依赖它处理并列分数。"""
    values = np.asarray(values, dtype=float)
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranks = np.empty(values.size, dtype=float)
    start = 0
    while start < values.size:
        end = start
        while end + 1 < values.size and sorted_values[end + 1] == sorted_values[start]:
            end += 1
        ranks[order[start : end + 1]] = 0.5 * (start + end) + 1.0
        start = end + 1
    return ranks


def _placement_values(scores, positive):
    """返回正/负样本的 placement values 与 AUC（等价于 Mann-Whitney 统计量）。"""
    scores = np.asarray(scores, dtype=float)
    total_ranks = _midrank(scores)
    positive_scores = scores[positive]
    negative_scores = scores[~positive]
    positive_ranks = _midrank(positive_scores)
    negative_ranks = _midrank(negative_scores)
    v10 = (total_ranks[positive] - positive_ranks) / negative_scores.size
    v01 = (total_ranks[~positive] - negative_ranks) / positive_scores.size
    return v10, v01, float(v10.mean())


def _delong_p(scores_a, scores_b, labels):
    """配对 DeLong 检验：z 检验比较两条相关 ROC 的 AUC 差异，双侧 p 值。"""
    positive = np.asarray(labels) == 1
    n_pos = int(positive.sum())
    n_neg = int((~positive).sum())
    if n_pos < 2 or n_neg < 2:
        return 1.0
    v10_a, v01_a, auc_a = _placement_values(scores_a, positive)
    v10_b, v01_b, auc_b = _placement_values(scores_b, positive)
    s10 = np.cov(np.vstack([v10_a, v10_b]), ddof=1)
    s01 = np.cov(np.vstack([v01_a, v01_b]), ddof=1)
    covariance = s10 / n_neg + s01 / n_pos
    variance = float(covariance[0, 0] + covariance[1, 1] - 2.0 * covariance[0, 1])
    if not np.isfinite(variance) or variance <= 0.0:
        # 两条 ROC 完全一致（方差为 0）时没有证据支持差异。
        return 1.0
    z_value = (auc_a - auc_b) / np.sqrt(variance)
    return float(2.0 * norm.sf(abs(z_value)))


def _mcnemar_p(correct_a, correct_b):
    """连续性校正的 McNemar 检验（1 自由度卡方）；无 discordant 对时 p=1。"""
    a_only = int(np.sum(correct_a & ~correct_b))
    b_only = int(np.sum(~correct_a & correct_b))
    discordant = a_only + b_only
    if discordant == 0:
        return 1.0, a_only, b_only
    statistic = max(0, abs(a_only - b_only) - 1) ** 2 / discordant
    return float(chi2.sf(statistic, 1)), a_only, b_only


def _binary_stats(labels, probabilities, threshold):
    predicted = probabilities >= threshold
    truth = labels == 1
    tp = int(np.sum(predicted & truth))
    fp = int(np.sum(predicted & ~truth))
    fn = int(np.sum(~predicted & truth))
    tn = int(np.sum(~predicted & ~truth))
    return {
        "n": int(labels.size),
        "auc": _auc(labels, probabilities),
        "accuracy": float((tp + tn) / labels.size),
        "sensitivity": float(tp / (tp + fn)) if tp + fn else 0.0,
        "specificity": float(tn / (tn + fp)) if tn + fp else 0.0,
        "f1": float(2 * tp / (2 * tp + fp + fn)) if 2 * tp + fp + fn else 0.0,
        "brier": _brier(labels, probabilities),
        "ece": _ece(labels, probabilities, 10),
        "positives": int(labels.sum()),
        "negatives": int(labels.size - labels.sum()),
    }


def _percentile_ci(values, level=95.0):
    if not values:
        return None
    alpha = (100.0 - level) / 2.0
    return [
        float(np.percentile(values, alpha)),
        float(np.percentile(values, 100.0 - alpha)),
    ]


def _bootstrap_cis(labels, probability_a, probability_b, threshold, resamples, seed):
    """配对 bootstrap（同一组重采样索引用于两模型），百分位法给 AUC 与 accuracy 的 95% CI。"""
    generator = np.random.default_rng(seed)
    draws = generator.integers(0, labels.size, size=(resamples, labels.size))
    result = {}
    for name, probabilities in (("A", probability_a), ("B", probability_b)):
        auc_values, accuracy_values = [], []
        for index in draws:
            sample_labels = labels[index]
            sample_probabilities = probabilities[index]
            accuracy_values.append(
                float(np.mean((sample_probabilities >= threshold) == sample_labels))
            )
            if sample_labels.min() != sample_labels.max():
                auc_values.append(_auc(sample_labels, sample_probabilities))
        result[name] = {
            "auc": _percentile_ci(auc_values),
            "accuracy": _percentile_ci(accuracy_values),
        }
    return result


def _paired_binary_tables(table_a, table_b):
    rows_a = _records(table_a, "predictionsA")
    rows_b = _records(table_b, "predictionsB")
    if not rows_a:
        raise ValueError("BenchmarkSummary 需要非空的 predictionsA")
    if not rows_b:
        raise ValueError("BenchmarkSummary 需要非空的 predictionsB")
    index_a = _indexed(rows_a, "predictionsA")
    index_b = _indexed(rows_b, "predictionsB")
    missing_a = set(index_b) - set(index_a)
    if missing_a:
        raise ValueError(_missing_message("predictionsA", missing_a))
    missing_b = set(index_a) - set(index_b)
    if missing_b:
        raise ValueError(_missing_message("predictionsB", missing_b))

    for name, rows in (("predictionsA", rows_a), ("predictionsB", rows_b)):
        no_label = [row.get("imageId") for row in rows if row.get("label") is None]
        if no_label:
            raise ValueError(
                f"BenchmarkSummary 的 {name} 记录缺失标签 label：{', '.join(map(str, no_label[:5]))}"
            )
        if any(isinstance(row.get("classProbabilities"), list) for row in rows):
            raise ValueError(
                "BenchmarkSummary 仅支持二分类配对比较，不支持带 classProbabilities 的多分类输入"
            )

    ordered_b = [index_b[row["imageId"]] for row in rows_a]
    mismatched = [
        row["imageId"]
        for row, other in zip(rows_a, ordered_b)
        if int(row["label"]) != int(other["label"])
    ]
    if mismatched:
        raise ValueError(
            "BenchmarkSummary 要求配对样本标签一致：imageId=" + ", ".join(mismatched[:5])
        )

    labels = np.asarray([int(row["label"]) for row in rows_a], dtype=int)
    if set(np.unique(labels).tolist()) != {0, 1}:
        raise ValueError("BenchmarkSummary 仅支持二分类配对比较：标签必须同时包含 0 与 1")
    probability_a = _probability_vector(rows_a, "predictionsA")
    probability_b = _probability_vector(ordered_b, "predictionsB")
    return labels, probability_a, probability_b


def _decision_curve(labels, probability_a, probability_b):
    """决策曲线分析：阈值 0.10–0.90（步长 0.01）下模型 A/B 与 treat-all/none 的净获益。

    净获益 ``NB = TP/N - FP/N * pt/(1-pt)``，其中 TP/FP 由概率按阈值 pt 二值化得到；
    treat-none 恒为 0（无人治疗，无收益也无损害）。
    """
    truth = np.asarray(labels) == 1
    total = float(truth.size)
    positives = float(truth.sum())
    negatives = total - positives
    rows = []
    differences = []
    for step in range(10, 91):
        threshold = step / 100.0
        weight = threshold / (1.0 - threshold)
        predicted_a = probability_a >= threshold
        predicted_b = probability_b >= threshold
        nb_a = float(np.sum(predicted_a & truth)) / total - (
            float(np.sum(predicted_a & ~truth)) / total * weight
        )
        nb_b = float(np.sum(predicted_b & truth)) / total - (
            float(np.sum(predicted_b & ~truth)) / total * weight
        )
        nb_all = positives / total - negatives / total * weight
        rows.append(
            {
                "threshold": threshold,
                "modelA": nb_a,
                "modelB": nb_b,
                "treatAll": nb_all,
                "treatNone": 0.0,
            }
        )
        differences.append(nb_a - nb_b)
    return rows, float(np.mean(differences))


@operator("BenchmarkSummary", version="1.0.0")
class BenchmarkSummaryOperator(Operator):
    """配对基准汇总：逐模型指标 + DeLong/McNemar 检验 + bootstrap 置信区间 + DCA。"""

    def run(self, ctx):
        params = ctx.parameters
        threshold = float(params.get("threshold", 0.5))
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("BenchmarkSummary 的 threshold 必须在 [0, 1] 之间")
        resamples = int(params.get("bootstrap", 1000))
        if not 100 <= resamples <= 10000:
            raise ValueError("BenchmarkSummary 的 bootstrap 必须在 [100, 10000] 之间")
        seed = int(params.get("seed", 42))

        labels, probability_a, probability_b = _paired_binary_tables(
            ctx.input_json("predictionsA"), ctx.input_json("predictionsB")
        )
        stats_a = _binary_stats(labels, probability_a, threshold)
        stats_b = _binary_stats(labels, probability_b, threshold)
        cis = _bootstrap_cis(labels, probability_a, probability_b, threshold, resamples, seed)
        for name, stats in (("A", stats_a), ("B", stats_b)):
            stats["aucCi95"] = cis[name]["auc"] or [stats["auc"], stats["auc"]]
            stats["accuracyCi95"] = cis[name]["accuracy"] or [
                stats["accuracy"],
                stats["accuracy"],
            ]

        correct_a = (probability_a >= threshold) == (labels == 1)
        correct_b = (probability_b >= threshold) == (labels == 1)
        mcnemar_p, a_only, b_only = _mcnemar_p(correct_a, correct_b)
        delong_p = _delong_p(probability_a, probability_b, labels)
        dca_rows, net_benefit_difference = _decision_curve(labels, probability_a, probability_b)

        metrics = {
            "n": int(labels.size),
            "threshold": threshold,
            "models": {"A": stats_a, "B": stats_b},
            "comparison": {
                "aucDifference": float(stats_a["auc"] - stats_b["auc"]),
                "delongP": delong_p,
                "mcnemarP": mcnemar_p,
                "accuracyDifference": float(stats_a["accuracy"] - stats_b["accuracy"]),
                "discordant": {"aOnly": a_only, "bOnly": b_only},
            },
            "dca": dca_rows,
        }

        report = {
            "forest": [
                {
                    "model": name,
                    "auc": stats["auc"],
                    "ciLow": stats["aucCi95"][0],
                    "ciHigh": stats["aucCi95"][1],
                }
                for name, stats in (("A", stats_a), ("B", stats_b))
            ],
            "dcaSource": (
                "决策曲线分析（DCA）：在同一批配对样本上以 0.10–0.90（步长 0.01）为阈值区间，"
                "净获益 NB = TP/N - FP/N × pt/(1-pt)，treat-all 视全体为阳性、treat-none 恒为 0"
            ),
            "netBenefitDifference": net_benefit_difference,
            "limitations": [
                "配对样本：要求两个模型在同一测试集、同一批 imageId 上逐样本配对，缺失或标签不一致直接报错",
                "同一测试集：DeLong 与 McNemar 都假设两模型共享同一批受试者，不能用于不同队列的间接比较",
                "bootstrap 为百分位法（2.5%/97.5%），按样本重采样，未按患者聚类，患者内相关会使区间偏窄",
                "DCA 的净获益依赖固定阈值区间（0.10–0.90）与样本阳性率，未做置信区间，跨数据集数值不可直接比较",
            ],
        }

        return {
            "metrics": ctx.output_json("benchmark-metrics.json", metrics, "metrics"),
            "report": ctx.output_json("benchmark-report.json", report, "report"),
        }
