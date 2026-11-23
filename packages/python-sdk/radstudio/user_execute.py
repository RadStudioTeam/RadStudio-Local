"""用户算子隔离子进程入口（``python -m radstudio.user_execute``）。

父进程（``radstudio.operator_serve``）负责输入物化、状态上报与产物导出；本进程只做三件事：

1. 从 stdin 读入本次调用的请求信封（唯一输入通道，不接参数、不接环境变量里的凭据）。
2. 把用户代码目录加入 ``sys.path``、安装依赖白名单守卫，再导入用户模块触发
   ``@operator(...)`` 注册——**注册表只活在本进程内**，退出即消失。
3. 走 SDK 既有执行路径（:func:`radstudio.operator.invoke`）并把结果按行协议写回 stdout。

通信协议（与 :mod:`radstudio.user_operator` 一致）::

    RADSTUDIO_EVENT\\t<json>    # node.progress 等事件，父进程转交状态写入器
    RADSTUDIO_RESULT\\t<json>   # 末行：{"operatorResult":…, "writtenFiles": […], "status": …}

本模块**不导入 httpx**：白名单守卫会拒绝它，网络传输一律留在父进程。
"""

from __future__ import annotations

import importlib
import io
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from .json_contract import decode_runtime_json, encode_runtime_json
from .user_exception import describe_exception
from .user_operator import (
    DEFAULT_ALLOW_PACKAGES,
    EVENT_PREFIX,
    MAX_CHILD_STDIN_BYTES,
    RESULT_PREFIX,
    DependencyBlockedError,
    PlatformImportBlockedError,
    UserOperatorError,
    find_shadowed_modules,
    make_import_guard,
    split_entrypoint,
    top_level_import_names,
)

#: 装载用户代码前必须就位的核心平台模块；先导入是为了让 jsonschema 等第三方依赖
#: 在守卫安装之前完成加载（守卫只约束**之后**发生的 import）。
_PRELOAD_MODULES = (
    "radstudio.operator",
    "radstudio.execution_contract",
    "radstudio.content_contract",
    "radstudio.port_contract",
    "radstudio.parameter_validation",
    "radstudio.artifact_boundary",
    "radstudio.json_contract",
)

_stdout_lock = threading.Lock()

#: 回传给用户的栈摘要长度（父进程再按日志上限截断）
_TRACEBACK_TAIL = 4000


