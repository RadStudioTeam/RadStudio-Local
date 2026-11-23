"""Frozen definitions and independently runnable SDK reproduction archives."""

import copy
import hashlib
import io
import json
import zipfile

import pytest

from radstudio import runner
from radstudio.catalog_snapshot import DEFINITION_FIELDS
from radstudio.json_contract import canonical_manifest_json


def request(tmp_path):
    image = tmp_path / "image.npy"
    image.write_bytes(b"synthetic image bytes")
    roi = tmp_path / "roi.npy"
    roi.write_bytes(b"synthetic mask bytes")
    manifest = {
        "schemaVersion": "1",
        "reader": {"key": "read-images", "version": "1.0.0", "implVersion": "1.0.0"},
        "collection": {"id": "ds", "revision": 1},
        "config": {
            "imageRole": "image",
            "roiRole": "roi",
            "requireRoi": False,
            "patientField": "patientId",
            "labelField": "labelValue",
            "positiveValue": "1",
            "negativeValue": "0",
        },
        "files": {
            "image.npy": {
                "path": str(image),
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            },
            "roi.npy": {
                "path": str(roi),
                "sha256": hashlib.sha256(roi.read_bytes()).hexdigest(),
            },
        },
        "rows": [
            {
                "rowId": "r1",
                "sampleId": "i",
                "fields": {"patientId": "p", "labelValue": "1"},
                "assets": [
                    {"role": "image", "fileId": "image.npy"},
                    {"role": "roi", "fileId": "roi.npy"},
                ],
            }
        ],
    }
    manifest_path = tmp_path / "input-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return {
        "runId": "frozen-catalog",
        "ir": {
            "schemaVersion": "2",
            "nodes": [
                {
                    "id": "data",
                    "type": "ReadImages",
                    "operatorVersion": "1.0.0",
                    "parameters": {
                        "dataCollectionId": "coll",
                        "readerConfigId": "cfg",
                    },
                }
            ],
            "edges": [],
        },
        "snapshotInputs": {
            "data": {
                "manifestPath": str(manifest_path),
                "manifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            }
        },
    }


def snapshot(value):
    definition = next(
        item
        for item in json.loads(runner.catalog_path().read_text(encoding="utf-8"))
        if item["type"] == "ReadImages" and item["version"] == "1.0.0"
    )
    body = {
        "version": 1,
        "source": "installed-catalog",
        "rules": {
            "parameterDialect": "radstudio-parameters/1",
            "implementationSelection": "exact-v1",
        },
        "nodes": {
            "data": {
                "requestedType": "ReadImages",
                "requestedVersion": "1.0.0",
                "definition": {
                    key: copy.deepcopy(value)
                    for key, value in definition.items()
                    if key in DEFINITION_FIELDS
                },
            }
        },
    }
    return {**body, "digest": hashlib.sha256(canonical_manifest_json(body)).hexdigest()}


def test_supplied_catalog_does_not_read_live_directory(tmp_path, monkeypatch):
    value = request(tmp_path)
    value["operatorCatalogSnapshot"] = snapshot(value)

    def forbidden():
        raise AssertionError("frozen replay read installed catalog")

    monkeypatch.setattr(runner, "catalog_path", forbidden)
    results = runner.run(value, tmp_path / "output", lambda *_: None)
    assert "data" in results


def test_corrupt_catalog_fails_before_output(tmp_path):
    value = request(tmp_path)
    value["operatorCatalogSnapshot"] = snapshot(value)
    value["operatorCatalogSnapshot"]["digest"] = "0" * 64
    with pytest.raises(ValueError, match="摘要"):
        runner.run(value, tmp_path / "output", lambda *_: None)
    assert not (tmp_path / "output").exists()


def test_reproduction_contains_runtime_schema_resources(tmp_path):
    value = request(tmp_path)
    context = runner.Context(value, tmp_path / "output", lambda *_: None)
    with zipfile.ZipFile(io.BytesIO(runner.reproduction(context))) as archive:
        assert "radstudio/schemas/parameter-schema.schema.json" in archive.namelist()
        assert "operator-catalog.json" in archive.namelist()
        exported = json.loads(archive.read("request.json"))
        assert exported["operatorCatalogSnapshot"]["digest"]


