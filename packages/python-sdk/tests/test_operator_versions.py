"""Exact implementation selection through serve, including legacy deployment aliases."""

from __future__ import annotations

import base64
import io
import json
import tarfile

import pytest

from radstudio import operator as framework
from radstudio.operator import Operator, operator
from radstudio.operator_serve import execute_payload as _execute_payload


def execute_payload(payload):
    return _execute_payload(payload).to_dict()


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(framework, "OPERATOR_REGISTRY", dict(framework.OPERATOR_REGISTRY))


def register_versions(name="VersionProbe"):
    calls = []

    @operator(name, version="1.0.0")
    class VersionOne(Operator):
        def run(self, ctx):
            calls.append("1.0.0")
            return {
                "out": ctx.output_json(
                    "version.json",
                    {"version": "1.0.0", "requested": ctx.request.operatorVersion},
                    "report",
                )
            }

    @operator(name, version="2.0.0")
    class VersionTwo(Operator):
        def run(self, ctx):
            calls.append("2.0.0")
            return {
                "out": ctx.output_json(
                    "version.json",
                    {"version": "2.0.0", "requested": ctx.request.operatorVersion},
                    "report",
                )
            }

    return calls


def request(name, version):
    value = {
        "requestId": "request",
        "runId": "run",
        "nodeId": "node",
        "operatorType": name,
        "parameters": {},
        "inputs": {},
    }
    if version is not None:
        value["operatorVersion"] = version
    return value


def output(result):
    assert result["status"] == "completed", result
    with tarfile.open(
        fileobj=io.BytesIO(base64.b64decode(result["outputBundle"])), mode="r:gz"
    ) as archive:
        return json.loads(archive.extractfile("version.json").read())


def test_serve_retains_requested_exact_version():
    calls = register_versions()
    result = execute_payload({"request": request("VersionProbe", "1.0.0")})
    assert output(result) == {"version": "1.0.0", "requested": "1.0.0"}
    assert calls == ["1.0.0"]


def test_serve_missing_version_never_runs_latest():
    calls = register_versions()
    result = execute_payload({"request": request("VersionProbe", "9.0.0")})
    assert result["status"] == "failed"
    assert result["error"]["code"] == "operator.version_unavailable"
    assert not calls


def test_known_legacy_fc_alias_maps_to_exact_base():
    calls = register_versions("QualityControl")
    result = execute_payload({"request": request("QualityControl", "1.0.0-fc")})
    assert output(result) == {"version": "1.0.0", "requested": "1.0.0-fc"}
    assert calls == ["1.0.0"]


def test_arbitrary_fc_suffix_is_not_a_license_to_trim():
    calls = register_versions("PrivateProbe")
    result = execute_payload({"request": request("PrivateProbe", "1.0.0-fc")})
    assert result["status"] == "failed"
    assert not calls


def test_exact_tag_registration_precedes_legacy_alias():
    register_versions("QualityControl")

    @operator("QualityControl", version="1.0.0-fc")
    class ExactTag(Operator):
        def run(self, ctx):
            return {"out": ctx.output_json("version.json", {"version": "exact-tag"}, "report")}

    assert (
        output(execute_payload({"request": request("QualityControl", "1.0.0-fc")}))["version"]
        == "exact-tag"
    )


def test_resolution_reports_actual_selection_and_versionless_legacy():
    register_versions()
    result = execute_payload({"request": request("VersionProbe", "1.0.0")})
    assert result["implementation"] == {
        "operatorType": "VersionProbe",
        "requestedVersion": "1.0.0",
        "resolvedVersion": "1.0.0",
        "selection": "exact",
    }
    legacy = execute_payload({"request": request("VersionProbe", None)})
    assert output(legacy)["version"] == "2.0.0"
    assert legacy["implementation"]["selection"] == "legacy-latest"


def test_missing_alias_target_does_not_use_another_installed_version():
    calls = register_versions("QualityControl")
    del framework.OPERATOR_REGISTRY["QualityControl@1.0.0"]
    result = execute_payload({"request": request("QualityControl", "1.0.0-fc")})
    assert result["status"] == "failed"
    assert result["error"]["details"]["requestedVersion"] == "1.0.0-fc"
    assert result["error"]["details"]["aliasTargetVersion"] == "1.0.0"
    assert not calls


def test_unknown_version_fails_before_input_download_and_finishes_status(monkeypatch):
    from radstudio import operator_serve

    register_versions()

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported version must not download input")

    statuses = []
    monkeypatch.setattr(operator_serve, "_http_get", forbidden)
    monkeypatch.setattr(
        operator_serve._StatusWriter, "_put", lambda self, payload: statuses.append(dict(payload))
    )
    value = request("VersionProbe", "9.0.0")
    value["transfer"] = {
        "mode": "object",
        "protocolVersion": 2,
        "inputManifestUrl": "https://example.invalid/not-read",
        "statusUrl": "https://example.invalid/status",
    }
    result = execute_payload({"request": value})
    assert result["status"] == "failed"
    assert statuses[-1]["status"] == "failed"
    assert statuses[-1]["result"]["error"]["code"] == "operator.version_unavailable"


def test_alias_asset_matches_source():
    from importlib.resources import files
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    assert (
        files("radstudio").joinpath("schemas/legacy-fc-aliases.json").read_bytes()
        == (root / "packages/operator-schema/legacy-fc-aliases.json").read_bytes()
    )


def test_once_process_reports_exact_builtin_version_and_utf8_failure(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    environment = {
        **os.environ,
        "PYTHONPATH": str(root / "packages/python-sdk"),
        "PYTHONIOENCODING": "utf-8",
    }
    for version, success in (("1.0.0", True), ("9.0.0", False)):
        value = request("FeatureFilter", version)
        value["parameters"] = {
            "missingRateThreshold": 0.2,
            "varianceThreshold": 0.01,
            "correlationThreshold": 0.95,
        }
        value["environment"] = {"note": "合成输入"}
        source = tmp_path / f"输入-{version}.json"
        destination = tmp_path / f"result-{version}.json"
        source.write_text(json.dumps({"request": value}, ensure_ascii=False), encoding="utf-8")
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "radstudio.operator_serve",
                "--once",
                "--in",
                str(source),
                "--out",
                str(destination),
            ],
            env=environment,
            capture_output=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
        assert process.returncode == (0 if success else 1), process.stderr
        result = json.loads(destination.read_text(encoding="utf-8"))["result"]
        if success:
            assert result["status"] == "completed"
            assert result["implementation"]["resolvedVersion"] == "1.0.0"
        else:
            assert result["error"]["code"] == "operator.version_unavailable"
            assert "此执行环境" in result["error"]["message"]


def test_identity_field_matches_machine_contract():
    from pathlib import Path

    import jsonschema

    register_versions()
    result = execute_payload({"request": request("VersionProbe", "1.0.0")})
    root = Path(__file__).resolve().parents[3]
    schema = json.loads(
        (root / "packages/operator-schema/operator-result.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(result, schema)


@pytest.mark.parametrize("version", ["", False, 0, []])
def test_direct_invocation_rejects_invalid_version_without_fallback(tmp_path, version):
    calls = register_versions()
    value = framework.OperatorRequest(
        requestId="request",
        runId="run",
        nodeId="node",
        operatorType="VersionProbe",
        operatorVersion=version,
        parameters={},
        inputs={},
    )
    result = framework.invoke(value, framework.BundleArtifactStore(tmp_path))
    assert result.status == "failed"
    assert not calls