def _protocol_error(code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    return {
        "protocolVersion": 1,
        "status": "failed",
        "operatorResult": None,
        "writtenFiles": [],
        "error": {"code": code, "message": message, "details": details or {}},
    }


def write_line(prefix: str, payload: Any, stream: io.BufferedWriter | None = None) -> None:
    """把一行协议消息原子写入 stdout 并立即 flush。

    用户代码可以随意 ``print``，因此协议消息必须整行写出（一次 write + 一次 flush），
    否则会与用户的半行输出交错，父进程再也解析不出结果。
    """
    data = (prefix + encode_runtime_json(payload).decode("utf-8") + "\n").encode("utf-8")
    target = stream if stream is not None else sys.stdout.buffer
    with _stdout_lock:
        target.write(data)
        target.flush()


def read_request(max_bytes: int = MAX_CHILD_STDIN_BYTES) -> dict:
    """从 stdin 读取请求信封（字节上限保护，超限即失败）。"""
    raw = sys.stdin.buffer.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise UserOperatorError(
            "code_too_large", f"子进程 stdin 请求超过 {max_bytes} 字节上限", {}
        )
    if not raw.strip():
        raise UserOperatorError("user_request_invalid", "子进程未收到算子请求", {})
    try:
        envelope = decode_runtime_json(raw, max_bytes=max_bytes)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise UserOperatorError("user_request_invalid", f"算子请求 JSON 无效：{exc}") from exc
    if not isinstance(envelope, dict):
        raise UserOperatorError("user_request_invalid", "算子请求信封必须是 JSON 对象", {})
    return envelope


def install_guard(allow_packages: list[str], user_names: set[str]) -> None:
    """安装依赖白名单审计钩子。

    ``sys.addaudithook`` 一经注册不可撤销，正好符合"本次调用的子进程只服务本次调用"；
    用户代码目录自身的顶层模块名要放行，否则连入口模块都 import 不进来。
    """
    guard = make_import_guard(
        allow_packages,
        extra_allowed=sorted(user_names),
        available_hint=list(allow_packages) or list(DEFAULT_ALLOW_PACKAGES),
    )
    if not hasattr(sys, "addaudithook"):  # pragma: no cover - Python < 3.8 的兜底
        raise UserOperatorError("user_runtime_unsupported", "当前解释器不支持审计钩子", {})
    sys.addaudithook(guard)


def resolve_entry_class(module: str, attribute: str) -> tuple[Any, Any]:
    """导入用户模块并按 ``Outer.Inner`` 属性链取到入口类。"""
    imported = importlib.import_module(module)
    target: Any = imported
    for part in attribute.split("."):
        try:
            target = getattr(target, part)
        except AttributeError as exc:
            raise UserOperatorError(
                "user_entrypoint_missing",
                f"用户代码入口 {module}:{attribute} 不存在（缺少属性 {part}）",
                {"module": module, "attribute": attribute},
            ) from exc
    return imported, target


def _exception_site(exc: BaseException, source_root: Path) -> dict[str, Any]:
    """结构化异常现场；采集本身出问题也不能吞掉用户已经拿到的错误码。"""
    try:
        return describe_exception(exc, source_root)
    except Exception:  # noqa: BLE001 - 现场是附加信息，不是结果本身
        return {}


def _child_usage() -> dict[str, int]:
    """本次子进程自己的峰值常驻内存（字节），由父进程原样转交、不做二次累加。

    父进程读 ``RUSAGE_CHILDREN`` 拿到的是「该进程启动以来所有子进程的最大值」：云函数实例
    常驻且 ``instanceConcurrency=1``，上一次调用的峰值会被算到这一次头上。所以让子进程自己读
    ``RUSAGE_SELF``（Linux 下单位是 KB）。Windows 没有 ``resource``，直接不报这一项。
    """
    try:
        import resource
    except ImportError:
        return {}
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)
    if sys.platform.startswith("linux"):
        peak *= 1024
    return {"peakRssBytes": peak} if peak > 0 else {}