def test_extracted_bundle_replays_with_embedded_sdk_only(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    value = request(tmp_path)
    value["unrelatedSecret"] = "must-not-export"
    original = copy.deepcopy(value)
    context = runner.Context(value, tmp_path / "source-output", lambda *_: None)
    bundle = tmp_path / "detached-bundle"
    bundle.mkdir()
    with zipfile.ZipFile(io.BytesIO(runner.reproduction(context))) as archive:
        archive.extractall(bundle)
        assert b"must-not-export" not in archive.read("request.json")
        assert any(name.startswith("snapshot-inputs/") for name in archive.namelist())
    assert value == original
    from radstudio.reproduction import _snapshot_node_segment

    input_dir = tmp_path / "separate-inputs"
    segment = _snapshot_node_segment("data")
    (input_dir / "inputs" / segment).mkdir(parents=True)
    shutil.copy2(tmp_path / "image.npy", input_dir / "inputs" / segment / "image.npy")
    shutil.copy2(tmp_path / "roi.npy", input_dir / "inputs" / segment / "roi.npy")
    output = tmp_path / "replayed"
    probe = """import runpy,sys
from pathlib import Path
sys.argv=sys.argv[1:]
try: runpy.run_path(sys.argv[0],run_name='__main__')
except SystemExit as error:
 if error.code == 0:
  import radstudio
  assert Path(radstudio.__file__).resolve().is_relative_to(Path(sys.argv[0]).resolve().parent),radstudio.__file__
 raise
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            probe,
            str(bundle / "reproduce.py"),
            "--dataset-dir",
            str(input_dir),
            "--output",
            str(output),
        ],
        cwd=bundle,
        env={**os.environ, "PYTHONPATH": str(bundle), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert (output / "data/samples.json").is_file()
    exported = json.loads((output / "operator-catalog.json").read_text(encoding="utf-8"))
    assert exported["source"] == "installed-catalog"
    # Altered inputs fail before creating another output directory.
    (input_dir / "inputs" / segment / "image.npy").write_bytes(b"changed")
    rejected = tmp_path / "rejected"
    process = subprocess.run(
        [
            sys.executable,
            str(bundle / "reproduce.py"),
            "--dataset-dir",
            str(input_dir),
            "--output",
            str(rejected),
        ],
        cwd=bundle,
        env={**os.environ, "PYTHONPATH": str(bundle), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert process.returncode != 0 and "Input hash mismatch" in process.stderr
    assert not rejected.exists()


def test_frozen_schema_cannot_fall_back_to_installed_schema(tmp_path):
    value = request(tmp_path)
    value["operatorCatalogSnapshot"] = snapshot(value)
    definition = value["operatorCatalogSnapshot"]["nodes"]["data"]["definition"]
    definition["parametersSchema"]["properties"]["imageRole"] = {"type": "integer"}
    body = {
        key: child for key, child in value["operatorCatalogSnapshot"].items() if key != "digest"
    }
    value["operatorCatalogSnapshot"]["digest"] = hashlib.sha256(
        canonical_manifest_json(body)
    ).hexdigest()
    with pytest.raises(ValueError):
        runner.run(value, tmp_path / "output", lambda *_: None)
    # 冻结 schema 在节点执行时才核对（读取节点不校验 IR 参数），失败不发布产物。
    assert not (tmp_path / "output" / "manifest.json").exists()


def test_mismatched_catalog_binding_and_unknown_version_reject(tmp_path):
    for field, changed in (
        ("requestedType", "Other"),
        ("requestedVersion", "9.0.0"),
        ("definition-version", "9.0.0"),
    ):
        value = request(tmp_path)
        value["operatorCatalogSnapshot"] = snapshot(value)
        entry = value["operatorCatalogSnapshot"]["nodes"]["data"]
        if field == "definition-version":
            entry["definition"]["version"] = changed
        else:
            entry[field] = changed
        body = {
            key: child for key, child in value["operatorCatalogSnapshot"].items() if key != "digest"
        }
        value["operatorCatalogSnapshot"]["digest"] = hashlib.sha256(
            canonical_manifest_json(body)
        ).hexdigest()
        with pytest.raises(ValueError):
            runner.run(value, tmp_path / "output", lambda *_: None)
    assert not (tmp_path / "output").exists()


def test_runner_reports_exact_implementation_selection(tmp_path):
    value = request(tmp_path)
    value["operatorCatalogSnapshot"] = snapshot(value)
    runner.run(value, tmp_path / "output", lambda *_: None)
    provenance = json.loads(
        (tmp_path / "output/execution-provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["status"] == "completed"
    assert provenance["implementationReports"]["data"]["selection"] == "exact"
    assert provenance["implementationReports"]["data"]["resolvedVersion"] == "1.0.0"


def test_unavailable_implementation_rejects_before_output_directory(tmp_path):
    value = request(tmp_path)
    value["operatorCatalogSnapshot"] = snapshot(value)
    value["ir"]["nodes"][0]["operatorVersion"] = "9.0.0"
    entry = value["operatorCatalogSnapshot"]["nodes"]["data"]
    entry["requestedVersion"] = "9.0.0"
    entry["definition"]["version"] = "9.0.0"
    body = {
        key: child for key, child in value["operatorCatalogSnapshot"].items() if key != "digest"
    }
    value["operatorCatalogSnapshot"]["digest"] = hashlib.sha256(
        canonical_manifest_json(body)
    ).hexdigest()
    with pytest.raises(KeyError) as error:
        runner.run(value, tmp_path / "output", lambda *_: None)
    assert error.value.code == "operator.version_unavailable"
    assert not (tmp_path / "output").exists()


def test_runner_request_preserves_decimal_parameters():
    from decimal import Decimal

    from radstudio.catalog_snapshot import load_runner_request

    value = load_runner_request(
        b'{"parameters":{"x":0.12345678901234567890123456789},"metric":1e-200}'
    )
    assert value["parameters"]["x"] == Decimal("0.12345678901234567890123456789")
    assert value["metric"] == Decimal("1e-200")


def test_explicit_null_catalog_rejects_without_fallback(tmp_path):
    value = request(tmp_path)
    value["operatorCatalogSnapshot"] = None
    with pytest.raises(ValueError):
        runner.run(value, tmp_path / "output", lambda *_: None)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("failure", ["wrong-port", "changed-bytes"])
def test_failed_node_does_not_publish_artifacts(tmp_path, monkeypatch, failure):
    from radstudio.operators_readers import ReadImagesOperator

    def broken(self, ctx):
        reference = ctx.output_json("samples.json", [], "sample_collection")
        if failure == "changed-bytes":
            (ctx.store.root / "data/samples.json").write_bytes(b"changed")
            return {"samples": reference}
        return {"unknown": reference}

    monkeypatch.setattr(ReadImagesOperator, "run", broken)
    events = []
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        runner.run(request(tmp_path), output, lambda kind, payload: events.append(kind))
    assert "artifact.created" not in events
    assert "node.completed" not in events
    assert "node.failed" in events
    assert not (output / "manifest.json").exists()
    provenance = json.loads((output / "execution-provenance.json").read_text(encoding="utf-8"))
    assert provenance["status"] == "failed"


def test_runner_uses_frozen_invocation_execution_rule(tmp_path):
    value = request(tmp_path)
    value["operatorCatalogSnapshot"] = snapshot(value)
    frozen = value["operatorCatalogSnapshot"]
    frozen["rules"].update(contentContracts=1, artifactExecution=1, invocationExecution=1)
    frozen["digest"] = hashlib.sha256(
        canonical_manifest_json({key: child for key, child in frozen.items() if key != "digest"})
    ).hexdigest()
    runner.run(value, tmp_path / "output", lambda *_: None)
    provenance = json.loads(
        (tmp_path / "output/execution-provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["parameterReceipts"]["data"]["version"] == 2
    assert len(provenance["parameterReceipts"]["data"]["invocationDigest"]) == 64
