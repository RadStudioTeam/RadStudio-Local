"""Task 5 单测：算子框架（注册表、invoke 契约、产物存储双模式、serve 模式）。"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from radstudio import operator as operator_module
from radstudio.operator import (
    ArtifactStore,
    BundleArtifactStore,
    Operator,
    OperatorRequest,
    invoke,
    legacy_to_ref,
    make_ref,
    operator,
    parse_ref,
    ref_to_legacy,
    resolve_operator,
)
from radstudio.operator_serve import (
    OperatorServeHandler,
    TransferError,
    _StatusWriter,
    _validate_protocol_version,
    execute_payload,
)


@pytest.fixture(autouse=True)
def _restore_registry():
    """测试间隔离全局算子注册表，避免测试算子泄漏到其他测试模块。"""
    snapshot = dict(operator_module.OPERATOR_REGISTRY)
    yield
    operator_module.OPERATOR_REGISTRY.clear()
    operator_module.OPERATOR_REGISTRY.update(snapshot)


@operator("TestEcho", version="1.2.3")
class TestEchoOperator(Operator):
    def run(self, ctx):
        table = ctx.input_json("table")
        return {"out": ctx.output_json("echo.json", {"echo": table}, "application/json")}


@operator("TestBoom", version="1.0.0")
class TestBoomOperator(Operator):
    def run(self, ctx):
        raise RuntimeError("boom")


@operator("TestMountRead", version="1.0.0")
class TestMountReadOperator(Operator):
    """读取工作区相对路径文件（挂载模式模拟影像算子）。"""

    def run(self, ctx):
        data = ctx.store.resolve_path("data/input.bin").read_bytes()
        return {"out": ctx.output_json("out.json", {"size": len(data)}, "application/json")}


@operator("TestMulti", version="1.0.0")
class TestMultiOperator(Operator):
    """多值输入：按声明顺序逐份读取同名端口。"""

    def run(self, ctx):
        rows = [table["records"][0] for table in ctx.input_set_json("features")]
        return {"out": ctx.output_json("out.json", {"rows": rows}, "application/json")}


def _request(operator_type, inputs=None, version=None, parameters=None):
    data = {
        "requestId": "0d9a4b8e-6e58-4e94-8f8f-7b0b0e5e7d91",
        "runId": "1c2a1fb8-93ab-4c0f-8d16-9dd0dbcb0e7e",
        "nodeId": "node-1",
        "operatorType": operator_type,
        "parameters": parameters or {},
        "inputs": inputs or {},
    }
    if version:
        data["operatorVersion"] = version
    return data


def _tar_bytes(entries: dict[str, bytes]) -> str:
    """构造 tar.gz base64（模拟云函数 bundle 载荷）。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buffer.getvalue()).decode()


# ---------------------------------------------------------------------------
# 注册表与 invoke 契约
# ---------------------------------------------------------------------------


def test_resolve_operator_by_type_and_version():
    assert resolve_operator("TestEcho") is TestEchoOperator
    assert resolve_operator("TestEcho", "1.2.3") is TestEchoOperator
    with pytest.raises(KeyError):
        resolve_operator("NoSuchOperator")


def test_decorator_requires_operator_subclass():
    with pytest.raises(TypeError):

        @operator("TestBad")
        class NotAnOperator:
            pass


def test_invoke_completed(tmp_path):
    store = ArtifactStore(tmp_path)
    table_ref = store.write_json("table.json", {"records": [1, 2]}, "feature-table")
    result = invoke(_request("TestEcho", {"table": table_ref}), store)
    assert result.status == "completed"
    assert result.error is None
    assert parse_ref(result.outputs["out"])["path"] == "echo.json"
    assert json.loads(store.read_bytes(result.outputs["out"])) == {"echo": {"records": [1, 2]}}
    assert {a.storagePath for a in result.artifacts} == {"echo.json"}


def test_invoke_multi_value_inputs(tmp_path):
    store = ArtifactStore(tmp_path)
    first = store.write_json("a.json", {"records": [1]}, "feature-table")
    second = store.write_json("b.json", {"records": [2]}, "feature-table")
    request = _request("TestMulti")
    request["inputSets"] = {"features": [first, second]}
    result = invoke(request, store)
    assert result.status == "completed"
    assert json.loads(store.read_bytes(result.outputs["out"])) == {"rows": [1, 2]}