def run() -> dict:
    """执行一次用户算子调用，返回要写回父进程的结果信封。"""
    envelope = read_request()
    request = envelope.get("request")
    workspace_root = envelope.get("workspaceRoot")
    spec = envelope.get("userOperator")
    if not isinstance(request, dict) or not isinstance(spec, dict) or not workspace_root:
        raise UserOperatorError(
            "user_request_invalid",
            "请求信封缺少 request/workspaceRoot/userOperator",
            {"keys": sorted(str(k) for k in envelope)},
        )
    code_dir = Path(str(spec.get("codeDir") or "")).resolve()
    entrypoint = str(spec.get("entrypoint") or "")
    allow_packages = [str(item) for item in (spec.get("allowPackages") or [])]
    if not code_dir.is_dir():
        raise UserOperatorError(
            "user_code_missing", f"用户代码目录不存在：{code_dir}", {"codeDir": str(code_dir)}
        )
    module_name, attribute = split_entrypoint(entrypoint)
    root = Path(str(workspace_root)).resolve()
    root.mkdir(parents=True, exist_ok=True)

    shadowed = find_shadowed_modules(code_dir)
    if shadowed:
        raise UserOperatorError(
            "user_code_shadowing",
            f"用户代码不得覆盖平台或标准库模块：{', '.join(shadowed)}",
            {"modules": shadowed},
        )

    # sys.path 顺序：用户代码目录优先，确保用户包内的相对导入指向自己而非同名第三方。
    code_dir_str = str(code_dir)
    if code_dir_str not in sys.path:
        sys.path.insert(0, code_dir_str)
    for name in _PRELOAD_MODULES:
        importlib.import_module(name)
    install_guard(allow_packages, top_level_import_names(code_dir))

    from .operator import (
        OPERATOR_REGISTRY,
        BundleArtifactStore,
        OperatorResolutionError,
        invoke,
        resolve_operator,
    )

    store = BundleArtifactStore(root)

    def emit(event: str, data: dict) -> None:
        write_line(EVENT_PREFIX, {"event": event, "data": dict(data or {})})

    try:
        imported, _entry_class = resolve_entry_class(module_name, attribute)
    except DependencyBlockedError as exc:
        raise UserOperatorError(
            "user_dependency_blocked",
            str(exc),
            {"module": module_name, "entrypoint": entrypoint},
        ) from exc
    except PlatformImportBlockedError as exc:
        raise UserOperatorError("user_import_blocked", str(exc), {"module": module_name}) from exc
    except ModuleNotFoundError as exc:
        raise UserOperatorError(
            "user_module_missing",
            f"用户代码里没有模块 {module_name}：{exc}",
            {"module": module_name, "topLevel": sorted(top_level_import_names(code_dir))},
        ) from exc
    except UserOperatorError:
        raise
    # 用户代码在 import 期炸了：栈要原样回传给用户，但对外仍是一个稳定的错误码。
    except Exception as exc:
        details: dict[str, Any] = {"traceback": traceback.format_exc()[-_TRACEBACK_TAIL:]}
        site = _exception_site(exc, code_dir)
        if site:
            details["exception"] = site
        raise UserOperatorError(
            "user_import_failed",
            f"导入用户代码 {module_name} 失败：{type(exc).__name__}: {exc}",
            details,
        ) from exc

    # 用户模块 import 完成 → @operator 装饰器已在**本进程**注册表里落位。
    # 先按请求的 type/version 显式解析，未命中时给出可自助排查的报错（列出实际注册到的键），
    # 命中后才交给 invoke() 走既有执行路径（契约校验、产物登记、进度事件）。
    requested_type = str(request.get("operatorType") or "")
    requested_version = request.get("operatorVersion")
    try:
        resolve_operator(requested_type, requested_version)
    except OperatorResolutionError as exc:
        raise UserOperatorError(
            "user_not_registered",
            f"用户代码未注册请求的算子：{exc}",
            {
                "entrypoint": entrypoint,
                "module": imported.__name__,
                "requested": _requested_key(request),
                "registered": sorted(str(key) for key in OPERATOR_REGISTRY),
            },
        ) from exc

    result = invoke(
        request,
        store,
        emit=emit,
        cancel_check=None,
        allow_legacy_fc_aliases=False,
        capture_traceback=True,
        traceback_source_root=code_dir,
    )
    return {
        "protocolVersion": 1,
        "status": result.status,
        "operatorResult": result.to_dict(),
        "writtenFiles": sorted(store.written_names()),
        "usage": _child_usage(),
        "error": result.error.to_dict() if result.error is not None else None,
    }


def _requested_key(request: dict) -> str:
    version = request.get("operatorVersion")
    operator_type = str(request.get("operatorType") or "")
    return f"{operator_type}@{version}" if version else operator_type


def main() -> int:
    """子进程主流程：任何异常都转成结果信封写回，绝不把裸栈丢给父进程。"""
    # 用户代码里的 print 与协议行共用 stdout：先握住真正的输出流，再交给用户折腾。
    out = getattr(sys.stdout, "buffer", None) or io.BytesIO()
    try:
        envelope = run()
    except UserOperatorError as exc:
        write_line(RESULT_PREFIX, _protocol_error(exc.code, str(exc), exc.details), out)
        return 0
    except (DependencyBlockedError, PlatformImportBlockedError) as exc:
        code = getattr(exc, "code", "user_dependency_blocked")
        write_line(RESULT_PREFIX, _protocol_error(code, str(exc)), out)
        return 0
    except (ValueError, TypeError) as exc:
        write_line(RESULT_PREFIX, _protocol_error("user_request_invalid", str(exc)), out)
        return 0
    except BaseException as exc:  # noqa: BLE001 - 兜底：宁可回 failed 也不留空 stdout
        write_line(
            RESULT_PREFIX,
            _protocol_error(
                "user_runner_failed",
                f"{type(exc).__name__}: {exc}",
                {"traceback": traceback.format_exc()[-_TRACEBACK_TAIL:]},
            ),
            out,
        )
        return 0
    write_line(RESULT_PREFIX, envelope, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
