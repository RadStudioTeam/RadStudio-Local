"""算子 HTTP 服务模式（serve mode）。

以 ``python -m radstudio.operator_serve --host 0.0.0.0 --port 8080`` 启动，
作为云函数容器（阿里云 FC / 腾讯云 SCF / Cloud Run）或本地 docker 镜像的
常驻入口，由 Go 侧执行器（docker executor / cloud_executor）统一派发调用。

端点：
- ``POST /invoke``：执行一次算子调用。
    请求体 JSON：{"request": <operator-request>, "inputBundle": "<tar.gz base64, 可选>"}
    响应体 JSON：{"result": <operator-result>}；成功时 result.outputBundle
    为输出工作区 tar.gz base64（云函数 bundle 模式）。
- ``GET /healthz``：健康检查。

认证：``Authorization: Bearer <token>``，token 取环境变量
``RADSTUDIO_OPERATOR_TOKEN``；未配置时拒绝执行（安全默认，避免无鉴权的执行端点）。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import signal
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from pathlib import Path

from .artifact_boundary import ArtifactBoundaryError, hash_file, workspace_path
from .execution_contract import (
    ExecutionContractError,
    invocation_artifact_ports,
    validate_execution_contract,
)
from .json_contract import decode_runtime_json, encode_runtime_json
from .operator import (
    BundleArtifactStore,
    OperatorError,
    OperatorRequest,
    OperatorResolutionError,
    OperatorResult,
    invoke,
    make_ref,
    parse_ref,
    resolve_invocation,
)
from .user_operator import (
    UserOperatorError,
    UserOperatorSpec,
    fetch_from_spec,
    parse_user_operator_spec,
    run_user_operator_isolated,
)

# 云函数平台请求体上限普遍在 32MB 量级，本地保护上限设 512MB 防止误传
_MAX_BODY_BYTES = 512 * 1024 * 1024

# 算子传输协议版本：2 起 worker 显式下发 mountInputs 并携带 protocolVersion；
# 缺失/0/1 视为旧协议兼容，高于本端支持版本按 400 拒绝，避免静默错配。
PROTOCOL_VERSION = 3
SUPPORTED_PROTOCOL_VERSIONS = (1, 2, PROTOCOL_VERSION)


class TransferError(Exception):
    """带外传输或输入非法错误，携带应回复的 HTTP 状态码。

    4xx 表示请求本身被拒（不可重试，如 sha256 不匹配）；5xx 表示传输层失败
    （对象存储临时不可用等，worker 会重试）。
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _transfer_client():
    """对象存储传输专用 httpx.Client（惰性导入，仅对象模式付费）。

    云函数环境每次冷握手成本高：同一调用的清单下载、逐文件下载、输出上传
    共享一个连接池，复用 TLS 连接。
    """
    import httpx

    return httpx.Client(timeout=httpx.Timeout(30.0, read=600.0, write=600.0), follow_redirects=True)


def _http_get(url: str, client=None) -> bytes:
    """经预签名 GET URL 下载对象；client 可传入共享连接池。"""
    import httpx

    try:
        if client is None:
            with _transfer_client() as shared:
                resp = shared.get(url)
        else:
            resp = client.get(url)
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPError as exc:
        raise TransferError(500, f"download input bundle failed: {exc}") from exc


