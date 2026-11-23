"""出版级图表与报告算子。

PaperFigures 只消费上游 JSON 产物，不读取数据集文件，因此可直接运行在
单体 runner 和云函数 serve 模式。所有图同时输出 PNG（供网页/报告内嵌）
与用户选择的出版格式；preview.pdf 用于网页内预览。
"""

from __future__ import annotations

import base64
import io
import json
import math
import zipfile

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from openpyxl import Workbook

from .operator import Operator, operator

_FIGURE_TITLES = {
    "roc": "ROC curve",
    "pr": "Precision-recall curve",
    "calibration": "Calibration curve",
    "decisionCurve": "Decision curve analysis",
    "confusion": "Confusion matrix",
    "forest": "Performance summary",
}
_FORMATS = {"png", "pdf", "svg", "tiff"}


def _metric(metrics, key, default=None):
    return (metrics or {}).get(key, default)


def _fmt(value, digits=3):
    if value is None or isinstance(value, (dict, list)):
        return "NA"
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        if math.isnan(float(value)) or math.isinf(float(value)):
            return "NA"
        return f"{float(value):.{digits}f}"
    return str(value)


def _plot_roc(ax, data):
    if not data:
        ax.text(0.5, 0.5, "ROC data not available", ha="center", va="center")
        return
    ax.plot([0, 1], [0, 1], linestyle="--", linewidth=0.8, color="#8c8c8c")
    if "fpr" in data:
        ax.plot(data["fpr"], data["tpr"], linewidth=1.6, color="#0b6e99")
        if data.get("auc") is not None:
            ax.text(0.55, 0.12, f"AUC = {_fmt(data['auc'])}", color="#0b6e99")
    for curve in data.get("curves") or []:
        ax.plot(
            curve.get("fpr", []),
            curve.get("tpr", []),
            linewidth=1.2,
            label=f"Class {curve.get('class')} (AUC {_fmt(curve.get('auc'))})",
        )
    if data.get("curves"):
        ax.legend(loc="lower right", frameon=False, fontsize=7)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("1 - Specificity")
    ax.set_ylabel("Sensitivity")
    ax.set_title(_FIGURE_TITLES["roc"])


def _plot_pr(ax, data):
    if not data:
        ax.text(0.5, 0.5, "PR data not available", ha="center", va="center")
        return
    if "precision" in data:
        ax.plot(data["recall"], data["precision"], linewidth=1.6, color="#0b6e99")
        if data.get("averagePrecision") is not None:
            ax.text(0.06, 0.12, f"AP = {_fmt(data['averagePrecision'])}", color="#0b6e99")
    for curve in data.get("curves") or []:
        ax.plot(
            curve.get("recall", []),
            curve.get("precision", []),
            linewidth=1.2,
            label=f"Class {curve.get('class')} (AP {_fmt(curve.get('averagePrecision'))})",
        )
    if data.get("curves"):
        ax.legend(loc="lower left", frameon=False, fontsize=7)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_title(_FIGURE_TITLES["pr"])


def _plot_calibration(ax, data):
    if not data or not data.get("bins"):
        ax.text(0.5, 0.5, "Calibration data not available", ha="center", va="center")
        return
    predicted = [point["predicted"] for point in data["bins"]]
    observed = [point["observed"] for point in data["bins"]]
    ax.plot([0, 1], [0, 1], linestyle="--", linewidth=0.8, color="#8c8c8c")
    ax.plot(predicted, observed, marker="o", linewidth=1.5, color="#0b6e99")
    text = f"Brier = {_fmt(data.get('brier'))}"
    if data.get("slope") is not None:
        text += f"  slope = {_fmt(data['slope'])}"
    ax.text(0.04, 0.92, text, transform=ax.transAxes, fontsize=8, color="#0b6e99")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Predicted probability")
    ax.set_ylabel("Observed proportion")
    ax.set_title(_FIGURE_TITLES["calibration"])


def _plot_decision_curve(ax, data):
    thresholds = (data or {}).get("thresholds") or []
    if not thresholds:
        ax.text(0.5, 0.5, "Decision curve data not available", ha="center", va="center")
        return
    ax.plot(thresholds, data.get("model", []), linewidth=1.6, color="#0b6e99", label="Model")
    ax.plot(
        thresholds,
        data.get("treatAll", []),
        linewidth=1.0,
        linestyle="--",
        color="#bd5b00",
        label="Treat all",
    )
    ax.plot(
        thresholds,
        data.get("treatNone", []),
        linewidth=1.0,
        linestyle=":",
        color="#555555",
        label="Treat none",
    )
    ax.axhline(0, linewidth=0.6, color="#bbbbbb")
    ax.set_xlim(0, 1)
    ax.set_xlabel("Threshold probability")
    ax.set_ylabel("Net benefit")
    ax.set_title(_FIGURE_TITLES["decisionCurve"])
    ax.legend(frameon=False, fontsize=7)


