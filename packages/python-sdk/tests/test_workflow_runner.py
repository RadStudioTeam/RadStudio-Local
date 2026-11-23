import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import zipfile
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


def runner():
    assert importlib.util.find_spec("radstudio.runner"), "Workflow runner is not implemented"
    from radstudio import runner

    return runner


def table(n=300):
    rng = np.random.default_rng(42)
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
                    1,
                    float(i % 3),
                ],
            }
            for i in range(n)
        ],
    }


def test_grouped_300_sample_oof_and_training_local_statistics():
    r = runner()
    t = table()
    split = r.make_split(t)
    trained = r.train(
        t,
        split,
        {"missingRateThreshold": 0.2, "varianceThreshold": 0.01, "correlationThreshold": 0.95},
        {"C": Decimal("1.0"), "maxIterations": 1000},
    )
    assert len(trained["predictions"]["records"]) == 300
    for fold, pipe in zip(split["folds"], trained["pipelines"]):
        train_ids = set(fold["trainIds"])
        test_ids = set(fold["testIds"])
        assert not train_ids & test_ids
        train_rows = [v for v in t["records"] if v["imageId"] in train_ids]
        test_rows = [v for v in t["records"] if v["imageId"] in test_ids]
        assert not {v["patientId"] for v in train_rows} & {v["patientId"] for v in test_rows}
        expected = np.median([v["values"] for v in train_rows], axis=0)
        np.testing.assert_allclose(pipe.named_steps["impute"].statistics_, expected)
    assert r.evaluate(trained["predictions"], 0.5)["metrics"]["evaluation"] == "out_of_fold"


def test_strict_ids_classes_and_split_alignment():
    r = runner()
    t = table(40)
    split = r.make_split(t)
    for field in ["patientId", "imageId"]:
        bad = copy.deepcopy(t)
        bad["records"][0][field] = ""
        with pytest.raises(ValueError):
            r.make_split(bad)
    bad = copy.deepcopy(t)
    bad["records"][0]["imageId"] = bad["records"][1]["imageId"]
    with pytest.raises(ValueError):
        r.make_split(bad)
    bad = copy.deepcopy(t)
    for row in bad["records"]:
        row["label"] = 0
    with pytest.raises(ValueError):
        r.make_split(bad)
    bad = copy.deepcopy(t)
    bad["records"][0]["label"] = 1
    with pytest.raises(ValueError, match="identity|split|patient"):
        r.train(bad, split, {}, {})


def template():
    return json.loads(
        (Path(__file__).parents[2] / "workflow-schema/examples/default-workflow.json").read_text(
            encoding="utf-8"
        )
    )