def _http_put_file(url: str, path: Path, client=None) -> None:
    """按文件对象流式 PUT：输出 tar.gz 不整包驻留内存，httpx 按 Content-Length 直传。"""
    import httpx

    try:
        with path.open("rb") as fp:
            if client is None:
                with _transfer_client() as shared:
                    resp = shared.put(url, content=fp)
            else:
                resp = client.put(url, content=fp)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise TransferError(500, f"upload output bundle failed: {exc}") from exc


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_VALID_STATUSES = frozenset({"running", "completed", "failed", "cancelled"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _normalize_status(status: str) -> str:
    text = str(status or "").strip().lower()
    if text == "succeeded":
        text = "completed"
    return text if text in _VALID_STATUSES else "failed"


def _validate_protocol_version(transfer: dict) -> None:
    raw = transfer.get("protocolVersion")
    if raw in (None, "", 0):
        return
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise TransferError(400, f"invalid protocolVersion: {raw!r}") from exc
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise TransferError(
            400,
            f"unsupported protocolVersion {version}; supported {SUPPORTED_PROTOCOL_VERSIONS}",
        )


class _StatusWriter:
    """异步任务状态对象写入器（worker 预签名 PUT）。

    worker 异步提交时下发 ``statusUrl``（PUT）与 ``cancelUrl``（GET）：算子按
    ``heartbeatSeconds`` 周期重写状态对象兼作存活心跳，worker 轮询同一对象获取
    进度与终态。状态上报是旁路能力——任何网络错误都只忽略，不能影响执行。

    状态对象结构::

        {"status": "running|completed|failed|cancelled",
         "progress": 0-100, "message": "...", "updatedAt": "...",
         "result": {<operator-result>}}   # 仅终态
    """

    def __init__(self, transfer: dict | None):
        transfer = transfer or {}
        self.status_url = str(transfer.get("statusUrl") or "")
        self.cancel_url = str(transfer.get("cancelUrl") or "")
        try:
            self.interval = max(2, int(transfer.get("heartbeatSeconds") or 15))
        except (TypeError, ValueError):
            self.interval = 15
        self._lock = threading.Lock()
        self._payload: dict = {
            "status": "running",
            "progress": 0.0,
            "message": "",
            "updatedAt": _utcnow(),
        }
        self._cancelled = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_progress = -1.0
        self._last_progress_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.status_url)

    def start(self) -> None:
        if not self.enabled:
            return
        self.update("running", progress=0.0, message="accepted")
        self._thread = threading.Thread(target=self._beat, name="radstudio-status", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def update(
        self,
        status: str,
        progress: float | None = None,
        message: str | None = None,
        result: dict | None = None,
    ) -> None:
        if not self.enabled:
            return
        status = _normalize_status(status)
        with self._lock:
            current = str(self._payload.get("status") or "running")
            # 终态幂等：状态机只允许 running → terminal，终态不可回退/被覆盖。
            if current in _TERMINAL_STATUSES and status != current:
                return
            self._payload["status"] = status
            if progress is not None:
                self._payload["progress"] = round(float(progress), 2)
            if message is not None:
                self._payload["message"] = message
            if result is not None:
                self._payload["result"] = result
            self._payload["updatedAt"] = _utcnow()
            payload = dict(self._payload)
        self._put(payload)

    def progress(self, percent: float, message: str = "") -> None:
        """进度节流：同值不重复写，最多每 2 秒一次；心跳线程兜底刷新时间戳。"""
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            changed = abs(float(percent) - self._last_progress) >= 0.01 or (
                bool(message) and message != self._payload.get("message")
            )
            if not changed or now - self._last_progress_at < 2.0:
                return
            self._last_progress = float(percent)
            self._last_progress_at = now
        self.update("running", progress=percent, message=message or None)

    def finish(self, result: dict) -> None:
        status = str(result.get("status") or "failed")
        self.update(status, progress=100.0 if status == "completed" else None, result=result)

    def fail(self, message: str) -> None:
        self.update("failed", message=message)

    def _put(self, payload: dict) -> None:
        try:
            with _transfer_client() as client:
                resp = client.put(
                    self.status_url,
                    content=encode_runtime_json(payload),
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
        except Exception:  # noqa: BLE001, S110 - 状态上报失败不影响执行
            pass

    def _check_cancel(self) -> bool:
        if not self.cancel_url:
            return False
        try:
            with _transfer_client() as client:
                resp = client.get(self.cancel_url)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 - 取消检查失败按未取消处理
            return False

    def _beat(self) -> None:
        while not self._stopped.wait(self.interval):
            if not self._cancelled.is_set() and self._check_cancel():
                self._cancelled.set()
            with self._lock:
                self._payload["updatedAt"] = _utcnow()
                payload = dict(self._payload)
            self._put(payload)


def _materialize_input_manifest(store: BundleArtifactStore, manifest_url: str, client=None) -> None:
    """对象模式：拉取输入清单，并发下载逐文件物化到工作区并校验 sha256。

    每个文件下载完成即落盘，不跨文件累积字节——云函数内存受限，工作区
    整体大小远超内存时必须逐文件流经。清单为 worker 侧内容寻址去重上传后
    生成的 JSON：``{"version":1,"files":[{"path","sha256","size","url"}]}``。
    url 为预签名 GET，容器内不持有任何对象存储凭据。
    """
    raw = _http_get(manifest_url, client)
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TransferError(400, f"invalid input manifest: {exc}") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list):
        raise TransferError(400, "input manifest missing files array")
    _materialize_manifest_entries(store, files, client)


def _materialize_manifest_entries(store: BundleArtifactStore, files: list, client=None) -> None:
    """逐文件下载清单条目并物化（对象模式与挂载回退共用）。"""

    def fetch(entry: dict) -> None:
        if not isinstance(entry, dict):
            raise TransferError(400, "invalid manifest entry")
        path = entry.get("path")
        url = entry.get("url")
        if not path or not url:
            raise TransferError(400, "manifest entry missing path/url")
        data = _http_get(url, client)
        want = entry.get("sha256")
        if want and hashlib.sha256(data).hexdigest() != want:
            raise TransferError(400, f"manifest file sha256 mismatch: {path}")
        try:
            store.materialize_file(path, data)
        except ValueError as exc:
            raise TransferError(400, str(exc)) from exc

    if not files:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(files))) as pool:
        # 迭代消费以在工作线程抛出 TransferError 时立即传播。
        for _ in pool.map(fetch, files):
            pass


