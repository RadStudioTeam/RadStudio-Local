"""统一用户算子运行时测试：内联源码规格、serve 侧分流、工作台调试信封适配与真实隔离子进程执行。

分三层：
1. 纯函数（规格解析、摘要口径、路径安全）——不启动进程，逐条守住拒绝分支。
2. 真实隔离子进程（``run_debug_request`` / ``execute_payload``）——证明「内联源码 → 子进程
   导入注册 → 产物复核 → 回显」整条链路成立，这是用户能不能自助调试的唯一硬证据。
3. HTTP 入口（``UserOperatorServeHandler``）——鉴权、开关、按 body 形态分流。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from radstudio import operator_serve, user_operator, user_serve
from radstudio.operator import OperatorResult, parse_ref
from radstudio.user_operator import (
    InlineCodeFile,
    UserOperatorError,
    fetch_from_spec,
    inline_code_hash,
    parse_user_operator_spec,
    write_inline_files,
)

# --- 内联源码规格 ---------------------------------------------------------


def _source(module: str = "myops.py", body: str = "VALUE = 1\n") -> list[dict]:
    return [{"path": module, "content": body}]


def _go_code_hash(files: list[dict]) -> str:
    """Go 侧 customOperatorCodeHash 的等价实现：按路径升序喂 ``路径\\0摘要\\0``。"""
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry["path"]):
        per_file = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
        digest.update(f"{item['path']}\x00{per_file}\x00".encode())
    return digest.hexdigest()


def test_inline_digest_matches_go_formulation_and_is_order_free():
    files = [
        {"path": "pkg/mod_b.py", "content": "B = 2\n"},
        {"path": "pkg/mod_a.py", "content": "A = 1\n"},
    ]
    expected = _go_code_hash(files)
    spec = parse_user_operator_spec(
        {"entrypoint": "pkg.mod_a:A", "files": files, "sha256": expected}
    )
    assert spec.inline is True
    assert spec.code_sha256 == expected
    # 条目顺序不参与摘要：乱序下发同一份源码必须还能对上
    shuffled = parse_user_operator_spec(
        {"entrypoint": "pkg.mod_a:A", "files": list(reversed(files)), "sha256": expected}
    )
    assert shuffled.code_sha256 == expected
    assert inline_code_hash(
        [InlineCodeFile(path=item["path"], content=item["content"]) for item in files]
    ) == expected


def test_inline_spec_rejects_tampered_digest():
    files = _source()
    with pytest.raises(UserOperatorError) as excinfo:
        parse_user_operator_spec(
            {"entrypoint": "myops:Run", "files": files, "sha256": "0" * 64}
        )
    assert excinfo.value.code == "code_hash_mismatch"
    assert excinfo.value.details["expected"] == "0" * 64
    assert excinfo.value.details["actual"] == _go_code_hash(files)


@pytest.mark.parametrize(
    "bad_path",
    ["../evil.py", "/abs/evil.py", "pkg/../../evil.py", "C:/x/evil.py", "pkg\\mod.py", "..\\x.py"],
)
def test_inline_spec_rejects_unsafe_paths(bad_path):
    with pytest.raises(UserOperatorError) as excinfo:
        parse_user_operator_spec(
            {"entrypoint": "myops:Run", "files": [{"path": bad_path, "content": "x\n"}]}
        )
    assert excinfo.value.code in {"code_path_unsafe", "user_spec_invalid"}


def test_inline_spec_enforces_entry_and_byte_limits():
    big = [{"path": f"m{i}.py", "content": "x" * 40} for i in range(3)]
    with pytest.raises(UserOperatorError) as excinfo:
        parse_user_operator_spec({"entrypoint": "m0:Run", "files": big, "maxEntries": 2})
    assert excinfo.value.code == "code_too_large"

    with pytest.raises(UserOperatorError) as excinfo:
        parse_user_operator_spec(
            {"entrypoint": "m0:Run", "files": _source(body="x" * 500), "maxFileBytes": 256}
        )
    assert excinfo.value.code == "code_too_large"

    with pytest.raises(UserOperatorError) as excinfo:
        parse_user_operator_spec(
            {
                "entrypoint": "m0:Run",
                "files": [{"path": f"m{i}.py", "content": "x" * 300} for i in range(3)],
                "maxFileBytes": 1024,
                "maxTotalBytes": 512,
            }
        )
    assert excinfo.value.code == "code_too_large"


def test_inline_spec_rejects_duplicate_paths_and_bad_content():
    with pytest.raises(UserOperatorError) as excinfo:
        parse_user_operator_spec(
            {
                "entrypoint": "a:Run",
                "files": [
                    {"path": "a.py", "content": "1\n"},
                    {"path": "./a.py", "content": "2\n"},
                ],
            }
        )
    assert excinfo.value.code == "code_path_unsafe"

    with pytest.raises(UserOperatorError):
        parse_user_operator_spec({"entrypoint": "a:Run", "files": [{"path": "a.py"}]})
    with pytest.raises(UserOperatorError):
        parse_user_operator_spec({"entrypoint": "a:Run", "files": []})


def test_spec_accepts_go_key_aliases_and_requires_a_source():
    archive = {
        "entrypoint": "myops:Run",
        "url": "https://example.invalid/code.zip",
        "sha256": "a" * 64,
    }
    spec = parse_user_operator_spec(archive)
    assert spec.code_url == archive["url"] and spec.code_sha256 == "a" * 64
    assert spec.inline is False
    # codeUrl/codeSha256 与 url/sha256 同义
    assert parse_user_operator_spec(
        {
            "entrypoint": "myops:Run",
            "codeUrl": archive["url"],
            "codeSha256": "a" * 64,
        }
    ) == spec
    with pytest.raises(UserOperatorError) as excinfo:
        parse_user_operator_spec({"entrypoint": "myops:Run"})
    assert excinfo.value.code == "user_spec_invalid"


def test_inline_fetch_never_touches_the_network(tmp_path):
    def exploding_transport(url: str, timeout: float) -> bytes:
        raise AssertionError(f"内联模式不应发起下载：{url}")

    spec = parse_user_operator_spec({"entrypoint": "myops:Run", "files": _source()})
    written = fetch_from_spec(spec, tmp_path / "code", transport=exploding_transport)
    assert written == ["myops.py"]
    assert (tmp_path / "code" / "myops.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_write_inline_files_refuses_symlink_escape(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    (code_dir / "link.py").symlink_to(outside)
    with pytest.raises(UserOperatorError) as excinfo:
        write_inline_files(
            [InlineCodeFile(path="link.py", content="x\n")],
            code_dir,
            max_file_bytes=1024,
            max_total_bytes=1024,
        )
    assert excinfo.value.code == "code_path_unsafe"
    assert outside.read_text(encoding="utf-8") == "secret\n"


# --- serve 侧分流 ---------------------------------------------------------


def test_user_spec_source_covers_both_dispatchers():
    code = {"entrypoint": "myops:Run", "files": _source()}
    assert operator_serve._user_spec_source({"userOperator": code}) is code
    assert operator_serve._user_spec_source({"request": {"userCode": code}}) is code
    # 两处同时出现时顶层优先（归档包形态是更老的调用方）
    other = {"entrypoint": "other:Run"}
    assert (
        operator_serve._user_spec_source({"userOperator": other, "request": {"userCode": code}})
        is other
    )
    assert operator_serve._user_spec_source({"request": {"operatorType": "IBSI2D"}}) is None


def test_child_request_strips_source_and_presigned_addresses():
    spec = parse_user_operator_spec({"entrypoint": "myops:Run", "files": _source()})
    request_data = {
        "requestId": "r",
        "runId": "u",
        "nodeId": "n",
        "operatorType": "MyOp",
        "parameters": {},
        "inputs": {"samples": "file:samples.json#sha256=" + "a" * 64 + ";type=application/json"},
        "transfer": {
            "mode": "object",
            "inputManifestUrl": "https://oss.example/manifest?sig=secret",
        },
        "userCode": {**spec.__dict__, "submissionId": "sub-1"},
    }
    child = operator_serve._child_request(request_data, spec, "object")
    assert "userCode" not in child
    assert child["transfer"] == {"mode": "object"}
    assert child["entrypoint"] == "myops:Run"
    assert "sig=secret" not in json.dumps(child)


# --- 工作台调试信封适配 ----------------------------------------------------


def test_debug_spec_source_defaults_and_prioritises_code_section():
    envelope = {
        "entrypoint": "top:Run",
        "code": {"files": _source(), "sha256": _go_code_hash(_source())},
    }
    source = user_serve._debug_spec_source(envelope)
    assert source["files"] == envelope["code"]["files"]
    assert source["entrypoint"] == "top:Run"
    assert source["timeoutSeconds"] == user_serve.DEBUG_DEFAULT_TIMEOUT_SECONDS
    # code.entrypoint / 显式 timeoutSeconds 覆盖默认
    override = user_serve._debug_spec_source(
        {"code": {"entrypoint": "inside:Run", "files": _source()}, "timeoutSeconds": 30}
    )
    assert override["entrypoint"] == "inside:Run" and override["timeoutSeconds"] == 30
    # 非正数/布尔/字符串的 timeoutSeconds 一律回落默认（不让用户把超时调成 0）
    assert (
        user_serve._debug_spec_source({"timeoutSeconds": 0})["timeoutSeconds"]
        == user_serve.DEBUG_DEFAULT_TIMEOUT_SECONDS
    )


def test_debug_child_request_fills_missing_context_ids():
    request = user_serve._debug_child_request({"operatorType": "MyOp", "parameters": {"a": 1}}, "m:R")
    assert request["runId"] == user_serve.DEBUG_RUN_ID
    assert request["nodeId"] == user_serve.DEBUG_NODE_ID
    assert request["requestId"] == "debug"
    assert request["inputs"] == {} and request["operatorType"] == "MyOp"
    assert "operatorVersion" not in request
    assert (
        user_serve._debug_child_request(
            {"requestId": "req-9", "operatorVersion": "1.2.0", "parameters": ["bad"]}, "m:R"
        )["requestId"]
        == "req-9"
    )


def test_materialize_debug_inputs_builds_verified_refs(tmp_path):
    inputs = {
        "samples": {"name": "samples", "kind": "json", "json": [{"id": "a"}, {"id": "b"}]},
        "notes": {"name": "notes", "kind": "text", "text": "第一行\n"},
        "empty": {"name": "empty", "kind": "text", "text": ""},
    }
    refs, errors = user_serve._materialize_debug_inputs(inputs, tmp_path)
    assert errors == []
    assert set(refs) == {"samples", "notes"}
    info = parse_ref(refs["samples"])
    assert info["path"] == "samples.json" and info["type"] == "application/json"
    assert (tmp_path / "samples.json").read_bytes().startswith(b"[")
    text_info = parse_ref(refs["notes"])
    assert text_info["path"] == "notes.txt" and text_info["type"] == "text/plain"
    # 摘要按磁盘真实内容计算，不是调用方声称的
    assert text_info["sha256"] == hashlib.sha256("第一行\n".encode()).hexdigest()


def test_materialize_debug_inputs_rejects_hostile_ports_and_oversize(tmp_path):
    refs, errors = user_serve._materialize_debug_inputs(
        {"../escape": {"json": {}}, "a/b": {"json": {}}, "ok": "not-an-object"}, tmp_path
    )
    assert refs == {} and len(errors) == 3
    big = {"samples": {"text": "x" * (user_serve.DEBUG_INPUT_MAX_BYTES + 1)}}
    _refs, errors = user_serve._materialize_debug_inputs(big, tmp_path)
    assert any("超过" in message for message in errors)
    _refs, errors = user_serve._materialize_debug_inputs(["not", "a", "dict"], tmp_path)
    assert errors and "对象" in errors[0]


def test_log_text_and_debug_outputs(tmp_path):
    assert user_serve._log_text({"stdout": "out\n", "stderr": "err\n"}) == (
        "[stdout]\nout\n\n[stderr]\nerr\n"
    )
    assert user_serve._log_text(None).count("[stdout]") == 1

    from radstudio.operator import BundleArtifactStore

    store = BundleArtifactStore(tmp_path)
    (tmp_path / "report.json").write_text('{"ok": true}\n', encoding="utf-8")
    (tmp_path / "model.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
    outputs = user_serve._debug_outputs(store, {"report.json", "model.bin", "gone.txt"})
    by_name = {entry["name"]: entry for entry in outputs}
    assert by_name["report.json"]["kind"] == "text"
    assert json.loads(by_name["report.json"]["text"]) == {"ok": True}
    assert by_name["report.json"]["truncated"] is False
    # 二进制不塞 base64（Go 侧出现即丢弃），只如实报大小
    assert by_name["model.bin"]["kind"] == "binary" and "base64" not in by_name["model.bin"]
    assert by_name["model.bin"]["size"] == 5
    assert "gone.txt" not in by_name


def test_debug_outputs_marks_truncation_and_overflow(tmp_path):
    from radstudio.operator import BundleArtifactStore

    store = BundleArtifactStore(tmp_path)
    (tmp_path / "big.txt").write_text("a" * (user_serve.DEBUG_OUTPUT_TEXT_BYTES + 10), encoding="utf-8")
    outputs = user_serve._debug_outputs(store, {"big.txt"})
    assert outputs[0]["truncated"] is True
    assert len(outputs[0]["text"]) == user_serve.DEBUG_OUTPUT_TEXT_BYTES

    names = {f"f{i}.txt" for i in range(user_serve.DEBUG_OUTPUT_MAX_FILES + 5)}
    for name in names:
        (tmp_path / name).write_text("x", encoding="utf-8")
    outputs = user_serve._debug_outputs(store, names)
    assert outputs[-1]["kind"] == "notice" and outputs[-1]["truncated"] is True
    assert len([entry for entry in outputs if entry["kind"] != "notice"]) == (
        user_serve.DEBUG_OUTPUT_MAX_FILES
    )


# --- 真实隔离子进程执行 ----------------------------------------------------

_ECHO_SOURCE = '''"""工作台调试用示例算子：读样本清单，写一份 JSON 报告。"""

from radstudio.operator import Operator, operator


@operator("WorkbenchEcho", version="1.0.0")
class WorkbenchEcho(Operator):
    def run(self, ctx):
        samples = ctx.input_json("samples")
        threshold = ctx.parameters.get("threshold", 0)
        report = {"count": len(samples), "threshold": threshold, "ids": [s["id"] for s in samples]}
        return {"report": ctx.output_json("report", report, "application/json")}
'''


def _debug_envelope(**overrides):
    files = [{"path": "echo_op.py", "content": _ECHO_SOURCE}]
    envelope = {
        "requestId": "dbg-1",
        "operatorType": "WorkbenchEcho",
        "operatorVersion": "1.0.0",
        "entrypoint": "echo_op:WorkbenchEcho",
        "parameters": {"threshold": 3},
        "code": {"files": files, "sha256": _go_code_hash(files)},
        "inputs": {"samples": {"name": "samples", "kind": "json", "json": [{"id": "a"}, {"id": "b"}]}},
        "allowPackages": ["numpy"],
        "timeoutSeconds": 90,
    }
    envelope.update(overrides)
    return envelope


def test_run_debug_request_executes_and_echoes_output():
    body = user_serve.run_debug_request(_debug_envelope())
    assert body["status"] == "completed"
    names = {entry["name"]: entry for entry in body["outputs"]}
    # 输入文件不算产物：只回显算子真正写出的那个端口
    assert set(names) == {"report"}
    assert json.loads(names["report"]["text"]) == {
        "count": 2,
        "threshold": 3,
        "ids": ["a", "b"],
    }
    assert body["error"] is None
    metrics = body["metrics"]["userOperator"]
    assert metrics["entrypoint"] == "echo_op:WorkbenchEcho"
    # 用户在工作台要能看到进程级事实：退出码、是否超时、耗时
    assert metrics["exitCode"] == 0 and metrics["timedOut"] is False
    assert metrics["processDurationMs"] >= 0


def _print_source(body: str) -> str:
    """按 run() 方法体拼装一个会往 stdout/stderr 写内容的算子源码。"""
    return (
        "import sys\n\n"
        "from radstudio.operator import Operator, operator\n\n\n"
        '@operator("WorkbenchPrint", version="1.0.0")\n'
        "class WorkbenchPrint(Operator):\n"
        "    def run(self, ctx):\n"
        f"{body}\n"
        '        return {"report": ctx.output_json("report", {"ok": True}, "application/json")}\n'
    )


def _printing_envelope(**overrides):
    files = [
        {
            "path": "print_op.py",
            "content": _print_source(
                '        print("workbench-stdout-marker")\n'
                '        print("workbench-stderr-marker", file=sys.stderr)'
            ),
        }
    ]
    envelope = _debug_envelope(
        operatorType="WorkbenchPrint",
        operatorVersion="1.0.0",
        entrypoint="print_op:WorkbenchPrint",
        code={"files": files, "sha256": _go_code_hash(files)},
    )
    envelope.update(overrides)
    return envelope


def test_run_debug_request_keeps_user_output_and_hides_protocol_framing():
    """用户 print 的内容逐字进日志，内部协议行不得混进去。

    回归两层：早期实现把子进程行当 str 处理，算子只要 print 就整次调用失败；
    同时 RADSTUDIO_RESULT / RADSTUDIO_EVENT 被复制进 stdout，让工作台日志里凭空
    多出一段用户读不懂、也不属于自己代码的框架文本。
    """
    body = user_serve.run_debug_request(_printing_envelope())
    assert body["status"] == "completed", body["error"]
    assert "workbench-stdout-marker" in body["logs"]
    assert "workbench-stderr-marker" in body["logs"]
    assert "RADSTUDIO_RESULT" not in body["logs"]
    assert "RADSTUDIO_EVENT" not in body["logs"]
    assert json.loads({entry["name"]: entry for entry in body["outputs"]}["report"]["text"]) == {
        "ok": True
    }


def test_run_debug_request_forwards_incremental_logs():
    """流式调试靠 on_log 拿增量；用户输出必须按流分开，协议行不得混进来。"""
    chunks: list[tuple[str, str]] = []
    body = user_serve.run_debug_request(
        _printing_envelope(), on_log=lambda stream, text: chunks.append((stream, text))
    )
    assert body["status"] == "completed", body["error"]
    stdout = "".join(text for stream, text in chunks if stream == "stdout")
    stderr = "".join(text for stream, text in chunks if stream == "stderr")
    assert "workbench-stdout-marker" in stdout
    assert "workbench-stderr-marker" in stderr
    assert "RADSTUDIO_RESULT" not in stdout and "RADSTUDIO_EVENT" not in stdout


def test_run_debug_request_ignores_broken_log_callback():
    """日志回传是旁路能力：回调炸了也不能把用户的执行带崩。"""

    def boom(stream, text):
        raise RuntimeError("callback exploded")

    body = user_serve.run_debug_request(_printing_envelope(), on_log=boom)
    assert body["status"] == "completed", body["error"]
    assert "workbench-stdout-marker" in body["logs"]


def test_split_stream_text_never_splits_utf8_characters():
    text = "中文日志" * 3
    pieces = user_serve._split_stream_text(text, limit=4)
    assert "".join(pieces) == text
    assert "\ufffd" not in "".join(pieces)
    assert all(len(piece.encode("utf-8")) <= 4 for piece in pieces)


def test_run_debug_request_quiet_operator_produces_no_stdout_bytes():
    """不打印的算子 stdout 应为空——用来自 E2E 的证据：曾经稳定出现 1139 字节协议行。"""
    body = user_serve.run_debug_request(_debug_envelope())
    assert body["status"] == "completed", body["error"]
    assert body["metrics"]["userOperator"]["stdoutBytes"] == 0


def test_run_debug_request_replaces_oversize_stdout_line(monkeypatch):
    """超上限的单行改用替换文案，而不是把 str 写进字节缓冲把整次调用打挂。"""
    monkeypatch.setattr(user_operator, "MAX_CHILD_STDOUT_BYTES", 4096)
    files = [{"path": "print_op.py", "content": _print_source('        print("y" * 20000)')}]
    body = user_serve.run_debug_request(
        _debug_envelope(
            operatorType="WorkbenchPrint",
            entrypoint="print_op:WorkbenchPrint",
            code={"files": files, "sha256": _go_code_hash(files)},
        )
    )
    assert body["status"] == "completed", body["error"]
    assert "超出上限，已丢弃" in body["logs"]
    assert "yyyy" not in body["logs"]


def test_run_debug_request_surfaces_unregistered_operator():
    files = [{"path": "echo_op.py", "content": _ECHO_SOURCE}]
    body = user_serve.run_debug_request(
        _debug_envelope(
            operatorType="NotRegistered",
            operatorVersion="9.9.9",
            code={"files": files, "sha256": _go_code_hash(files)},
        )
    )
    assert body["status"] == "failed"
    assert body["error"]["code"] == "user_not_registered"
    # 用户要能自助排查：报错里列出实际注册到的键
    assert "WorkbenchEcho@1.0.0" in json.dumps(body["error"]["details"], ensure_ascii=False)


def test_run_debug_request_blocks_dependency_outside_allowlist():
    source = (
        "import httpx\n\n"
        "from radstudio.operator import Operator, operator\n\n\n"
        '@operator("NetOp", version="1.0.0")\n'
        "class NetOp(Operator):\n"
        "    def run(self, ctx):\n"
        "        return {}\n"
    )
    files = [{"path": "net_op.py", "content": source}]
    body = user_serve.run_debug_request(
        _debug_envelope(
            operatorType="NetOp",
            entrypoint="net_op:NetOp",
            code={"files": files, "sha256": _go_code_hash(files)},
        )
    )
    assert body["status"] == "failed"
    assert body["error"]["code"] == "user_dependency_blocked"
    assert "httpx" in json.dumps(body["error"], ensure_ascii=False)


def test_run_debug_request_rejects_bad_spec_and_inputs():
    with pytest.raises(operator_serve.TransferError) as excinfo:
        user_serve.run_debug_request({"operatorType": "X", "entrypoint": "no_colon"})
    assert excinfo.value.status == 400

    files = [{"path": "echo_op.py", "content": _ECHO_SOURCE}]
    body = user_serve.run_debug_request(
        _debug_envelope(
            code={"files": files, "sha256": _go_code_hash(files)},
            inputs={"../evil": {"json": {}}},
        )
    )
    assert body["status"] == "failed"
    assert body["error"]["code"] == "user_input_invalid"
    assert "../evil" in body["logs"]


def test_execute_payload_runs_user_code_from_request_userCode(monkeypatch):
    """工作流节点形态：request.userCode 内联源码 → 标准 operator-result。"""
    monkeypatch.setenv(user_serve.USER_OPERATORS_ENV, "1")
    files = [{"path": "echo_op.py", "content": _ECHO_SOURCE}]
    payload = {
        "request": {
            "requestId": "req-1",
            "runId": "run-1",
            "nodeId": "node-1",
            "operatorType": "WorkbenchEcho",
            "operatorVersion": "1.0.0",
            "parameters": {"threshold": 7},
            "inputs": {},
            "userCode": {
                "submissionId": "sub-1",
                "revisionId": "rev-1",
                "entrypoint": "echo_op:WorkbenchEcho",
                "sha256": _go_code_hash(files),
                "files": files,
                "allowPackages": ["numpy"],
            },
        }
    }
    # 缺少必填输入：用户代码在子进程里报错，但路径必须走隔离执行而不是内置注册表
    result = operator_serve.execute_payload(payload)
    assert isinstance(result, OperatorResult)
    assert (result.requestId, result.runId, result.nodeId) == ("req-1", "run-1", "node-1")
    assert result.status == "failed"
    assert result.error.code == "operator.failed"
    assert "samples" in result.error.message
    assert "userOperator" in result.metrics
    assert result.metrics["userOperator"]["entrypoint"] == "echo_op:WorkbenchEcho"


def test_execute_payload_rejects_user_code_when_runtime_disabled(monkeypatch):
    monkeypatch.delenv(user_serve.USER_OPERATORS_ENV, raising=False)
    files = [{"path": "echo_op.py", "content": _ECHO_SOURCE}]
    payload = {
        "request": {
            "requestId": "req-2",
            "runId": "run-2",
            "nodeId": "node-2",
            "operatorType": "WorkbenchEcho",
            "parameters": {},
            "inputs": {},
            "userCode": {"entrypoint": "echo_op:WorkbenchEcho", "files": files},
        }
    }
    with pytest.raises(operator_serve.TransferError) as excinfo:
        operator_serve.execute_payload(payload)
    assert excinfo.value.status == 403


# --- HTTP 入口 -------------------------------------------------------------


class _QuietHandler(user_serve.UserOperatorServeHandler):
    def log_message(self, *args):  # 测试里不要刷 stderr
        pass


class _Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _post(url: str, body: dict, token: str = "tok"):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture()
def user_env(monkeypatch):
    monkeypatch.setenv("RADSTUDIO_OPERATOR_TOKEN", "tok")
    monkeypatch.setenv(user_serve.USER_OPERATORS_ENV, "1")


def test_healthz_advertises_user_runtime(user_env, monkeypatch):
    monkeypatch.setenv("RADSTUDIO_BUILD", "test-build")
    server = _Server(_QuietHandler)
    try:
        with urllib.request.urlopen(f"{server.base}/healthz", timeout=10) as response:
            body = json.loads(response.read())
        assert body["status"] == "ok" and body["runtime"] == "user"
        assert body["userOperators"] is True
        assert body["protocolVersion"] == user_serve.PROTOCOL_VERSION
        assert body["build"] == "test-build"
    finally:
        server.close()


def test_invoke_requires_token_and_known_path(user_env):
    server = _Server(_QuietHandler)
    try:
        request = urllib.request.Request(
            f"{server.base}/invoke", data=b"{}", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 401
        assert _post(f"{server.base}/nope", {})[0] == 404
    finally:
        server.close()


def test_invoke_returns_503_without_configured_token(monkeypatch):
    monkeypatch.delenv("RADSTUDIO_OPERATOR_TOKEN", raising=False)
    monkeypatch.setenv(user_serve.USER_OPERATORS_ENV, "1")
    server = _Server(_QuietHandler)
    try:
        assert _post(f"{server.base}/invoke", {"operatorType": "X"}) == (
            503,
            {"error": "RADSTUDIO_OPERATOR_TOKEN is not configured"},
        )
    finally:
        server.close()


def test_invoke_forbidden_when_user_operators_disabled(user_env, monkeypatch):
    monkeypatch.delenv(user_serve.USER_OPERATORS_ENV, raising=False)
    server = _Server(_QuietHandler)
    try:
        status, body = _post(f"{server.base}/invoke", {"operatorType": "X"})
        assert status == 403
        assert user_serve.USER_OPERATORS_ENV in body["error"]
    finally:
        server.close()


def test_invoke_dispatches_by_body_shape(user_env, monkeypatch):
    """有 request 键 → 标准协议 {"result": …}；否则 → 工作台扁平响应。"""
    sentinel_result = OperatorResult(
        requestId="r", runId="u", nodeId="n", status="completed", outputs={"report": "file:a.json"}
    )
    monkeypatch.setattr(user_serve, "execute_payload", lambda payload: sentinel_result)
    monkeypatch.setattr(
        user_serve, "run_debug_request", lambda envelope: {"status": "completed", "outputs": []}
    )
    server = _Server(_QuietHandler)
    try:
        status, body = _post(f"{server.base}/invoke", {"request": {"operatorType": "X"}})
        assert status == 200 and set(body) == {"result"}
        assert body["result"]["requestId"] == "r"

        status, body = _post(f"{server.base}/invoke", {"operatorType": "X"})
        assert status == 200 and body == {"status": "completed", "outputs": []}
    finally:
        server.close()


def test_invoke_reports_operator_and_runtime_failures(user_env, monkeypatch):
    monkeypatch.setattr(
        user_serve,
        "run_debug_request",
        lambda envelope: (_ for _ in ()).throw(operator_serve.TransferError(400, "bad spec")),
    )
    server = _Server(_QuietHandler)
    try:
        assert _post(f"{server.base}/invoke", {"operatorType": "X"}) == (400, {"error": "bad spec"})
    finally:
        server.close()

    def boom(envelope):
        raise RuntimeError("runtime exploded")

    monkeypatch.setattr(user_serve, "run_debug_request", boom)
    server = _Server(_QuietHandler)
    try:
        status, body = _post(f"{server.base}/invoke", {"operatorType": "X"})
        assert status == 500 and "runtime exploded" in body["error"]
    finally:
        server.close()


def test_invoke_rejects_non_object_body(user_env):
    server = _Server(_QuietHandler)
    try:
        request = urllib.request.Request(
            f"{server.base}/invoke",
            data=b"[1,2]",
            headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 400
    finally:
        server.close()


def test_debug_invoke_over_http_runs_real_subprocess(user_env):
    """端到端：一次真实的 HTTP 调试调用（常驻进程 → 子进程 → 产物复核 → 扁平响应）。"""
    server = _Server(_QuietHandler)
    try:
        status, body = _post(f"{server.base}/invoke", _debug_envelope())
        assert status == 200
        assert body["status"] == "completed"
        assert json.loads(body["outputs"][0]["text"])["count"] == 2
    finally:
        server.close()


def _post_stream(url: str, body: dict, token: str = "tok"):
    """取回原始流式响应（urllib 会自动解掉 chunked 编码，但保留逐行读取能力）。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
        },
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=60)