def snapshot_input(tmp_path, samples):
    """把样本列表折叠成读取节点输入快照：files 用绝对路径，行只带字段与资产角色。"""
    files = {}
    rows = []
    for index, sample in enumerate(samples):
        assets = []
        for key, role in (("imagePath", "image"), ("roiPath", "roi")):
            path = Path(sample[key])
            file_id = f"{index}-{role}{path.suffix}"
            files[file_id] = {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            assets.append({"role": role, "fileId": file_id})
        rows.append(
            {
                "rowId": sample["imageId"],
                "sampleId": sample["imageId"],
                "fields": {
                    "patientId": sample["patientId"],
                    "labelValue": sample["labelValue"],
                },
                "assets": assets,
            }
        )
    manifest = {
        "schemaVersion": "1",
        "reader": {"key": "read-images", "version": "1.0.0", "implVersion": "1.0.0"},
        "collection": {"id": "ds1", "revision": 1},
        "config": {
            "imageRole": "image",
            "roiRole": "roi",
            "requireRoi": True,
            "patientField": "patientId",
            "labelField": "labelValue",
            "positiveValue": "1",
            "negativeValue": "0",
        },
        "files": files,
        "rows": rows,
    }
    path = tmp_path / "input-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return {
        "manifestPath": str(path),
        "manifestSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("source_bound", [False, True])
def test_generated_images_full_dag_and_reproduction(tmp_path, monkeypatch, source_bound):
    r = runner()
    from types import SimpleNamespace

    from radstudio import reproduction as bundle_module

    installed = list(importlib.metadata.distributions())
    monkeypatch.setattr(
        bundle_module,
        "_installed_distributions",
        lambda: iter([*installed, SimpleNamespace(metadata={}, version="unknown")]),
    )
    rng = np.random.default_rng(5)
    samples = []
    for i in range(40):
        image = tmp_path / f"i{i}.npy"
        roi = tmp_path / f"r{i}.npy"
        np.save(image, rng.normal(100 + 30 * (i % 2), 10, (16, 16)))
        mask = np.zeros((16, 16))
        mask[2:14, 2:14] = 1
        np.save(roi, mask)
        samples.append(
            {
                "imageId": f"i{i}",
                "patientId": f"p{i}",
                "imagePath": str(image),
                "roiPath": str(roi),
                "labelValue": str(i % 2),
                "qcStatus": "passed",
            }
        )
    ir = template()
    input_snapshot = snapshot_input(tmp_path, samples)
    expected_nodes = 11
    if source_bound:
        expected_nodes = 12
        ir["nodes"].append(
            {
                "id": "feature_validation",
                "type": "ValidateFeatureTable",
                "operatorVersion": "1.0.0",
                "parameters": {},
            }
        )
        split_node = next(node for node in ir["nodes"] if node["id"] == "data_split")
        split_node["operatorVersion"] = "1.2.0"
        split_node["parameters"] = {"folds": 5, "randomSeed": 42}
        edge = next(
            edge
            for edge in ir["edges"]
            if edge["target"] == "data_split" and edge["targetInput"] == "features"
        )
        edge["source"] = "feature_validation"
        ir["edges"].append(
            {
                "source": "feature_extraction",
                "target": "feature_validation",
                "sourceOutput": "features",
                "targetInput": "features",
            }
        )

    events = []
    out = tmp_path / "out"
    result = r.run(
        {
            "runId": "r1",
            "ir": ir,
            "snapshotInputs": {"data_input": input_snapshot},
            "imageDigest": "sha256:test",
        },
        out,
        lambda typ, payload: events.append((typ, payload)),
    )
    assert len(result) == expected_nodes
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert any(a["artifactType"] == "archive" for a in manifest)
    assert sum(t == "node.completed" for t, p in events) == expected_nodes
    for a in manifest:
        assert (out / a["path"]).is_file()
    ref = result["feature_extraction"]["features"]
    assert hashlib.sha256((out / ref["path"]).read_bytes()).hexdigest() == ref["sha256"]
    xlsx = next(a for a in manifest if a["artifactType"] == "feature_table_xlsx")
    assert xlsx["path"].endswith(".xlsx")
    from openpyxl import load_workbook

    workbook = load_workbook(out / xlsx["path"])
    header = [cell.value for cell in workbook["features"][1]]
    feature_names = json.loads((out / ref["path"]).read_text(encoding="utf-8"))["featureNames"]
    assert header == ["imageId", "patientId", "label", *feature_names]
    assert workbook["features"].max_row == 1 + 40
    with zipfile.ZipFile(
        out / next(a["path"] for a in manifest if a["artifactType"] == "archive")
    ) as z:
        assert {"reproduce.py", "requirements.lock", "request.json", "radstudio/runner.py"} <= set(
            z.namelist()
        )
    # Replay the complete generated-image workflow from the extracted package, outside source imports.
    import os
    import shutil
    import subprocess
    import sys

    detached = tmp_path / "detached-reproduction"
    detached.mkdir()
    archive_path = out / next(a["path"] for a in manifest if a["artifactType"] == "archive")
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(detached)
    input_dir = tmp_path / "separate-replay-inputs"
    input_dir.mkdir()
    by_digest = {
        hashlib.sha256(Path(sample[key]).read_bytes()).hexdigest(): Path(sample[key])
        for sample in samples
        for key in ("imagePath", "roiPath")
    }
    for item in json.loads((detached / "input-hashes.json").read_text(encoding="utf-8")):
        source = by_digest[item["sha256"]]
        target = input_dir / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    replayed = tmp_path / "replayed-full-workflow"
    process = subprocess.run(
        [
            sys.executable,
            str(detached / "reproduce.py"),
            "--dataset-dir",
            str(input_dir),
            "--output",
            str(replayed),
        ],
        cwd=detached,
        env={**os.environ, "PYTHONPATH": str(detached), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    replay_events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    assert sum(event["eventType"] == "node.completed" for event in replay_events) == expected_nodes
    replay_manifest = json.loads((replayed / "manifest.json").read_text(encoding="utf-8"))
    assert all((replayed / item["path"]).is_file() for item in replay_manifest)
    metrics_path = next(item["path"] for item in manifest if item["artifactType"] == "metrics")
    original_metrics = json.loads((out / metrics_path).read_text(encoding="utf-8"))
    replay_metrics = json.loads((replayed / metrics_path).read_text(encoding="utf-8"))
    assert replay_metrics["auc"] == pytest.approx(original_metrics["auc"], abs=1e-12)
    replay_provenance = json.loads(
        (replayed / "execution-provenance.json").read_text(encoding="utf-8")
    )
    original_provenance = json.loads(
        (out / "execution-provenance.json").read_text(encoding="utf-8")
    )
    assert replay_provenance["catalogDigest"] == original_provenance["catalogDigest"]
    assert replay_provenance["workflowDigest"] == original_provenance["workflowDigest"]
    assert replay_provenance["status"] == "completed"
    if source_bound:
        from radstudio.content_contract import validate_content_file

        original_split = out / result["data_split"]["split"]["path"]
        replay_split = replayed / result["data_split"]["split"]["path"]
        validate_content_file(
            original_split,
            "split_plan",
            {"id": "radstudio/split-plan", "version": "1"},
            reference_roots=[out],
        )
        validate_content_file(
            replay_split,
            "split_plan",
            {"id": "radstudio/split-plan", "version": "1"},
            reference_roots=[replayed],
        )

    events = []
    partial = r.run(
        {
            "runId": "r2",
            "ir": ir,
            "snapshotInputs": {"data_input": input_snapshot},
            "targetNodeId": "roi_validation",
        },
        tmp_path / "partial",
        lambda t, p: events.append((t, p)),
    )
    assert set(partial) == {"data_input", "image_conversion", "quality_control", "roi_validation"}


def test_image_format_conversion_grayscales_rgb_images(tmp_path):
    r = runner()
    rng = np.random.default_rng(7)
    samples = []
    for i in range(4):
        image = tmp_path / f"rgb{i}.png"
        roi = tmp_path / f"rgb{i}_mask.png"
        color = rng.integers(0, 255, (20, 24, 3), dtype=np.uint8)
        Image.fromarray(color).save(image)
        mask = np.zeros((20, 24), dtype=np.uint8)
        mask[4:16, 5:19] = 1
        Image.fromarray(mask).save(roi)
        samples.append(
            {
                "imageId": f"i{i}",
                "patientId": f"p{i}",
                "imagePath": str(image),
                "roiPath": str(roi),
                "labelValue": str(i % 2),
                "qcStatus": "passed",
            }
        )
    ir = {
        "schemaVersion": "2",
        "name": "conversion",
        "nodes": [
            {
                "id": "data_input",
                "type": "ReadImages",
                "name": "影像读取",
                "operatorVersion": "1.0.0",
                "parameters": {},
            },
            {
                "id": "image_conversion",
                "type": "ImageFormatConversion",
                "name": "影像格式转换",
                "operatorVersion": "1.0.0",
                "parameters": {"imageMode": "grayscale", "roiMode": "binary"},
            },
        ],
        "edges": [
            {
                "source": "data_input",
                "target": "image_conversion",
                "sourceOutput": "samples",
                "targetInput": "samples",
            }
        ],
    }
    out = tmp_path / "out"
    result = r.run(
        {
            "runId": "r1",
            "ir": ir,
            "snapshotInputs": {"data_input": snapshot_input(tmp_path, samples)},
            "targetNodeId": "image_conversion",
            "imageDigest": "sha256:test",
        },
        out,
        lambda t, p: None,
    )
    converted = result["image_conversion"]["samples"]
    payload = json.loads((out / converted["path"]).read_text(encoding="utf-8"))
    assert len(payload) == 4
    for row in payload:
        with Image.open(row["imagePath"]) as im:
            assert im.mode == "L"
            assert np.asarray(im).ndim == 2
        assert np.asarray(Image.open(row["roiPath"])).ndim == 2