def _materialize_mount_inputs(store: BundleArtifactStore, transfer: dict, client=None) -> set[str]:
    """挂载模式：把 OSS 挂载点的输入文件映射进工作区，返回挂载路径集合。

    worker 只传对象键与预签名 GET（回退用），不传字节。挂载点缺失时（本地
    serve 冒烟/未配置挂载）回退为预签名下载，保证同一镜像两种环境都可运行。
    """
    # 零输入节点（如 FeatureFilter）的挂载列表为空；worker 旧版本可能省略
    # 该字段（omitempty），按空列表处理以兼容，避免协议误判为任务失败。
    entries = transfer.get("mountInputs") or []
    if not isinstance(entries, list):
        raise TransferError(400, "invalid mountInputs: expected array")
    root_value = (
        transfer.get("mountRoot") or os.environ.get("RADSTUDIO_FC_MOUNT_ROOT") or "/mnt/oss"
    )
    root = Path(root_value)
    mounted: set[str] = set()
    fallback: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise TransferError(400, "invalid mount entry")
        path, obj = entry.get("path"), entry.get("object")
        if not path or not obj:
            raise TransferError(400, "mount entry missing path/object")
        obj_path = Path(obj)
        if obj_path.is_absolute() or ".." in obj_path.parts:
            raise TransferError(400, f"mount object escapes root: {obj}")
        source = root / obj_path
        if source.is_file():
            try:
                store.mount_file(path, source)
            except ValueError as exc:
                raise TransferError(400, str(exc)) from exc
        else:
            fallback.append(entry)
        mounted.add(str(path).replace("\\", "/"))
    if fallback:
        _materialize_manifest_entries(store, fallback, client)
    return mounted


def load_operator_payload(data: bytes) -> dict:
    return decode_runtime_json(data, max_bytes=_MAX_BODY_BYTES)