def _ndjson_lines(response) -> list[dict]:
    return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line]


def test_debug_stream_returns_ndjson_logs_then_result(user_env):
    """流式调试：先到达 log 行，末行才是 result；分块传输头必须真实下发。"""
    server = _Server(_QuietHandler)
    try:
        with _post_stream(f"{server.base}/invoke", {**_printing_envelope(), "stream": True}) as resp:
            assert resp.headers["Content-Type"].startswith("application/x-ndjson")
            assert resp.headers.get("Transfer-Encoding") == "chunked"
            assert resp.headers.get("X-Accel-Buffering") == "no"
            lines = _ndjson_lines(resp)
        assert lines[-1]["type"] == "result"
        assert lines[-1]["result"]["status"] == "completed"
        log_lines = lines[:-1]
        assert log_lines and {line["type"] for line in log_lines} == {"log"}
        streamed = "".join(line["text"] for line in log_lines)
        assert "workbench-stdout-marker" in streamed
        assert "workbench-stderr-marker" in streamed
        assert "RADSTUDIO_RESULT" not in streamed
        # 结果里仍带完整日志，客户端可以终态时整体替换实时预览
        assert "workbench-stdout-marker" in lines[-1]["result"]["logs"]
    finally:
        server.close()


def test_debug_stream_maps_transfer_error_to_terminal_line(user_env, monkeypatch):
    def boom(envelope, on_log=None, cancel_check=None):
        raise operator_serve.TransferError(400, "bad spec")

    monkeypatch.setattr(user_serve, "run_debug_request", boom)
    server = _Server(_QuietHandler)
    try:
        with _post_stream(f"{server.base}/invoke", {"operatorType": "X", "stream": True}) as resp:
            assert resp.headers["Content-Type"].startswith("application/x-ndjson")
            lines = _ndjson_lines(resp)
        assert len(lines) == 1
        assert lines[0]["type"] == "error" and lines[0]["status"] == 400
        assert lines[0]["error"]["code"] == "debug_request_invalid"
        assert lines[0]["error"]["message"] == "bad spec"
    finally:
        server.close()


def test_debug_stream_without_flag_stays_flat_json(user_env):
    """未要求流式：响应保持今天的扁平 JSON，旧客户端零影响。"""
    server = _Server(_QuietHandler)
    try:
        status, body = _post(f"{server.base}/invoke", _debug_envelope())
        assert status == 200
        assert set(body) >= {"status", "outputs", "logs", "error", "metrics"}
        assert body["status"] == "completed"
    finally:
        server.close()


def test_user_serve_imports_no_builtin_operators():
    """用户运行时里不得存在可被冒用的内置实现：导入 user_serve 后内置注册表必须为空。

    必须在干净解释器里验——同批其它用例会为了走通执行路径装载内置算子。
    """
    code = (
        "import radstudio.user_serve, radstudio.operator as o;"
        "print(len(o.OPERATOR_REGISTRY))"
    )
    sdk_root = Path(user_serve.__file__).resolve().parent.parent
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(sdk_root),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0"