def test_request_rejects_invalid_input_sets():
    base = _request("TestEcho")

    with pytest.raises(TypeError, match="inputSets must be an object"):
        OperatorRequest.from_dict({**base, "inputSets": ["ref"]})
    with pytest.raises(TypeError, match="inputSets values must be arrays"):
        OperatorRequest.from_dict({**base, "inputSets": {"features": "ref"}})
    with pytest.raises(TypeError, match="inputSets values must be arrays of string references"):
        OperatorRequest.from_dict({**base, "inputSets": {"features": [1]}})
    with pytest.raises(TypeError, match="inputSets values must be arrays of string references"):
        OperatorRequest.from_dict({**base, "inputSets": {"features": []}})
    with pytest.raises(ValueError, match="inputSets port repeats single-value input"):
        OperatorRequest.from_dict(
            {**base, "inputs": {"features": "file:x#sha256=a;type=t"}, "inputSets": {"features": ["file:y#sha256=b;type=t"]}}
        )


def test_context_input_set_missing_port(tmp_path):
    store = ArtifactStore(tmp_path)
    request = _request("TestMulti")
    result = invoke(request, store)
    assert result.status == "failed"
    assert "Missing required input set: features" in result.error.message


def test_invoke_failed_captures_error(tmp_path):
    result = invoke(_request("TestBoom"), ArtifactStore(tmp_path))
    assert result.status == "failed"
    assert result.error.code == "operator.failed"
    assert "boom" in result.error.message
    # 契约回显：失败结果仍携带请求标识
    assert result.requestId == "0d9a4b8e-6e58-4e94-8f8f-7b0b0e5e7d91"


def test_invoke_cancel_check_returns_cancelled(tmp_path):
    @operator("TestCancel", version="1.0.0")
    class TestCancelOperator(Operator):
        def run(self, ctx):
            ctx.check_cancelled()
            return {}

    result = invoke(_request("TestCancel"), ArtifactStore(tmp_path), cancel_check=lambda: True)
    assert result.status == "cancelled"
    assert result.error.code == "operator.cancelled"


def test_invoke_unknown_operator_returns_failed(tmp_path):
    result = invoke(_request("NoSuchOperator"), ArtifactStore(tmp_path))
    assert result.status == "failed"
    assert "NoSuchOperator" in result.error.message


def test_invoke_version_not_available(tmp_path):
    result = invoke(_request("TestEcho", version="9.9.9"), ArtifactStore(tmp_path))
    assert result.status == "failed"
    assert "9.9.9" in result.error.message


def test_invoke_missing_required_input(tmp_path):
    result = invoke(_request("TestEcho", {}), ArtifactStore(tmp_path))
    assert result.status == "failed"
    assert "Missing required input" in result.error.message


def test_invoke_collects_events(tmp_path):
    events = []
    invoke(
        _request("TestBoom"),
        ArtifactStore(tmp_path),
        emit=lambda event, payload: events.append((event, payload)),
    )
    assert [e[0] for e in events] == ["node.started", "node.failed"]


def test_request_from_dict_validation():
    with pytest.raises(ValueError):
        OperatorRequest.from_dict({"requestId": "r"})
    with pytest.raises(TypeError):
        OperatorRequest.from_dict({**_request("X"), "parameters": []})
    with pytest.raises(TypeError):
        OperatorRequest.from_dict({**_request("X"), "inputs": {"a": 1}})
    req = OperatorRequest.from_dict(_request("TestEcho", version="1.2.3"))
    assert req.operatorVersion == "1.2.3"
    assert req.environment is None


# ---------------------------------------------------------------------------
# 产物引用助手
# ---------------------------------------------------------------------------


def test_ref_helpers_roundtrip():
    ref = make_ref("a/b.json", "f" * 64, "table")
    assert ref == f"file:a/b.json#sha256={'f' * 64};type=table"
    legacy = ref_to_legacy(ref)
    assert legacy == {"path": "a/b.json", "sha256": "f" * 64, "type": "table"}
    assert legacy_to_ref(legacy) == ref
    with pytest.raises(ValueError):
        parse_ref("not-a-ref")


# ---------------------------------------------------------------------------
# ArtifactStore：本地目录模式
# ---------------------------------------------------------------------------