# --- 用户算子（自定义 Python 算子）隔离执行 -------------------------------

#: 用户算子运行时开关：只有装了预置依赖白名单的用户算子镜像才允许装载用户代码
USER_OPERATORS_ENV = "RADSTUDIO_USER_OPERATORS"
_TRUTHY_ENV = {"1", "true", "yes", "on"}
_DEFAULT_ARTIFACT_KIND = "application/octet-stream"


def _user_operators_enabled() -> bool:
    """本镜像是否允许执行用户代码（默认关闭，内置算子镜像不受影响）。"""
    return str(os.environ.get(USER_OPERATORS_ENV) or "").strip().lower() in _TRUTHY_ENV


#: 用户代码包下载传输注入点（仅测试与本地联调用；生产走预签名 HTTPS GET）
_user_code_transport: Callable[[str, float], bytes] | None = None


def _user_spec_source(payload: dict) -> dict | None:
    """找出本次调用携带的用户代码段；没有则返回 None（走内置算子路径）。

    两个下发方的位置不同：工作台调试与归档包方式放在 body 顶层 ``userOperator``，
    工作流节点由 Go 的 ``userNodeExecutor`` 挂在 ``request.userCode`` 里（源码随标准
    算子协议一起下发）。两处都只认一个来源，同时出现时顶层优先。
    """
    section = payload.get("userOperator")
    if isinstance(section, dict):
        return section
    request_data = payload.get("request")
    if isinstance(request_data, dict):
        code = request_data.get("userCode")
        if isinstance(code, dict):
            return code
    return None


def _child_request(request_data: dict, spec: UserOperatorSpec, mode: str | None) -> dict:
    """交给子进程的请求副本：剥掉所有预签名地址与源码本身。

    预签名 URL 本身就是 capability，落进用户代码等于给了读写对象存储的口子；
    子进程只保留计算必需的字段，物化与上传全部留在父进程。``transfer`` 不参与
    调用契约摘要，因此剥离后按内容寻址的校验仍然一致。``userCode`` 是父进程已经
    落盘的源码快照，再随 stdin 传一遍只会放大请求体，也让用户代码读到 revision 元数据。
    """
    child = dict(request_data)
    child.pop("transfer", None)
    child.pop("userCode", None)
    if mode:
        child["transfer"] = {"mode": mode}
    # 入口信息附到 request 上：OperatorRequest.from_dict 忽略未知字段，
    # 子进程仍以 userOperator.entrypoint 为准，这里只是便于排障与自检。
    child["entrypoint"] = spec.entrypoint
    return child


def _user_failure(request: OperatorRequest, run: dict) -> OperatorResult:
    """把隔离进程层面的失败（超时/崩溃/协议错乱）转成算子结果。"""
    error = run.get("error") if isinstance(run.get("error"), dict) else {}
    details = dict(error.get("details") or {})
    details["logs"] = run.get("logs") or {}
    details["exitCode"] = run.get("exitCode")
    if run.get("timedOut"):
        details["timeoutSeconds"] = (details.get("timeoutSeconds") or 0) or None
    status = "cancelled" if run.get("cancelled") else "failed"
    return OperatorResult(
        requestId=request.requestId,
        runId=request.runId,
        nodeId=request.nodeId,
        status=status,
        error=OperatorError(
            code=str(error.get("code") or "user_protocol_error"),
            message=str(error.get("message") or "用户算子执行失败"),
            details={k: v for k, v in details.items() if v is not None},
        ),
    )


