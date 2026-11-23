import hashlib

import pytest

from radstudio import operator as framework
from radstudio.operator import ArtifactStore, Operator, invoke, operator


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(framework, "OPERATOR_REGISTRY", dict(framework.OPERATOR_REGISTRY))


def test_workspace_writes_cannot_escape(tmp_path):
    store = ArtifactStore(tmp_path / "workspace")
    with pytest.raises(ValueError):
        store.write_bytes("../escaped.json", b"private", "report")
    assert not (tmp_path / "escaped.json").exists()


def test_reference_reads_cannot_escape_even_with_correct_hash(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    ref = f"file:../outside.json#sha256={hashlib.sha256(b'outside').hexdigest()};type=report"
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path / "workspace").read_bytes(ref)


def test_corrupt_output_is_failed_without_publishing_artifact_events(tmp_path):
    @operator("CorruptOutput", version="1.0.0")
    class CorruptOutput(Operator):
        def run(self, ctx):
            ref = ctx.output_json("output.json", {"ok": True}, "report")
            (ctx.store.root / "output.json").write_bytes(b"changed after write")
            return {"out": ref}

    events = []
    result = invoke(
        {
            "requestId": "request",
            "runId": "run",
            "nodeId": "n",
            "operatorType": "CorruptOutput",
            "operatorVersion": "1.0.0",
            "parameters": {},
            "inputs": {},
        },
        ArtifactStore(tmp_path),
        lambda kind, payload: events.append(kind),
    ).to_dict()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "operator.output_invalid"
    assert "artifact.created" not in events


def test_shared_reference_cases():
    import json
    from pathlib import Path

    from radstudio.artifact_boundary import parse_reference

    cases = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "operator-schema/tests/artifact-reference-cases.json"
        ).read_text(encoding="utf-8")
    )
    for case in cases:
        if case["valid"]:
            assert parse_reference(case["reference"]), case["name"]
        else:
            with pytest.raises(ValueError, match=".+"):
                parse_reference(case["reference"])


def test_operator_cannot_publish_fake_completion_before_validation(tmp_path):
    @operator("FakeCompletion", version="1.0.0")
    class FakeCompletion(Operator):
        def run(self, ctx):
            ctx.emit("node.completed", {"nodeId": "other", "outputs": {}})
            ctx.emit("run.completed", {})
            ref = ctx.output_json("output.json", {}, "report")
            (ctx.store.root / "output.json").write_bytes(b"corrupt")
            return {"out": ref}

    events = []
    result = invoke(
        {
            "requestId": "req",
            "runId": "run",
            "nodeId": "n",
            "operatorType": "FakeCompletion",
            "operatorVersion": "1.0.0",
            "parameters": {},
            "inputs": {},
        },
        ArtifactStore(tmp_path),
        lambda event, payload: events.append(event),
    )
    assert result.status == "failed"
    assert "node.completed" not in events
    assert "run.completed" not in events
    assert "node.failed" in events