def test_artifact_store_roundtrip_and_hash_check(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.write_bytes("model.bin", b"hello", "model")
    assert parse_ref(ref)["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert store.read_bytes(ref) == b"hello"
    (tmp_path / "model.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.read_bytes(ref)


def test_artifact_store_content_addressing(tmp_path):
    a = ArtifactStore(tmp_path / "a")
    b = ArtifactStore(tmp_path / "b")
    assert a.write_bytes("x.json", b"{}", "json") == b.write_bytes("x.json", b"{}", "json")


def test_read_missing_artifact(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = make_ref("missing.json", "0" * 64, "json")
    with pytest.raises(FileNotFoundError):
        store.read_bytes(ref)


def test_store_resolve_path(tmp_path):
    store = ArtifactStore(tmp_path / "ws")
    assert store.resolve_path("data/img.png") == tmp_path / "ws" / "data" / "img.png"
    absolute = str(tmp_path / "elsewhere.png")
    assert str(store.resolve_path(absolute)) == absolute


# ---------------------------------------------------------------------------
# BundleArtifactStore：云函数 bundle 模式
# ---------------------------------------------------------------------------


def test_bundle_roundtrip(tmp_path):
    src = BundleArtifactStore(tmp_path / "src")
    ref = src.write_json("table.json", {"a": 1}, "table")
    src.write_bytes("model.bin", b"weights", "model")
    dst = BundleArtifactStore(tmp_path / "dst")
    dst.import_bundle(src.export_bundle())
    assert dst.read_bytes(ref) == src.read_bytes(ref)


def _tar_names(raw: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        return set(tar.getnames())


def test_bundle_export_excludes_inputs(tmp_path):
    """输入工作区（物化文件）不参与输出传输，只回传本次写入的产物与本地文件。"""
    store = BundleArtifactStore(tmp_path / "ws")
    store.materialize_file("data/input.bin", b"input-bytes")
    store.import_bundle(_tar_bytes({"data/imported.bin": b"imported"}))
    ref = store.write_json("table.json", {"a": 1}, "table")
    store.write_local("preview.png", b"png")

    names = _tar_names(store.export_bytes())
    assert names == {"table.json", "preview.png"}

    dst = BundleArtifactStore(tmp_path / "dst")
    dst.import_bundle(store.export_bundle())
    assert dst.read_json(ref) == {"a": 1}


def test_bundle_export_to_excludes_inputs(tmp_path):
    """流式导出路径（对象模式）与内存导出使用同一输出集合。"""
    store = BundleArtifactStore(tmp_path / "ws")
    store.materialize_file("images/a.png", b"\x89PNG")
    store.write_json("features.json", {"rows": 3}, "feature_table")
    out = tmp_path / "out.tar.gz"
    size, digest = store.export_to(out)
    assert size == out.stat().st_size
    assert digest == hashlib.sha256(out.read_bytes()).hexdigest()
    assert _tar_names(out.read_bytes()) == {"features.json"}


def test_bundle_empty_workspace_exports_empty_archive(tmp_path):
    store = BundleArtifactStore(tmp_path / "ws")
    assert _tar_names(store.export_bytes()) == set()


def test_track_referenced_outputs_includes_passthrough_inputs(tmp_path):
    """非挂载模式：samples.json 间接引用的输入文件必须随输出包回传。"""
    store = BundleArtifactStore(tmp_path / "ws")
    store.materialize_file("data/0_image.png", b"pixels")
    store.materialize_file("data/0_roi.png", b"mask")
    img_sha = hashlib.sha256(b"pixels").hexdigest()
    store.write_json(
        "samples.json",
        {
            "samples": [
                {
                    "imagePath": "data/0_image.png",
                    "roiPath": "data/0_roi.png",
                    "extra": make_ref("data/0_image.png", img_sha, "image"),
                }
            ]
        },
        "sample_collection",
    )
    added = store.track_referenced_outputs()
    assert added == {"data/0_image.png", "data/0_roi.png"}
    assert _tar_names(store.export_bytes()) == {
        "samples.json",
        "data/0_image.png",
        "data/0_roi.png",
    }


def test_track_referenced_outputs_skips_mounted_inputs(tmp_path):
    """挂载模式：直读 OSS 的输入不再打包回传。"""
    store = BundleArtifactStore(tmp_path / "ws")
    store.mount_file("data/0_image.png", _write_file(tmp_path / "oss" / "obj", b"pixels"))
    store.write_json(
        "samples.json", {"samples": [{"imagePath": "data/0_image.png"}]}, "sample_collection"
    )
    store.track_referenced_outputs(skip={"data/0_image.png"})
    assert _tar_names(store.export_bytes()) == {"samples.json"}


def test_mount_file_streams_copy_from_mount(tmp_path):
    """挂载输入物化为真实文件：FC 运行时不允许跨挂载点跟随符号链接。"""
    source = _write_file(tmp_path / "oss" / "obj", b"pixels" * 1024)
    store = BundleArtifactStore(tmp_path / "ws")
    store.mount_file("data/0_image.png", source)
    target = tmp_path / "ws" / "data/0_image.png"
    assert not target.is_symlink()
    assert target.read_bytes() == source.read_bytes()


def _write_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_serve_mount_mode_reads_from_mount_root(tmp_path, monkeypatch):
    """挂载模式：输入经 OSS 挂载点直读（不下载），输出包只含本次产物。"""
    mount = tmp_path / "oss"
    _write_file(mount / "datasets" / "ab" / "abc123", b"mounted-bytes")
    monkeypatch.setenv("RADSTUDIO_FC_MOUNT_ROOT", str(mount))
    uploaded = {}

    class PutHandler(BaseHTTPRequestHandler):
        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            uploaded["raw"] = self.rfile.read(length)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), PutHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = _request("TestMountRead", parameters={})
        request["transfer"] = {
            "mode": "mount",
            "mountRoot": str(mount),
            "mountInputs": [
                {
                    "path": "data/input.bin",
                    "object": "datasets/ab/abc123",
                    "sha256": hashlib.sha256(b"mounted-bytes").hexdigest(),
                }
            ],
            "outputUrl": f"http://127.0.0.1:{server.server_address[1]}/out",
        }
        result = execute_payload({"request": request})
        assert result.status == "completed"
        assert result.outputBundle is None
        assert result.transfer["mode"] == "mount"
        # 调用级计费：serve 端上报本次调用时长/起止时间
        invocation = result.metrics["invocation"]
        assert invocation["durationMs"] >= 0
        assert invocation["startedAt"] and invocation["finishedAt"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert _tar_names(uploaded["raw"]) == {"out.json"}
    with tarfile.open(fileobj=io.BytesIO(uploaded["raw"]), mode="r:gz") as tar:
        out = json.loads(tar.extractfile("out.json").read())
    assert out == {"size": len(b"mounted-bytes")}


def test_serve_mount_mode_falls_back_to_download(tmp_path, monkeypatch):
    """挂载点缺失（本地/未配置挂载）时回退预签名下载，同一镜像可运行。"""
    body = b"fallback-bytes"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("RADSTUDIO_FC_MOUNT_ROOT", str(tmp_path / "missing"))
        request = _request("TestMountRead", parameters={})
        request["transfer"] = {
            "mode": "mount",
            "mountInputs": [
                {
                    "path": "data/input.bin",
                    "object": "datasets/ab/abc123",
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "url": f"http://127.0.0.1:{server.server_address[1]}/obj",
                }
            ],
        }
        result = execute_payload({"request": request})
        assert result.status == "completed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_serve_mount_mode_rejects_escaping_object(tmp_path, monkeypatch):
    monkeypatch.setenv("RADSTUDIO_FC_MOUNT_ROOT", str(tmp_path))
    request = _request("TestMountRead", parameters={})
    request["transfer"] = {
        "mode": "mount",
        "mountInputs": [{"path": "data/input.bin", "object": "../etc/passwd"}],
    }
    with pytest.raises(TransferError, match="escapes root"):
        execute_payload({"request": request})


def test_serve_mount_mode_accepts_missing_mount_inputs(tmp_path, monkeypatch):
    """零输入节点：旧 worker 省略空 mountInputs 时按空列表执行，不判协议错误。"""

    @operator("TestZeroInput")
    class ZeroInputOperator(Operator):
        def run(self, ctx):
            return {"out": ctx.output_json("out.json", {"ok": True}, "application/json")}

    monkeypatch.setenv("RADSTUDIO_FC_MOUNT_ROOT", str(tmp_path))
    request = _request("TestZeroInput")
    request["transfer"] = {"mode": "mount", "mountInputs": []}
    assert execute_payload({"request": request}).status == "completed"

    del request["transfer"]["mountInputs"]
    assert execute_payload({"request": request}).status == "completed"


def test_serve_writes_failed_status_on_transfer_error(tmp_path, monkeypatch):
    """异步状态上报：异常须立即写 failed，worker 不再盲等心跳超时。"""
    statuses = []

    class StatusHandler(BaseHTTPRequestHandler):
        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            statuses.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("RADSTUDIO_FC_MOUNT_ROOT", str(tmp_path))
        request = _request("TestMountRead", parameters={})
        request["transfer"] = {
            "mode": "mount",
            "mountInputs": [{"path": "data/input.bin", "object": "../etc/passwd"}],
            "statusUrl": f"http://127.0.0.1:{server.server_address[1]}/status",
        }
        with pytest.raises(TransferError, match="escapes root"):
            execute_payload({"request": request})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert statuses[-1]["status"] == "failed"
    assert "escapes root" in statuses[-1]["message"]


@contextlib.contextmanager
def _status_capture():
    statuses = []

    class StatusHandler(BaseHTTPRequestHandler):
        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            statuses.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield statuses, f"http://127.0.0.1:{server.server_address[1]}/status"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_serve_accepts_protocol_version_three():
    """协议 3 用于多值输入；1/2/3 均可用，未知版本仍拒绝。"""
    _validate_protocol_version({"protocolVersion": 1})
    _validate_protocol_version({"protocolVersion": 2})
    _validate_protocol_version({"protocolVersion": 3})
    with pytest.raises(TransferError, match="unsupported protocolVersion"):
        _validate_protocol_version({"protocolVersion": 4})


def test_serve_rejects_unsupported_protocol_version():
    """协议版本高于算子支持范围时立即拒绝并写 failed，而不是静默错解协议。"""
    with _status_capture() as (statuses, status_url):
        request = _request("TestEcho")
        request["transfer"] = {"protocolVersion": 4, "statusUrl": status_url}
        with pytest.raises(TransferError, match="unsupported protocolVersion"):
            execute_payload({"request": request})
    assert statuses[-1]["status"] == "failed"
    assert "unsupported protocolVersion" in statuses[-1]["message"]


def test_status_writer_terminal_state_is_immutable():
    """状态机单写者：completed 终态之后的迟到失败写入必须被丢弃。"""
    with _status_capture() as (statuses, status_url):
        writer = _StatusWriter({"statusUrl": status_url})
        writer.start()
        writer.finish({"status": "completed"})
        writer.fail("late failure")
        writer.stop()
    assert [item["status"] for item in statuses] == ["running", "completed"]


def test_bundle_rejects_path_escape(tmp_path):
    store = BundleArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        store.import_bundle(_tar_bytes({"../escape.txt": b"x"}))


def test_bundle_rejects_links(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    store = BundleArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="link"):
        store.import_bundle(base64.b64encode(buffer.getvalue()).decode())


# ---------------------------------------------------------------------------
# serve 模式（模拟云函数 HTTP 端点）
# ---------------------------------------------------------------------------


class TestOperatorServe:
    @pytest.fixture()
    def serve_url(self, monkeypatch):
        monkeypatch.setenv("RADSTUDIO_OPERATOR_TOKEN", "test-token")
        server = ThreadingHTTPServer(("127.0.0.1", 0), OperatorServeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def test_healthz(self, serve_url):
        resp = httpx.get(f"{serve_url}/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["protocolVersion"] == 3

    def test_unknown_path_404(self, serve_url):
        assert httpx.get(f"{serve_url}/nope").status_code == 404
        assert httpx.post(f"{serve_url}/nope").status_code == 404

    def test_invoke_requires_token(self, serve_url):
        resp = httpx.post(f"{serve_url}/invoke", json={"request": _request("TestEcho")})
        assert resp.status_code == 401
        resp = httpx.post(
            f"{serve_url}/invoke",
            json={"request": _request("TestEcho")},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_invoke_without_configured_token(self, monkeypatch):
        monkeypatch.delenv("RADSTUDIO_OPERATOR_TOKEN", raising=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), OperatorServeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            resp = httpx.post(
                f"http://127.0.0.1:{server.server_address[1]}/invoke",
                json={"request": _request("TestEcho")},
            )
            assert resp.status_code == 503
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_invoke_bad_body(self, serve_url):
        resp = httpx.post(
            f"{serve_url}/invoke",
            content=b"not json",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400
        resp = httpx.post(
            f"{serve_url}/invoke",
            json={"inputBundle": ""},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400

    def test_invoke_operator_failure_returns_failed_result(self, serve_url):
        resp = httpx.post(
            f"{serve_url}/invoke",
            json={"request": _request("TestBoom")},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["status"] == "failed"
        assert "boom" in result["error"]["message"]

    def test_invoke_bundle_roundtrip(self, serve_url, tmp_path):
        src = BundleArtifactStore(tmp_path / "in")
        table_ref = src.write_json("table.json", {"records": [1, 2, 3]}, "feature-table")
        resp = httpx.post(
            f"{serve_url}/invoke",
            json={
                "request": _request("TestEcho", {"table": table_ref}),
                "inputBundle": src.export_bundle(),
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["status"] == "completed"
        assert parse_ref(result["outputs"]["out"])["path"] == "echo.json"
        with tarfile.open(
            fileobj=io.BytesIO(base64.b64decode(result["outputBundle"])), mode="r:gz"
        ) as tar:
            extracted = tar.extractfile("echo.json").read()
        assert json.loads(extracted) == {"echo": {"records": [1, 2, 3]}}

    def test_execute_payload_object_mode(self, monkeypatch, tmp_path):
        """对象模式：输入经 inputManifestUrl 拉清单并逐文件下载物化、输出经 outputUrl 上传，body 不内联工作区。"""
        import radstudio.operator_serve as serve

        src = BundleArtifactStore(tmp_path / "in")
        table_ref = src.write_json("table.json", {"records": [1, 2, 3]}, "feature-table")

        # 模拟 worker 内容寻址上传：逐文件写入对象存储，构造输入清单对象。
        table_bytes = (tmp_path / "in" / "table.json").read_bytes()
        manifest = {
            "version": 1,
            "files": [
                {
                    "path": "table.json",
                    "sha256": hashlib.sha256(table_bytes).hexdigest(),
                    "size": len(table_bytes),
                    "url": "mem://artifacts/table",
                }
            ],
        }
        objects: dict[str, bytes] = {
            "mem://artifacts/table": table_bytes,
            "mem://manifest": json.dumps(manifest).encode(),
        }
        monkeypatch.setattr(serve, "_http_get", lambda url, client=None: objects[url])

        def _put_file(url, path, client=None):
            objects[url] = path.read_bytes()

        monkeypatch.setattr(serve, "_http_put_file", _put_file)

        request = _request("TestEcho", {"table": table_ref})
        request["transfer"] = {
            "mode": "object",
            "inputManifestUrl": "mem://manifest",
            "outputUrl": "mem://out",
        }
        result = serve.execute_payload({"request": request})

        assert result.status == "completed"
        # 对象模式不内联 outputBundle，改回填 transfer 元数据。
        assert result.outputBundle is None
        assert result.transfer["mode"] == "object"
        uploaded = objects["mem://out"]
        assert result.transfer["outputSha256"] == hashlib.sha256(uploaded).hexdigest()
        assert result.transfer["outputSize"] == len(uploaded)
        # 上传的输出 bundle 含算子产物。
        dst = BundleArtifactStore(tmp_path / "out")
        dst.import_bytes(uploaded)
        assert json.loads(dst.read_bytes(result.outputs["out"])) == {"echo": {"records": [1, 2, 3]}}

    def test_export_to_streaming(self, tmp_path):
        """export_to 流式落盘：字节内容与 export_bytes 一致，返回正确 size/sha256。"""
        src = BundleArtifactStore(tmp_path / "ws")
        table_ref = src.write_json("table.json", {"records": [1, 2, 3]}, "feature-table")

        out_path = tmp_path / "out.tar.gz"
        size, sha = src.export_to(out_path)

        raw = out_path.read_bytes()
        assert size == len(raw)
        assert sha == hashlib.sha256(raw).hexdigest()
        dst = BundleArtifactStore(tmp_path / "restored")
        dst.import_bytes(raw)
        assert json.loads(dst.read_bytes(table_ref)) == {"records": [1, 2, 3]}

    def test_execute_payload_object_mode_sha_mismatch(self, monkeypatch, tmp_path):
        """清单文件 sha256 不匹配 → TransferError(400)，不执行算子。"""
        import radstudio.operator_serve as serve

        manifest = {
            "version": 1,
            "files": [
                {
                    "path": "table.json",
                    "sha256": hashlib.sha256(b"expected").hexdigest(),
                    "size": len(b"expected"),
                    "url": "mem://artifacts/table",
                }
            ],
        }
        objects: dict[str, bytes] = {
            "mem://artifacts/table": b"corrupted",
            "mem://manifest": json.dumps(manifest).encode(),
        }
        monkeypatch.setattr(serve, "_http_get", lambda url, client=None: objects[url])
        request = _request("TestEcho")
        request["transfer"] = {
            "mode": "object",
            "inputManifestUrl": "mem://manifest",
            "outputUrl": "mem://out",
        }
        with pytest.raises(serve.TransferError) as exc:
            serve.execute_payload({"request": request})
        assert exc.value.status == 400