def _adopt_user_outputs(
    request: OperatorRequest, run: dict, store: BundleArtifactStore
) -> OperatorResult:
    """按磁盘真实内容登记并复核子进程产物，重建算子结果。

    子进程注册表随进程消失，父进程不能凭它声称的哈希放行产物：逐个文件重算
    SHA-256 并与输出引用比对，任何路径逃逸、文件缺失或摘要不符都直接失败。
    """
    data = run.get("result") or {}
    raw_error = data.get("error") if isinstance(data.get("error"), dict) else None
    error = (
        OperatorError(
            code=str(raw_error.get("code") or "operator.failed"),
            message=str(raw_error.get("message") or ""),
            details=dict(raw_error.get("details") or {}),
        )
        if raw_error
        else None
    )
    status = str(data.get("status") or "failed")
    if status not in ("completed", "failed", "cancelled"):
        status = "failed"
    outputs = data.get("outputs") if isinstance(data.get("outputs"), dict) else {}
    result = OperatorResult(
        requestId=str(data.get("requestId") or request.requestId),
        runId=str(data.get("runId") or request.runId),
        nodeId=str(data.get("nodeId") or request.nodeId),
        status=status,
        outputs={str(port): str(ref) for port, ref in outputs.items()},
        metrics=data.get("metrics") if isinstance(data.get("metrics"), dict) else {},
        error=error,
        executionContract=(
            data.get("executionContract") if isinstance(data.get("executionContract"), dict) else None
        ),
        implementation=(
            data.get("implementation") if isinstance(data.get("implementation"), dict) else None
        ),
    )
    if status != "completed":
        if error is not None:
            error.details.setdefault("logs", run.get("logs") or {})
            error.details.setdefault("exitCode", run.get("exitCode"))
        return result

    # 产物归属：子进程写了哪些文件由它自报，但登记与校验一律在父进程按磁盘内容重做。
    try:
        adopted = store.adopt_written(run.get("writtenFiles") or [])
        kinds: dict[str, str] = {}
        for meta in data.get("artifacts") or []:
            if isinstance(meta, dict) and meta.get("storagePath"):
                kinds[str(meta["storagePath"])] = str(meta.get("type") or _DEFAULT_ARTIFACT_KIND)
        for ref in result.outputs.values():
            info = parse_ref(ref)
            kinds.setdefault(str(info["path"]), str(info["type"]))
        for name in sorted(adopted):
            store.register(name, kinds.get(name) or _DEFAULT_ARTIFACT_KIND)
        verified: dict[str, str] = {}
        for port, ref in result.outputs.items():
            info = parse_ref(ref)
            path = workspace_path(store.root, str(info["path"]), must_exist=True)
            digest, _size = hash_file(path)
            if digest != str(info["sha256"]):
                raise ArtifactBoundaryError(
                    "artifact.hash_mismatch",
                    f"用户算子产物摘要不匹配：{info['path']}",
                    {"path": str(info["path"]), "expected": str(info["sha256"]), "actual": digest},
                )
            verified[port] = make_ref(str(info["path"]), digest, str(info["type"]))
        result.outputs = verified
        result.artifacts = [
            meta for meta in store.artifacts() if meta.storagePath in adopted
        ]
    except (ArtifactBoundaryError, UserOperatorError, ValueError) as exc:
        code = str(getattr(exc, "code", "user_artifact_invalid"))
        result.status = "failed"
        result.outputs = {}
        result.artifacts = []
        result.error = OperatorError(
            code=code,
            message=str(exc),
            details={
                "logs": run.get("logs") or {},
                "exitCode": run.get("exitCode"),
                **dict(getattr(exc, "details", None) or {}),
            },
        )
    return result