def _plot_confusion(ax, data):
    matrix = np.asarray((data or {}).get("matrix") or [])
    if matrix.ndim != 2 or matrix.size == 0:
        ax.text(0.5, 0.5, "Confusion matrix not available", ha="center", va="center")
        return
    labels = (data or {}).get("labels") or list(range(matrix.shape[0]))
    image = ax.imshow(matrix, cmap="Blues")
    ax.figure.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
    for row in range(matrix.shape[0]):
        for col in range(matrix.shape[1]):
            value = int(matrix[row, col])
            color = "white" if value > matrix.max() / 2 else "#222222"
            ax.text(col, row, str(value), ha="center", va="center", color=color, fontsize=9)
    ax.set_xticks(range(len(labels)), [str(label) for label in labels])
    ax.set_yticks(range(len(labels)), [str(label) for label in labels])
    ax.set_xlabel("Predicted")
    ax.set_ylabel("Actual")
    ax.set_title(_FIGURE_TITLES["confusion"])


def _plot_forest(ax, metrics):
    metrics = metrics or {}
    rows = []
    for key, label in (
        ("auc", "AUC"),
        ("sensitivity", "Sensitivity"),
        ("specificity", "Specificity"),
        ("accuracy", "Accuracy"),
        ("ppv", "PPV"),
        ("npv", "NPV"),
    ):
        value = metrics.get(key)
        if value is None:
            continue
        ci = (metrics.get("ci") or {}).get(key)
        rows.append((label, float(value), ci))
    if not rows:
        ax.text(0.5, 0.5, "Metrics not available", ha="center", va="center")
        return
    positions = np.arange(len(rows))
    values = [row[1] for row in rows]
    lower = [row[1] - row[2][0] if row[2] else 0 for row in rows]
    upper = [row[2][1] - row[1] if row[2] else 0 for row in rows]
    ax.errorbar(
        values,
        positions,
        xerr=[lower, upper],
        fmt="o",
        color="#0b6e99",
        ecolor="#555555",
        capsize=3,
        linewidth=1.2,
    )
    for position, value in zip(positions, values):
        ax.text(min(1.02, value + 0.03), position, _fmt(value), va="center", fontsize=8)
    ax.set_yticks(positions, [row[0] for row in rows])
    ax.set_xlim(0, 1.05)
    ax.set_xlabel("Estimate")
    ax.set_title(_FIGURE_TITLES["forest"])


def _plot(kind, ax, inputs):
    if kind == "roc":
        _plot_roc(ax, inputs.get("roc"))
    elif kind == "pr":
        _plot_pr(ax, inputs.get("pr"))
    elif kind == "calibration":
        _plot_calibration(ax, inputs.get("calibration"))
    elif kind == "decisionCurve":
        _plot_decision_curve(ax, inputs.get("decisionCurve"))
    elif kind == "confusion":
        _plot_confusion(ax, inputs.get("confusion"))
    elif kind == "forest":
        _plot_forest(ax, inputs.get("metrics"))


def _captions(selected, inputs):
    metrics = inputs.get("metrics") or {}
    captions = []
    for kind in selected:
        if kind == "roc":
            captions.append(
                {
                    "id": kind,
                    "caption": f"Receiver operating characteristic curve for {metrics.get('evaluation', 'evaluation')} predictions"
                    + (f" (AUC = {_fmt(metrics.get('auc'))})." if metrics.get("auc") is not None else "."),
                }
            )
        elif kind == "pr":
            captions.append(
                {
                    "id": kind,
                    "caption": "Precision-recall curve"
                    + (f" (average precision = {_fmt(metrics.get('prAuc'))})." if metrics.get("prAuc") is not None else "."),
                }
            )
        elif kind == "calibration":
            captions.append(
                {
                    "id": kind,
                    "caption": "Calibration curve"
                    + (f" (Brier score = {_fmt(metrics.get('brier'))})." if metrics.get("brier") is not None else "."),
                }
            )
        elif kind == "decisionCurve":
            captions.append(
                {"id": kind, "caption": "Decision curve analysis showing net benefit across threshold probabilities."}
            )
        elif kind == "confusion":
            captions.append(
                {"id": kind, "caption": f"Confusion matrix at the selected operating threshold ({_fmt(metrics.get('threshold'))})."}
            )
        elif kind == "forest":
            captions.append(
                {"id": kind, "caption": "Summary of discrimination and classification performance with bootstrap confidence intervals where available."}
            )
    return captions


