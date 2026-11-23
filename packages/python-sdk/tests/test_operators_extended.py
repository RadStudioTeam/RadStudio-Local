"""P0 扩展节点：可配置 CV、留出集、外部验证、图表与报告。"""

import io
import json
import zipfile

import numpy as np
import pytest

from radstudio.operator import (
    BundleArtifactStore,
    OperatorRequest,
    invoke,
    resolve_operator,
)
from radstudio.operator_serve import load_builtin_operators

load_builtin_operators()


def table(n=200):
    rng = np.random.default_rng(11)
    return {
        "featureNames": ["signal", "noise", "constant", "duplicate"],
        "records": [
            {
                "imageId": f"i{i}",
                "patientId": f"p{i // 2}",
                "label": (i // 2) % 2,
                "values": [
                    float((i // 2) % 2 + rng.normal()),
                    float(rng.normal()),
                    1.0,
                    float(i % 3),
                ],
            }
            for i in range(n)
        ],
    }


def request(node, operator_type, parameters, inputs):
    return OperatorRequest(
        requestId=f"req-{node}",
        runId="run-extended",
        nodeId=node,
        operatorType=operator_type,
        parameters=parameters,
        inputs=inputs,
    )


def completed(result):
    assert result.status == "completed", result.error
    return result.outputs


def test_extended_operators_registered():
    for operator_type, version in [
        ("DataSplit", "1.1.0"),
        ("FeatureFilter", "1.1.0"),
        ("HoldoutSplit", "1.0.0"),
        ("ModelApply", "1.0.0"),
        ("Evaluation", "1.1.0"),
        ("PaperFigures", "1.0.0"),
        ("Report", "1.1.0"),
        ("DLCNNInference", "1.1.0"),
    ]:
        cls = resolve_operator(operator_type, version)
        assert (cls.operator_type, cls.operator_version) == (operator_type, version)


def test_holdout_external_validation_and_publication_figures(tmp_path):
    store = BundleArtifactStore(tmp_path / "workspace")
    features = store.write_json("features.json", table(), "feature_table")

    split = completed(
        invoke(
            request(
                "split",
                "DataSplit",
                {"folds": 3, "randomSeed": 7},
                {"features": features},
            ),
            store,
        )
    )
    plan = store.read_json(split["split"])
    assert len(plan["folds"]) == 3 and plan["seed"] == 7

    strategy = completed(
        invoke(
            request(
                "filter",
                "FeatureFilter",
                {
                    "method": "univariate",
                    "missingRateThreshold": 0.2,
                    "varianceThreshold": 0.01,
                    "correlationThreshold": 0.95,
                    "univariateTopK": 2,
                },
                {},
            ),
            store,
        )
    )
    trained = completed(
        invoke(
            request(
                "train",
                "LogisticRegression",
                {"C": 1.0, "maxIterations": 1000},
                {
                    "features": features,
                    "split": split["split"],
                    "strategy": strategy["strategy"],
                },
            ),
            store,
        )
    )
    model = store.read_json(trained["model"])
    assert model["kind"] == "cross_validation_ensemble"
    assert model["foldCount"] == 3

    holdout = completed(
        invoke(
            request(
                "holdout",
                "HoldoutSplit",
                {
                    "validationRatio": 0.2,
                    "testRatio": 0.2,
                    "randomSeed": 3,
                    "stratify": True,
                },
                {"features": features},
            ),
            store,
        )
    )
    holdout_plan = store.read_json(holdout["split"])
    assert holdout_plan["scheme"] == "holdout"
    partitions = holdout_plan["partitions"]
    patient_sets = {}
    for name in ("train", "validation", "test"):
        part = store.read_json(holdout[name])
        assert part["featureNames"] == table()["featureNames"]
        assert {row["imageId"] for row in part["records"]} == set(partitions[name])
        labels = {row["label"] for row in part["records"]}
        assert labels == {0, 1}
        patient_sets[name] = {row["patientId"] for row in part["records"]}
    assert not patient_sets["train"] & patient_sets["validation"]
    assert not patient_sets["train"] & patient_sets["test"]
    assert not patient_sets["validation"] & patient_sets["test"]

    external = completed(
        invoke(
            request(
                "apply",
                "ModelApply",
                {},
                {"model": trained["model"], "features": holdout["test"]},
            ),
            store,
        )
    )
    predictions = store.read_json(external["predictions"])
    assert predictions["evaluation"] == "external"
    assert len(predictions["records"]) == len(partitions["test"])

    evaluation = completed(
        invoke(
            request(
                "eval",
                "Evaluation",
                {
                    "threshold": 0.5,
                    "thresholdMode": "youden",
                    "bootstrap": 200,
                    "ciLevel": 0.95,
                    "calibrationBins": 8,
                    "seed": 5,
                },
                {"predictions": external["predictions"]},
            ),
            store,
        )
    )
    metrics = store.read_json(evaluation["metrics"])
    assert metrics["evaluation"] == "external"
    assert metrics["n"] == len(predictions["records"])
    assert 0 <= metrics["auc"] <= 1
    assert metrics["ci"]["auc"][0] <= metrics["auc"] <= metrics["ci"]["auc"][1]
    assert store.read_json(evaluation["calibration"])["brier"] is not None
    assert store.read_json(evaluation["decisionCurve"])["thresholds"]
    json.dumps(metrics, allow_nan=False)

    figures = completed(
        invoke(
            request(
                "figures",
                "PaperFigures",
                {
                    "figures": ["roc", "pr", "calibration", "decisionCurve", "confusion", "forest"],
                    "formats": ["png", "pdf", "svg", "tiff"],
                    "dpi": 120,
                    "style": "nature",
                    "title": "External validation",
                },
                {
                    "metrics": evaluation["metrics"],
                    "roc": evaluation["roc"],
                    "pr": evaluation["pr"],
                    "calibration": evaluation["calibration"],
                    "decisionCurve": evaluation["decisionCurve"],
                    "confusion": evaluation["confusion"],
                },
            ),
            store,
        )
    )
    archive_bytes = store.read_bytes(figures["figures"])
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        for kind in ("roc", "pr", "calibration", "decisionCurve", "confusion", "forest"):
            assert f"figures/{kind}.png" in names
            assert f"figures/{kind}.pdf" in names
            assert f"figures/{kind}.svg" in names
            assert f"figures/{kind}.tiff" in names
        assert "figures/composite.png" in names
        manifest = json.loads(archive.read("figures/manifest.json"))
        assert len(manifest["captions"]) == 6
    assert store.read_bytes(figures["preview"]).startswith(b"%PDF")

    report = completed(
        invoke(
            request(
                "report",
                "Report",
                {"title": "External validation study", "language": "en"},
                {
                    "metrics": evaluation["metrics"],
                    "model": trained["model"],
                    "figures": figures["figures"],
                    "captions": figures["captions"],
                    "decisionCurve": evaluation["decisionCurve"],
                },
            ),
            store,
        )
    )
    html = store.read_bytes(report["report"]).decode("utf-8")
    assert "data:image/png;base64," in html
    assert "External validation study" in html
    markdown = store.read_bytes(report["summary"]).decode("utf-8")
    assert "| Metric | Value |" in markdown
    checklist = store.read_json(report["checklist"])
    assert checklist["externalValidation"] is True
    assert checklist["hasFigures"] is True


def test_holdout_rejects_insufficient_or_invalid_ratios():
    from radstudio import runner

    with pytest.raises(ValueError):
        runner.make_holdout(table(), validation_ratio=0.5, test_ratio=0.4)
    with pytest.raises(ValueError):
        runner.make_holdout(table(4), validation_ratio=0.3, test_ratio=0.3)


def test_paper_figures_missing_inputs_render_placeholders(tmp_path):
    store = BundleArtifactStore(tmp_path / "workspace")
    figures = completed(
        invoke(
            request(
                "figures-empty",
                "PaperFigures",
                {"figures": ["roc", "calibration"], "formats": ["png"]},
                {},
            ),
            store,
        )
    )
    with zipfile.ZipFile(io.BytesIO(store.read_bytes(figures["figures"]))) as archive:
        assert "figures/roc.png" in archive.namelist()
        assert "figures/calibration.png" in archive.namelist()