def _invoke_user_operator(
    *,
    request: OperatorRequest,
    request_data: dict,
    payload: dict,
    spec: UserOperatorSpec,
    store: BundleArtifactStore,
    workdir: Path,
    mode: str | None,
    emit,
    writer: _StatusWriter,
) -> OperatorResult:
    """装载并隔离执行一次用户算子（常驻进程绝不 import 用户代码）。"""
    sandbox = Path(tempfile.mkdtemp(prefix="radstudio-user-"))
    code_dir = sandbox / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        try:
            entries = fetch_from_spec(spec, code_dir, transport=_user_code_transport)
        except UserOperatorError as exc:
            return OperatorResult(
                requestId=request.requestId,
                runId=request.runId,
                nodeId=request.nodeId,
                status="failed",
                error=OperatorError(code=exc.code, message=str(exc), details=exc.details),
            )
        run = run_user_operator_isolated(
            request=_child_request(request_data, spec, mode),
            code_dir=code_dir,
            workspace_root=workdir,
            sandbox_dir=sandbox,
            entrypoint=spec.entrypoint,
            allow_packages=list(spec.allow_packages),
            timeout_seconds=spec.timeout_seconds,
            on_event=emit,
            cancel_check=writer.is_cancelled,
        )
        result = (
            _adopt_user_outputs(request, run, store)
            if isinstance(run.get("result"), dict)
            else _user_failure(request, run)
        )
        logs = run.get("logs") or {}
        result.metrics["userOperator"] = {
            "entrypoint": spec.entrypoint,
            "codeSha256": spec.code_sha256,
            "codeEntries": len(entries),
            "allowPackages": list(spec.allow_packages),
            "timeoutSeconds": spec.timeout_seconds,
            "exitCode": run.get("exitCode"),
            "timedOut": bool(run.get("timedOut")),
            "processDurationMs": int((time.monotonic() - started) * 1000),
            "stdoutBytes": len(str(logs.get("stdout") or "")),
            "stderrBytes": len(str(logs.get("stderr") or "")),
        }
        return result
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def execute_payload(payload: dict) -> OperatorResult:
    """执行一次算子调用，处理内联（inline）与带外（object/mount）传输模式。

    内联模式：输入取 body 的 inputBundle（tar.gz base64），输出写 result.outputBundle。
    带外模式：输入经清单或 OSS 挂载物化，输出经预签名 PUT 上传，result.transfer
    回填 outputSha256/outputSize，body 不内联工作区。

    异步模式（transfer.statusUrl 存在）：执行期间把进度/心跳写状态对象，
    worker 轮询获知进度与终态；worker 取消时通过 cancelUrl 标记，算子在下一次
    ctx.check_cancelled() 处退出。

    请求带 ``userOperator`` 段时改走隔离执行路径：代码包物化到本次调用独占的临时
    目录，在独立子进程里导入并运行，父进程只做拉包、物化、状态上报与产物导出。

    输入非法或传输失败抛 TransferError。
    """
    # 大请求（如 DataInput 的数百条样本清单）超过 FC 异步调用 128KB 上限时，
    # worker 把完整 payload 转存对象存储，body 只带预签名 GET。
    if (
        isinstance(payload, dict)
        and not isinstance(payload.get("request"), dict)
        and payload.get("requestRef")
    ):
        raw = _http_get(str(payload["requestRef"]))
        try:
            payload = load_operator_payload(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise TransferError(400, f"invalid requestRef payload: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
        raise TransferError(400, 'body must be {"request": {...}}')
    try:
        request = OperatorRequest.from_dict(payload["request"])
    except (ValueError, TypeError) as exc:
        raise TransferError(400, f"invalid operator request: {exc}") from exc

    # 用户算子：请求里的用户代码段决定本次调用是否走隔离装载路径。
    # 先校验规格再看开关，非法规格是永久错误（400，worker 不重试）；本镜像未启用
    # 用户算子时按 403 拒绝——内置算子镜像里根本没有预置依赖。
    spec: UserOperatorSpec | None = None
    spec_source = _user_spec_source(payload)
    if spec_source is not None:
        try:
            spec = parse_user_operator_spec(spec_source)
        except UserOperatorError as exc:
            raise TransferError(400, f"invalid userOperator: {exc}") from exc
        if not _user_operators_enabled():
            raise TransferError(
                403,
                f"user operators are disabled in this runtime; set {USER_OPERATORS_ENV}=1"
                " on the approved user-operator image",
            )

    transfer = request.transfer or {}
    mode = transfer.get("mode")
    writer = _StatusWriter(transfer)
    writer.start()
    invocation_started_at = _utcnow()
    invocation_started = time.monotonic()
    workdir = Path(tempfile.mkdtemp(prefix="radstudio-serve-"))
    try:
        _validate_protocol_version(transfer)
        store = BundleArtifactStore(workdir)
        try:
            validate_execution_contract(request)
            invocation_artifact_ports(request)
            if spec is None:
                # 内置算子必须在常驻进程里就能解析到注册项；用户算子的注册表只存在于
                # 隔离子进程内，这里提前 resolve 会以 operator.type_unavailable 误杀。
                resolve_invocation(request.operatorType, request.operatorVersion, True)
        except (OperatorResolutionError, ExecutionContractError) as error:
            result = OperatorResult(
                requestId=request.requestId,
                runId=request.runId,
                nodeId=request.nodeId,
                status="failed",
                error=OperatorError(code=error.code, message=str(error), details=error.details),
            )
        else:
            mounted_paths: set[str] = set()
            try:
                if mode == "mount":
                    with _transfer_client() as client:
                        mounted_paths = _materialize_mount_inputs(store, transfer, client)
                elif mode == "object":
                    manifest_url = transfer.get("inputManifestUrl")
                    if manifest_url:
                        with _transfer_client() as client:
                            _materialize_input_manifest(store, manifest_url, client)
                elif payload.get("inputBundle"):
                    store.import_bundle(payload["inputBundle"])
            except (ValueError, tarfile.TarError) as exc:
                raise TransferError(400, f"invalid input bundle: {exc}") from exc

            def emit(event: str, data: dict) -> None:
                if event == "node.progress":
                    try:
                        percent = float(data.get("percent", 0.0))
                    except (TypeError, ValueError):
                        percent = 0.0
                    writer.progress(percent, str(data.get("message") or ""))

            # 读取节点按工作区相对路径解析快照文件：执行端把输入物化到 store.root，
            # 这里把根目录交给内置算子；注入发生在 invoke 校验调用契约之后，
            # 避免把平台布局字段算进冻结摘要。用户算子的独立信封需要提前注入。
            if spec is not None:
                if request.environment is None:
                    request.environment = {}
                request.environment.setdefault("workspaceRoot", str(store.root))
            # Preserve the requested version. Only the explicit bundled legacy alias table may adapt a deployment tag.
            if spec is not None:
                # 用户算子：常驻进程绝不 import 用户代码，计算全部在隔离子进程里完成，
                # 心跳/进度/取消仍由外层 writer 负责（子进程只把事件写回 stdout）。
                result = _invoke_user_operator(
                    request=request,
                    request_data=payload["request"],
                    payload=payload,
                    spec=spec,
                    store=store,
                    workdir=workdir,
                    mode=mode,
                    emit=emit,
                    writer=writer,
                )
            else:
                result = invoke(
                    request,
                    store,
                    emit=emit,
                    cancel_check=writer.is_cancelled,
                    allow_legacy_fc_aliases=True,
                    workspace_root=str(store.root),
                )
            if result.status == "completed":
                # 输出清单间接引用的透传输入随输出包回传（挂载输入除外——下游经
                # OSS 直读，不再把大影像打包回传）。
                store.track_referenced_outputs(skip=mounted_paths)
                if mode in ("object", "mount"):
                    out_path = workdir / "output.tar.gz"
                    size, sha = store.export_to(out_path)
                    output_url = transfer.get("outputUrl")
                    if output_url:
                        with _transfer_client() as client:
                            _http_put_file(output_url, out_path, client)
                    result.transfer = {
                        "mode": mode,
                        "outputSha256": sha,
                        "outputSize": size,
                    }
                else:
                    result.outputBundle = store.export_bundle()
        # 调用级计费：执行端自报时长（含输入物化与输出上传），Go 侧以此为
        # 首选口径；缺失时回退 worker 墙钟时间。
        result.metrics["invocation"] = {
            "startedAt": invocation_started_at,
            "finishedAt": _utcnow(),
            "durationMs": int((time.monotonic() - invocation_started) * 1000),
        }
        writer.finish(result.to_dict())
        return result
    except Exception as exc:
        # 异步调用：worker 靠状态对象判活，异常必须立即写 failed，否则只能等
        # 心跳超时（默认 5 分钟）。状态上报失败不影响异常继续上抛。
        writer.fail(str(exc) or type(exc).__name__)
        raise
    finally:
        writer.stop()
        shutil.rmtree(workdir, ignore_errors=True)


def load_builtin_operators() -> None:
    """导入内置算子模块以触发 @operator 注册（模块不存在时跳过）。

    深度学习算子模块（operators_dl）缺失 torch 时跳过导入，避免基础
    算子镜像（无 [dl] extra）启动失败；调用 DL 算子时给出明确报错。
    """
    for module_name in (
        "radstudio.operators_bench",
        "radstudio.operators_builtin",
        "radstudio.operators_figures",
        "radstudio.operators_hybrid",
        "radstudio.operators_ibsi",
        "radstudio.operators_prep",
        "radstudio.operators_radiomics",
        "radstudio.operators_readers",
        "radstudio.operators_sc",
        "radstudio.operators_volumes",
    ):
        if find_spec(module_name) is not None:
            __import__(module_name)
    for module_name in ("radstudio.operators_dl", "radstudio.operators_seg"):
        try:
            __import__(module_name)
        except ImportError:
            pass


class OperatorServeHandler(BaseHTTPRequestHandler):
    """算子 serve 模式 HTTP 处理器。"""

    def _send_json(self, status: int, payload: dict) -> None:
        body = encode_runtime_json(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_denied(self) -> int | None:
        """返回拒绝状态码（401/503）；None 表示认证通过。"""
        expected = os.environ.get("RADSTUDIO_OPERATOR_TOKEN", "")
        if not expected:
            return 503
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return 401
        if not hmac.compare_digest(header[len("Bearer ") :], expected):
            return 401
        return None

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
        try:
            result = execute_payload(payload)
        except TransferError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        self._send_json(200, {"result": result.to_dict()})


def run_once(input_path: str, output_path: str) -> int:
    """单次调用模式（docker 执行器容器入口）。

    读入 ``{"request": <operator-request>, "inputBundle": "<tar.gz base64>"}``，
    执行算子后写出 ``{"result": <operator-result>}``；成功时 result.outputBundle
    为输出工作区。退出码：0=completed，1=failed（结果细节以 result.json 为准）。
    """
    payload = load_operator_payload(Path(input_path).read_bytes())
    if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
        raise TypeError('input must be {"request": {...}, "inputBundle": "..."}')
    result = execute_payload(payload)
    Path(output_path).write_text(
        encode_runtime_json({"result": result.to_dict()}).decode("utf-8"), encoding="utf-8"
    )
    return 0 if result.status == "completed" else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="RadStudio 算子服务（serve / once 模式）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument("--once", action="store_true", help="单次调用模式（非 HTTP 常驻）")
    parser.add_argument("--in", dest="input", help="once 模式：请求 JSON 文件路径")
    parser.add_argument("--out", dest="output", help="once 模式：结果 JSON 文件路径")
    args = parser.parse_args(argv)
    load_builtin_operators()
    if args.once:
        if not args.input or not args.output:
            parser.error("--once requires --in and --out")
        sys.exit(run_once(args.input, args.output))
    server = ThreadingHTTPServer((args.host, args.port), OperatorServeHandler)
    print(f"radstudio operator_serve listening on http://{args.host}:{args.port}", flush=True)
    # 云函数平台回收实例前发 SIGTERM：shutdown 必须从 serve_forever 之外的线程调用。
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