def _source_workbook(inputs):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "metrics"
    metrics = inputs.get("metrics") or {}
    sheet.append(["metric", "value"])
    for key, value in metrics.items():
        if isinstance(value, (dict, list)):
            continue
        sheet.append([key, value])
    roc = inputs.get("roc") or {}
    if "fpr" in roc:
        curve = workbook.create_sheet("roc")
        curve.append(["fpr", "tpr"])
        for fpr, tpr in zip(roc.get("fpr", []), roc.get("tpr", [])):
            curve.append([fpr, tpr])
    pr = inputs.get("pr") or {}
    if "precision" in pr:
        curve = workbook.create_sheet("pr")
        curve.append(["recall", "precision"])
        for recall, precision in zip(pr.get("recall", []), pr.get("precision", [])):
            curve.append([recall, precision])
    calibration = inputs.get("calibration") or {}
    if calibration.get("bins"):
        curve = workbook.create_sheet("calibration")
        curve.append(["predicted", "observed", "count"])
        for point in calibration["bins"]:
            curve.append([point.get("predicted"), point.get("observed"), point.get("count")])
    decision = inputs.get("decisionCurve") or {}
    if decision.get("thresholds"):
        curve = workbook.create_sheet("decisionCurve")
        curve.append(["threshold", "model", "treatAll", "treatNone"])
        for row in zip(
            decision.get("thresholds", []),
            decision.get("model", []),
            decision.get("treatAll", []),
            decision.get("treatNone", []),
        ):
            curve.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@operator("PaperFigures", version="1.0.0")
