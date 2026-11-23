"""统一用户算子运行时（``python -m radstudio.user_serve``）。

一个云函数同时接两个下发方，每个请求各自落到独占临时目录 + 独立子进程执行：

1. **工作流节点**（Go ``userNodeExecutor``）——标准算子协议
   ``{"request": {…, "userCode": {files, entrypoint, sha256, allowPackages}}}``
   → ``{"result": <operator-result>}``。与内置算子函数共用同一条传输链路
   （inline / object / mount、异步心跳、取消、计量），因此直接复用
   :func:`radstudio.operator_serve.execute_payload`。
2. **工作台调试**（Go ``invokeUserOperator``）——扁平请求
   ``{"requestId", "operatorType", "operatorVersion", "entrypoint",
   "code": {"files", "sha256"}, "inputs": {port: {"kind", "json" | "text"}},
   "parameters", "allowPackages", "timeoutSeconds"}``
   → 扁平响应 ``{"status", "outputs", "logs", "error"}``。
   请求带 ``"stream": true`` 时改为 NDJSON 分块（``Transfer-Encoding: chunked``）：
   执行过程中逐行下发 ``{"type":"log","stream":"stdout"|"stderr","text":…}``，
   末行为 ``{"type":"result","result":<扁平响应>}``（运行时层失败为 ``type=error``）。
   调试没有 run/node 语境、也不带预签名地址：输入按端口名物化进本次工作区，
   产物按内容回显（文本优先，二进制只报大小与截断标记）。

两种形态只看 body 里有没有 ``request`` 键区分，其余（鉴权、临时目录、依赖白名单、
环境脱敏、产物复核）共用同一套实现。

本进程**不加载内置算子注册表**：用户运行时里不存在可被冒用的内置实现，
``operatorType`` 只能命中用户代码自己注册的算子，否则子进程报
``user_not_registered``。用户代码本身也只在子进程里导入（见
:mod:`radstudio.user_execute`），常驻进程永不调度它。
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import signal
import tempfile
import threading
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .artifact_boundary import hash_file
from .json_contract import encode_runtime_json
from .operator import BundleArtifactStore, OperatorRequest, make_ref
from .operator_serve import (
    _MAX_BODY_BYTES,
    PROTOCOL_VERSION,
    USER_OPERATORS_ENV,
    OperatorServeHandler,
    TransferError,
    _adopt_user_outputs,
    _user_failure,
    _user_operators_enabled,
    execute_payload,
    load_operator_payload,
)
from .user_operator import (
    DEFAULT_LOG_TAIL_BYTES,
    UserOperatorError,
    fetch_from_spec,
    parse_user_operator_spec,
    run_user_operator_isolated,
)

#: 回显给工作台的文件预览上限（Go 侧还会再按总量裁剪，这里先守住本进程内存）
DEBUG_OUTPUT_TEXT_BYTES = 64 << 10
DEBUG_OUTPUT_MAX_FILES = 100
#: 单个调试输入端口的字节上限（样本清单级别，远小于影像）
DEBUG_INPUT_MAX_BYTES = 4 << 20
#: 调试没有心跳对象，超时只能靠子进程自身；这里给一个足够跑完小样本的默认值
DEBUG_DEFAULT_TIMEOUT_SECONDS = 120.0
#: 工作台调试语境下请求里缺失的字段用固定标识补齐（算子只会看到它们）
DEBUG_RUN_ID = "workbench-debug"
DEBUG_NODE_ID = "debug"
#: 流式调试：日志攒批窗口（秒）与单条 NDJSON 文本上限（按 UTF-8 字节切）
DEBUG_STREAM_BATCH_SECONDS = 0.2
DEBUG_STREAM_TEXT_BYTES = 32 << 10

_RUNTIME_NAME = "user"

_gpu_probe_cache: dict | None = None


def _gpu_info() -> dict:
    """GPU 能力探针（/healthz）：torch 版本 + CUDA 可用性，结果缓存（torch 首次导入很慢）。

    CPU 镜像没装 torch，返回空 dict；GPU 镜像返回 {"torch": "2.x", "cuda": bool, …}。
    常驻进程只读版本号与设备数，绝不在这里初始化完整 CUDA 上下文。
    """
    global _gpu_probe_cache
    if _gpu_probe_cache is None:
        try:
            import torch

            available = bool(torch.cuda.is_available())
            _gpu_probe_cache = {
                "torch": torch.__version__,
                "cuda": available,
                "cudaDevices": torch.cuda.device_count() if available else 0,
            }
        except (ImportError, OSError):
            # ImportError：CPU 镜像没有 torch；OSError：CUDA DLL 加载失败。缓存空 dict。
            _gpu_probe_cache = {}
    return _gpu_probe_cache


# --- 调试信封 → 规格 / 请求 -----------------------------------------------


def _debug_spec_source(envelope: dict) -> dict:
    """把工作台调试信封折叠成 :func:`parse_user_operator_spec` 认识的规格段。"""
    code = envelope.get("code") if isinstance(envelope.get("code"), dict) else {}
    timeout = envelope.get("timeoutSeconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        timeout = DEBUG_DEFAULT_TIMEOUT_SECONDS
    allow = code.get("allowPackages")
    if allow is None:
        allow = envelope.get("allowPackages")
    return {
        "entrypoint": str(code.get("entrypoint") or envelope.get("entrypoint") or ""),
        "files": code.get("files"),
        "url": code.get("url") or "",
        "sha256": code.get("sha256") or "",
        "allowPackages": allow,
        "timeoutSeconds": timeout,
    }


def _debug_child_request(envelope: dict, entrypoint: str) -> dict:
    """构造子进程要执行的算子请求（调试语境：无 transfer、无预签名地址）。"""

    def text(key: str, fallback: str = "") -> str:
        value = envelope.get(key)
        return str(value) if isinstance(value, str) and value else fallback

    parameters = envelope.get("parameters")
    request: dict[str, Any] = {
        "requestId": text("requestId", "debug"),
        "runId": DEBUG_RUN_ID,
        "nodeId": DEBUG_NODE_ID,
        "operatorType": text("operatorType"),
        "parameters": parameters if isinstance(parameters, dict) else {},
        "inputs": {},
        "entrypoint": entrypoint,
    }
    version = envelope.get("operatorVersion")
    if isinstance(version, str) and version:
        request["operatorVersion"] = version
    return request


def _debug_input_bytes(port: str, raw: Any) -> tuple[str, bytes] | str:
    """把 ``UserInvokeInput`` 转成 (相对路径, 字节)；不合格时返回中文错误串。"""
    if not isinstance(raw, dict):
        return f"输入端口 {port} 的取值必须是对象"
    payload = raw.get("json")
    if payload is not None and payload != {} and payload != [] and payload != "":
        try:
            data = encode_runtime_json(payload)
        except (TypeError, ValueError) as exc:
            return f"输入端口 {port} 的 JSON 无法序列化：{exc}"
        if len(data) > DEBUG_INPUT_MAX_BYTES:
            return f"输入端口 {port} 的 JSON 超过 {DEBUG_INPUT_MAX_BYTES} 字节"
        return (f"{port}.json", data)
    text_value = raw.get("text")
    if isinstance(text_value, str) and text_value:
        data = text_value.encode("utf-8")
        if len(data) > DEBUG_INPUT_MAX_BYTES:
            return f"输入端口 {port} 的文本超过 {DEBUG_INPUT_MAX_BYTES} 字节"
        return (f"{port}.txt", data)
    return ""


def _materialize_debug_inputs(inputs: Any, workdir: Path) -> tuple[dict[str, str], list[str]]:
    """把调试输入写进本次独占工作区并生成产物引用；返回 (端口→引用, 错误列表)。

    端口名必须是单个干净的路径分量（``samples``、``features``），带路径分隔符或
    ``..`` 的一律拒绝——输入名直接决定落盘路径。
    """
    refs: dict[str, str] = {}
    errors: list[str] = []
    if inputs is None or inputs == {}:
        return refs, errors
    if not isinstance(inputs, dict):
        return refs, ["inputs 必须是端口名到输入描述的对象"]
    from .user_operator import _safe_member_parts  # 与源码解包同一套路径约束

    for port, raw in inputs.items():
        name = str(port or "")
        try:
            parts = _safe_member_parts(name)
        except UserOperatorError as exc:
            errors.append(f"输入端口名无效：{name!r}（{exc}）")
            continue
        if len(parts) != 1:
            errors.append(f"输入端口名无效：{name!r}")
            continue
        written = _debug_input_bytes(parts[0], raw)
        if isinstance(written, str):
            if written:
                errors.append(written)
            continue
        rel, data = written
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digest, _size = hash_file(target)
        kind = "application/json" if rel.endswith(".json") else "text/plain"
        refs[parts[0]] = make_ref(rel, digest, kind)
    return refs, errors


def _log_text(logs: Any) -> str:
    streams = logs if isinstance(logs, dict) else {}
    chunks = [f"[{name}]\n{streams.get(name) or ''}" for name in ("stdout", "stderr")]
    return "\n".join(chunks)


def _debug_outputs(store: BundleArtifactStore, names: set[str]) -> list[dict[str, Any]]:
    """按磁盘真实内容回显调试产物（文本内联，二进制只报大小）。

    Go 侧对 base64 的处理是「出现即丢弃并标记截断」，所以这里不为二进制文件塞
    base64：与其给一份注定被丢掉的内容，不如如实报大小与 truncated，让用户知道
    算子确实产出了这个文件。
    """
    outputs: list[dict[str, Any]] = []
    for rel in sorted(names)[:DEBUG_OUTPUT_MAX_FILES]:
        path = store.root / rel
        if not path.is_file():
            continue
        _digest, size = hash_file(path)
        entry: dict[str, Any] = {"name": rel, "size": size}
        with path.open("rb") as handle:
            head = handle.read(DEBUG_OUTPUT_TEXT_BYTES)
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            entry["kind"] = "binary"
            entry["truncated"] = True
            outputs.append(entry)
            continue
        entry["kind"] = "text"
        entry["text"] = text
        entry["truncated"] = size > len(head)
        outputs.append(entry)
    if len(names) > DEBUG_OUTPUT_MAX_FILES:
        outputs.append(
            {
                "name": "…",
                "kind": "notice",
                "size": 0,
                "text": f"产物文件数超过 {DEBUG_OUTPUT_MAX_FILES} 个，仅回显前若干个。",
                "truncated": True,
            }
        )
    return outputs


def _split_stream_text(text: str, limit: int = DEBUG_STREAM_TEXT_BYTES) -> list[str]:
    """按 UTF-8 字节上限切分日志增量；切点前移到完整字符边界。"""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    while len(encoded) > limit:
        cut = limit
        while cut > 0 and encoded[cut] & 0xC0 == 0x80:
            cut -= 1
        if cut == 0:  # limit 远大于 4 字节，兜底避免死循环
            cut = limit
        chunks.append(encoded[:cut].decode("utf-8", errors="replace"))
        encoded = encoded[cut:]
    if encoded:
        chunks.append(encoded.decode("utf-8", errors="replace"))
    return chunks


def run_debug_request(
    envelope: dict,
    on_log: Callable[[str, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """执行一次工作台调试调用，返回扁平 ``UserInvokeResponse`` 结构。

    算子层面的失败（用户代码报错、依赖被拦、产物校验不过）以 200 + status=failed
    回传，让用户在工作台看到错误码与栈摘要；只有运行时层面的不可用才用 4xx。
    ``on_log`` 把用户输出按到达顺序增量回传（流式调试用），``cancel_check`` 在
    调用方断开连接后停止继续空跑。
    """
    if not isinstance(envelope, dict):
        raise TransferError(400, "调试请求体必须是 JSON 对象")
    try:
        spec = parse_user_operator_spec(_debug_spec_source(envelope))
    except UserOperatorError as exc:
        raise TransferError(400, f"invalid debug request: {exc}") from exc
    child_request = _debug_child_request(envelope, spec.entrypoint)
    sandbox = Path(tempfile.mkdtemp(prefix="radstudio-user-debug-"))
    code_dir = sandbox / "code"
    workdir = sandbox / "workspace"
    code_dir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        from .operator_serve import _user_code_transport

        fetch_from_spec(spec, code_dir, transport=_user_code_transport)
        refs, errors = _materialize_debug_inputs(envelope.get("inputs"), workdir)
        if errors:
            return {
                "status": "failed",
                "outputs": [],
                "logs": "\n".join(errors),
                "error": {
                    "code": "user_input_invalid",
                    "message": "调试输入无效：" + "；".join(errors),
                    "details": {},
                },
            }
        child_request["inputs"] = refs
        request = OperatorRequest.from_dict(child_request)
        run = run_user_operator_isolated(
            request=child_request,
            code_dir=code_dir,
            workspace_root=workdir,
            sandbox_dir=sandbox,
            entrypoint=spec.entrypoint,
            allow_packages=list(spec.allow_packages),
            timeout_seconds=spec.timeout_seconds,
            on_log=on_log,
            cancel_check=cancel_check,
            max_log_bytes=max(DEFAULT_LOG_TAIL_BYTES, 24 << 10),
        )
        store = BundleArtifactStore(workdir)
        result = (
            _adopt_user_outputs(request, run, store)
            if isinstance(run.get("result"), dict)
            else _user_failure(request, run)
        )
        logs = run.get("logs") or {}
        usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
        # 与工作流节点路径同一份执行元数据：用户在工作台要能看到退出码、是否超时、
        # 装载了几个源码文件，而不是只有一句 failed。
        metrics: dict[str, Any] = {
            "entrypoint": spec.entrypoint,
            "codeSha256": spec.code_sha256,
            "allowPackages": list(spec.allow_packages),
            "timeoutSeconds": spec.timeout_seconds,
            "exitCode": run.get("exitCode"),
            "timedOut": bool(run.get("timedOut")),
            "cancelled": bool(run.get("cancelled")),
            "processDurationMs": int(run.get("durationMs") or 0),
            "stdoutBytes": len(str(logs.get("stdout") or "")),
            "stderrBytes": len(str(logs.get("stderr") or "")),
        }
        peak_rss = usage.get("peakRssBytes")
        if isinstance(peak_rss, (int, float)) and not isinstance(peak_rss, bool) and peak_rss > 0:
            metrics["peakRssBytes"] = int(peak_rss)
        result.metrics["userOperator"] = metrics
        return {
            "status": result.status,
            "outputs": _debug_outputs(store, store.written_names()),
            "logs": _log_text(run.get("logs")),
            "error": result.error.to_dict() if result.error is not None else None,
            "metrics": {"userOperator": dict(result.metrics.get("userOperator") or {})},
        }
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


class UserOperatorServeHandler(OperatorServeHandler):
    """统一用户运行时入口：``/invoke`` 按请求形态分流，``/healthz`` 声明用户算子能力。"""

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "build": os.environ.get("RADSTUDIO_BUILD", ""),
                    "protocolVersion": PROTOCOL_VERSION,
                    "implementationSelection": "exact-v1",
                    "executionContractVersion": 1,
                    "artifactExecutionVersion": 1,
                    "runtime": _RUNTIME_NAME,
                    "userOperators": _user_operators_enabled(),
                    # 工作台据此决定是否展示「实时」日志；旧镜像没有这个键。
                    "debugLogStream": True,
                    # CPU 镜像为空 dict；GPU 镜像带 torch 版本与 CUDA 可用性。
                    "gpu": _gpu_info(),
                },
            )
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/invoke":
            self._send_json(404, {"error": "not found"})
            return
        denied = self._auth_denied()
        if denied is not None:
            if denied == 503:
                self._send_json(503, {"error": "RADSTUDIO_OPERATOR_TOKEN is not configured"})
            else:
                self._send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._send_json(400, {"error": f"invalid body size: {length}"})
            return
        try:
            payload = load_operator_payload(self.rfile.read(length))
        except (ValueError, UnicodeError, RecursionError) as exc:
            self._send_json(400, {"error": f"invalid request body: {exc}"})
            return
        if not _user_operators_enabled():
            self._send_json(
                403,
                {
                    "error": f"user operators are disabled in this runtime;"
                    f" set {USER_OPERATORS_ENV}=1 on the approved user-operator image"
                },
            )
            return
        if isinstance(payload, dict) and isinstance(payload.get("request"), dict):
            self._standard_invoke(payload)
            return
        self._debug_invoke(payload)

    def _standard_invoke(self, payload: dict) -> None:
        """工作流节点：标准算子协议，响应 ``{"result": …}``（与内置 serve 一致）。"""
        try:
            result = execute_payload(payload)
        except TransferError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        self._send_json(200, {"result": result.to_dict()})

    def _debug_invoke(self, payload: dict) -> None:
        """工作台调试：扁平请求，扁平响应；算子失败仍是 200。"""
        if isinstance(payload, dict) and payload.get("stream") is True:
            self._debug_stream_invoke(payload)
            return
        try:
            body = run_debug_request(payload)
        except TransferError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        except Exception as exc:  # noqa: BLE001 - 运行时自身的问题必须如实回 500
            self._send_json(500, {"error": f"user runtime failed: {type(exc).__name__}: {exc}"})
            return
        self._send_json(200, body)

    def _write_ndjson(self, payload: dict) -> bool:
        """写一条 HTTP chunk；客户端已断开时返回 False（调用方据此取消算子）。"""
        data = encode_runtime_json(payload) + b"\n"
        try:
            self.wfile.write(b"%x\r\n" % len(data))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _debug_stream_invoke(self, payload: dict) -> None:
        """流式调试：执行中边产出边下发 NDJSON 日志，末行才是结果。

        响应头一旦发出就无法再改成错误码，所以运行时层失败也编码成
        ``{"type":"error", …}`` 一行；非流式请求完全不受影响。
        """
        log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        outcome: dict[str, Any] = {}
        done = threading.Event()
        cancel_event = threading.Event()

        def on_log(stream_name: str, text: str) -> None:
            log_queue.put((stream_name, text))

        def work() -> None:
            try:
                outcome["body"] = run_debug_request(
                    payload, on_log=on_log, cancel_check=cancel_event.is_set
                )
            except TransferError as exc:
                outcome["error"] = (exc.status, exc.message)
            except Exception as exc:  # noqa: BLE001 - 运行时自身的问题如实回传
                outcome["error"] = (
                    500,
                    f"user runtime failed: {type(exc).__name__}: {exc}",
                )
            finally:
                done.set()

        threading.Thread(target=work, name="user-debug-stream", daemon=True).start()

        # HTTP/1.1 + chunked：不允许用 Content-Length 憋到执行结束才发。
        self.protocol_version = "HTTP/1.1"
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        # 反向代理默认会攒响应，明确关掉缓冲（Nginx 系网关认这个头）
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        pending: dict[str, list[str]] = {"stdout": [], "stderr": []}

        def flush_pending() -> bool:
            for stream_name in ("stdout", "stderr"):
                text = "".join(pending[stream_name])
                if not text:
                    continue
                pending[stream_name] = []
                for piece in _split_stream_text(text):
                    if not self._write_ndjson(
                        {"type": "log", "stream": stream_name, "text": piece}
                    ):
                        return False
            return True

        try:
            while not (done.is_set() and log_queue.empty()):
                try:
                    stream_name, text = log_queue.get(timeout=DEBUG_STREAM_BATCH_SECONDS)
                except queue.Empty:
                    # 攒批窗口内没有新日志也刷一次：长计算期间保持连接活跃
                    if not flush_pending():
                        cancel_event.set()
                        return
                    continue
                pending.setdefault(stream_name, []).append(text)
                while True:
                    try:
                        stream_name, text = log_queue.get_nowait()
                    except queue.Empty:
                        break
                    pending.setdefault(stream_name, []).append(text)
                if not flush_pending():
                    cancel_event.set()
                    return
            if not flush_pending():
                cancel_event.set()
                return
            error = outcome.get("error")
            if error is not None:
                status, message = int(error[0]), str(error[1])
                self._write_ndjson(
                    {
                        "type": "error",
                        "status": status,
                        "error": {
                            "code": "debug_request_invalid"
                            if 400 <= status < 500
                            else "user_runtime_failed",
                            "message": message,
                        },
                    }
                )
            else:
                body = outcome.get("body")
                self._write_ndjson(
                    {"type": "result", "result": body if isinstance(body, dict) else {}}
                )
        except (BrokenPipeError, ConnectionResetError, OSError):
            cancel_event.set()
            return
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="RadStudio 统一用户算子运行时")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    args = parser.parse_args(argv)
    # 刻意不调用 load_builtin_operators()：用户运行时里不该存在任何内置实现可被冒用。
    server = ThreadingHTTPServer((args.host, args.port), UserOperatorServeHandler)
    print(
        f"radstudio user_serve listening on http://{args.host}:{args.port} "
        f"(userOperators={_user_operators_enabled()})",
        flush=True,
    )
    signal.signal(
        signal.SIGTERM,
        lambda signum, frame: threading.Thread(target=server.shutdown, daemon=True).start(),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