class PaperFiguresOperator(Operator):
    """生成出版级图表、图注和源数据。"""

    def run(self, ctx):
        p = ctx.parameters
        selected = list(p.get("figures") or ["roc", "calibration", "decisionCurve", "confusion"])
        formats = [fmt for fmt in (p.get("formats") or ["png", "pdf"]) if fmt in _FORMATS]
        if "png" not in formats:
            formats.append("png")
        dpi = int(p.get("dpi", 300))
        style = str(p.get("style", "default"))
        if style == "nature":
            plt.rcParams.update(
                {
                    "font.family": "DejaVu Sans",
                    "font.size": 8,
                    "axes.linewidth": 0.6,
                    "xtick.major.width": 0.6,
                    "ytick.major.width": 0.6,
                }
            )
        elif style == "ieee":
            plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9})
        inputs = {
            key: ctx.input_json(key)
            for key in ("metrics", "roc", "pr", "calibration", "decisionCurve", "confusion")
            if key in ctx.inputs
        }
        captions = _captions(selected, inputs)
        archive = io.BytesIO()
        pdf_buffer = io.BytesIO()
        generated = []
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle, PdfPages(
            pdf_buffer, metadata={"Title": str(p.get("title") or "RadStudio figures")}
        ) as pdf:
            for kind in selected:
                fig, ax = plt.subplots(figsize=(4.2, 3.6))
                _plot(kind, ax, inputs)
                fig.tight_layout()
                for fmt in formats:
                    name = f"figures/{kind}.{fmt}"
                    buffer = io.BytesIO()
                    fig.savefig(
                        buffer,
                        format=fmt,
                        dpi=dpi,
                        bbox_inches="tight",
                    )
                    bundle.writestr(name, buffer.getvalue())
                    generated.append(name)
                pdf.savefig(fig)
                plt.close(fig)
            if selected:
                columns = 2
                rows_count = math.ceil(len(selected) / columns)
                composite, axes = plt.subplots(
                    rows_count, columns, figsize=(8.2, 3.4 * rows_count), squeeze=False
                )
                for index, kind in enumerate(selected):
                    _plot(kind, axes[index // columns][index % columns], inputs)
                for index in range(len(selected), rows_count * columns):
                    axes[index // columns][index % columns].axis("off")
                composite.tight_layout()
                for fmt in formats:
                    name = f"figures/composite.{fmt}"
                    buffer = io.BytesIO()
                    composite.savefig(
                        buffer,
                        format=fmt,
                        dpi=dpi,
                        bbox_inches="tight",
                    )
                    bundle.writestr(name, buffer.getvalue())
                    generated.append(name)
                pdf.savefig(composite)
                plt.close(composite)
            bundle.writestr(
                "figures/manifest.json",
                json.dumps(
                    {
                        "figures": generated,
                        "captions": captions,
                        "dpi": dpi,
                        "formats": formats,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        source = _source_workbook(inputs)
        return {
            "figures": ctx.output_bytes("figures.zip", archive.getvalue(), "figure_bundle"),
            "preview": ctx.output_bytes("figures.pdf", pdf_buffer.getvalue(), "figure_bundle"),
            "captions": ctx.output_json(
                "captions.json",
                {"captions": captions, "title": p.get("title") or "RadStudio figures"},
                "report",
            ),
            "sourceData": ctx.output_bytes("source-data.xlsx", source, "table"),
        }


def _escape(value):
    import html

    return html.escape(str(value))


@operator("Report", version="1.1.0")
class ReportOperatorV11(Operator):
    """论文式 HTML/Markdown 报告；不依赖单体 runner 的复现包上下文。"""

    def run(self, ctx):
        p = ctx.parameters
        metrics = ctx.input_json("metrics")
        model = ctx.input_json("model") if "model" in ctx.inputs else None
        details = ctx.input_json("report") if "report" in ctx.inputs else None
        captions = ctx.input_json("captions") if "captions" in ctx.inputs else {"captions": []}
        decision = ctx.input_json("decisionCurve") if "decisionCurve" in ctx.inputs else {}
        figure_images = []
        if "figures" in ctx.inputs:
            try:
                with zipfile.ZipFile(io.BytesIO(ctx.input_bytes("figures"))) as bundle:
                    for name in sorted(bundle.namelist()):
                        if name.lower().endswith(".png"):
                            figure_images.append(
                                (name, base64.b64encode(bundle.read(name)).decode("ascii"))
                            )
            except zipfile.BadZipFile as error:
                raise ValueError("Report figures input must be a PaperFigures ZIP") from error
        title = str(p.get("title") or "RadStudio imaging study report")
        language = str(p.get("language") or "en")
        heading = "研究结果报告" if language == "zh" else "Study report"
        rows = "".join(
            f"<tr><th>{_escape(key)}</th><td>{_escape(_fmt(value))}</td></tr>"
            for key, value in metrics.items()
            if not isinstance(value, (dict, list))
        )
        images = "".join(
            f'<figure><img src="data:image/png;base64,{data}" alt="{_escape(name)}"/>'
            f"<figcaption>{_escape(name)}</figcaption></figure>"
            for name, data in figure_images
            if not name.endswith("composite.png")
        )
        if not images:
            images = "".join(
                f'<figure><img src="data:image/png;base64,{data}" alt="{_escape(name)}"/>'
                f"<figcaption>{_escape(name)}</figcaption></figure>"
                for name, data in figure_images
            )
        caption_html = "".join(
            f"<li>{_escape(item.get('caption', ''))}</li>"
            for item in (captions.get("captions") or [])
        )
        report = f"""<!doctype html>
<html lang="{_escape(language)}"><meta charset="utf-8">
<title>{_escape(title)}</title>
<body>
<h1>{_escape(heading)}</h1>
<p>{_escape(title)}</p>
<h2>Performance</h2>
<table>{rows}</table>
<h2>Figures</h2>
{images}
<h2>Captions</h2><ul>{caption_html}</ul>
<h2>Model</h2><pre>{_escape(json.dumps(model, ensure_ascii=False, indent=2) if model else 'Not provided')}</pre>
<h2>Quality control</h2><pre>{_escape(json.dumps(details, ensure_ascii=False, indent=2) if details else 'Not provided')}</pre>
</body></html>"""
        summary = [
            f"# {title}",
            "",
            "## Performance",
            "",
            "| Metric | Value |",
            "| --- | --- |",
        ]
        summary.extend(
            f"| {key} | {_fmt(value)} |"
            for key, value in metrics.items()
            if not isinstance(value, (dict, list))
        )
        summary.extend(["", "## Figures", ""])
        summary.extend(f"- {item.get('caption', '')}" for item in (captions.get("captions") or []))
        checklist = {
            "externalValidation": metrics.get("evaluation") == "external",
            "hasConfidenceIntervals": bool(metrics.get("ci")),
            "hasCalibration": metrics.get("brier") is not None,
            "hasDecisionCurve": bool((decision or {}).get("thresholds")),
            "hasFigures": bool(figure_images),
            "hasModel": model is not None,
            "hasQualityControl": details is not None,
            "evaluationType": metrics.get("evaluation"),
        }
        return {
            "report": ctx.output_bytes("report.html", report.encode("utf-8"), "report"),
            "summary": ctx.output_bytes(
                "summary.md", ("\n".join(summary) + "\n").encode("utf-8"), "report"
            ),
            "checklist": ctx.output_json("checklist.json", checklist, "report"),
        }


# ---------------------------------------------------------------------------
# 消费端绑定：论文报告的 model 输入声明 radstudio/model@2。这一版只把模型信封原样印进
# 正文的 Model 段落，不读取其中任何键，所以 run 与 1.1.0 是同一个函数对象；绑定改变的是
# 建图阶段接受哪代血缘（接上模型才判，model 仍选填），报告内容逐字节不变。
# ---------------------------------------------------------------------------


@operator("Report", version="1.2.0")
class ReportOperatorV12(ReportOperatorV11):
    """论文报告：model 输入绑定 radstudio/model@2 的声明版本，报告与 1.1.0 相同。"""
