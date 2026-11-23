"""自定义 Python 算子的装载与隔离执行。

设计红线：**常驻 serve 进程绝不 import 用户代码**。用户代码按预签名 URL 拉到本次
调用独占的临时目录，只在独立子进程（``python -m radstudio.user_execute``）里导入并
执行；子进程退出后注册表、``sys.path`` 与一切全局状态随之销毁，同一云函数实例上的
其他请求不受影响。

三个能力：

- :func:`fetch_user_code`：HTTPS GET 拉包 → 校验 SHA-256 → 安全解包（防 zip-slip、
  绝对路径、``..`` 逃逸、符号链接与条目数/字节数超限）。
- :func:`build_scrubbed_env`：子进程环境脱敏，只保留解释器所需的最小变量集，
  显式剔除平台令牌与云凭据。
- :func:`make_import_guard` 与 :func:`run_user_operator_isolated`：依赖白名单守卫、
  超时（杀整个进程组）、协作取消与日志尾部回传。

子进程与父进程的通信协议（行协议，写在 stdout 上）::

    RADSTUDIO_EVENT\t<json>    # 算子事件（node.progress 等），由父进程转交状态写入器
    RADSTUDIO_RESULT\t<json>   # 最后一行：{"operatorResult":…, "writtenFiles": […]}

其余 stdout 行按用户代码的普通输出计入日志。用户代码随时可能 print，因此结果用前缀
行框定，而不是把裸 JSON 当作 stdout 的全部内容。
"""

from __future__ import annotations

import codecs
import hashlib
import io
import math
import os
import queue
import re
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .json_contract import decode_runtime_json, encode_runtime_json

# --- 协议与限额 -----------------------------------------------------------

EVENT_PREFIX = "RADSTUDIO_EVENT\t"
RESULT_PREFIX = "RADSTUDIO_RESULT\t"

DEFAULT_MAX_ENTRIES = 50
DEFAULT_MAX_FILE_BYTES = 512 * 1024
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024
#: 压缩包本身上限（解压后受 max_total_bytes 约束，这里是下载阶段的独立上限）
DEFAULT_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_TIMEOUT_SECONDS = 3600.0
#: 子进程 stdin 能接收的请求 JSON 上限（超限说明请求被篡改或异常巨大）
MAX_CHILD_STDIN_BYTES = 64 * 1024 * 1024
#: 子进程 stdout 累计读取上限，超出后丢弃普通输出行（协议行仍会解析）
MAX_CHILD_STDOUT_BYTES = 8 * 1024 * 1024
DEFAULT_LOG_TAIL_BYTES = 8 * 1024

#: 未声明时的预置依赖白名单（与 Dockerfile.operator-user 烘焙的轮子一致）
DEFAULT_ALLOW_PACKAGES = ("numpy", "pandas", "scipy", "sklearn", "PIL")

#: 超长 stdout 行的替代日志文本（写入时再编码，避免非 ASCII 字节字面量）
STDOUT_OVERFLOW_NOTE = "…子进程输出行超出上限，已丢弃\n"


class UserOperatorError(Exception):
    """用户算子装载/执行错误；``code`` 会原样进入算子结果或 HTTP 响应。"""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class DependencyBlockedError(ImportError):
    """用户代码导入了白名单之外的第三方包。

    必须是 ImportError 子类：审计钩子只允许以 ImportError 中止本次 import。
    """

    code = "user_dependency_blocked"


class PlatformImportBlockedError(ImportError):
    """用户代码导入了平台明令禁止的模块（默认不启用，见 ``BLOCKED_STDLIB_ROOTS``）。"""

    code = "user_import_blocked"


#: 始终允许导入根包（平台 SDK 自身；用户代码需要 ``from radstudio.operator import ...``）
PLATFORM_IMPORT_ROOTS = frozenset({"radstudio"})

#: 默认为空：预置依赖（joblib/loky 等）会经 multiprocessing.reduction 间接 import
#: socket，默认封锁会造成误伤。真正的边界是独立云函数 + 零平台凭据 + 实例不并发复用，
#: 需要收紧时由调用方传入 blocked 参数（网络/子进程逃逸在 FC 侧另行处置）。
BLOCKED_STDLIB_ROOTS: frozenset[str] = frozenset()

#: 发行名与导入名不同的常见包（键为 PEP 503 规范化发行名，用户按任一种写法声明都能放行）
PACKAGE_IMPORT_ALIASES = {
    "pillow": "PIL",
    "scikit-learn": "sklearn",
    "scikit-image": "skimage",
    "opencv-python": "cv2",
    "pyradiomics": "radiomics",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
}

#: 依赖展开的最大层数（只展开预置依赖的直接依赖，避免把整站 site-packages 放行）
MAX_DEPENDENCY_DEPTH = 2

# --- 环境脱敏 -------------------------------------------------------------

#: 从父进程环境原样透传的键（其余一律丢弃，白名单而非黑名单）
_ENV_PASSTHROUGH_KEYS = ("PATH", "LANG", "LC_ALL", "TZ")
#: Windows 上解释器/子进程创建必需的键（不是秘密，缺失会让 python.exe 起不来）
_WINDOWS_ENV_KEYS = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "SYSTEMDRIVE",
    "HOMEDRIVE",
)
#: 组装完成后仍受保护、绝不被删除的键（均为解释器运行必需、且不含凭据语义）
#: 注意 PYTHONHOME 不在其中：白名单式构造本就不带上它，保留会让用户环境劫持标准库定位。
_ENV_PROTECTED_KEYS = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        *_WINDOWS_ENV_KEYS,
    }
)
_ENV_DENY_PREFIXES = (
    "RADSTUDIO_",
    "AWS_",
    "ALIBABA_",
    "ALIYUN_",
    "OSS_",
    "S3_",
    "ACCESS_KEY",
    "SECRET",
    "TENCENT",
    "QCLOUD",
    "MINIO",
    "GOOGLE_CREDENTIAL",
    "CLOUDSDK",
)
_ENV_DENY_TOKENS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "APIKEY")


def _env_key_denied(name: str) -> bool:
    """判定环境变量是否必须从子进程环境中剔除。"""
    upper = str(name).upper()
    if upper in _ENV_PROTECTED_KEYS:
        return False
    if upper.startswith(_ENV_DENY_PREFIXES):
        return True
    return any(token in upper for token in _ENV_DENY_TOKENS)


def build_scrubbed_env(
    *,
    code_dir: Path | str,
    sandbox_dir: Path | str,
    base_env: Mapping[str, str] | None = None,
    sdk_parent: Path | str | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构造子进程环境：最小透传集 + 指向本次独占目录，且绝不携带平台凭据。

    ``PYTHONHOME`` 有意不保留（会劫持解释器自身的标准库定位）；``PYTHONPATH`` 由本
    函数重建为「用户代码目录 + SDK 目录」，不继承父进程取值。
    """
    source = dict(os.environ if base_env is None else base_env)
    windows = os.name == "nt"
    # Windows 环境变量键大小写不敏感，统一按大写取值后再以规范名写回。
    lookup: dict[str, str] = (
        {str(key).upper(): value for key, value in source.items()} if windows else dict(source)
    )

    def pick(key: str) -> str | None:
        return lookup.get(key.upper() if windows else key)

    env: dict[str, str] = {}
    for key in _ENV_PASSTHROUGH_KEYS + (_WINDOWS_ENV_KEYS if windows else ()):
        value = pick(key)
        if value is not None:
            env[key] = value

    roots = [str(Path(code_dir).resolve())]
    sdk_root = Path(sdk_parent).resolve() if sdk_parent is not None else _sdk_parent()
    sdk_path = str(sdk_root)
    if sdk_path not in roots:
        roots.append(sdk_path)
    env["PYTHONPATH"] = os.pathsep.join(roots)

    sandbox = str(Path(sandbox_dir).resolve())
    env["HOME"] = sandbox
    if windows:
        env["TEMP"] = sandbox
        env["TMP"] = sandbox
    else:
        env["TMPDIR"] = sandbox
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"

    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    # 最后一道闸：即便调用方通过 extra 试图注入令牌，也会被剔除（受保护键除外）。
    return {key: value for key, value in env.items() if not _env_key_denied(key)}


def _sdk_parent() -> Path:
    """radstudio 包的父目录（子进程经 PYTHONPATH 找到 SDK 与入口模块）。"""
    return Path(__file__).resolve().parent.parent


# --- 代码包拉取与安全解包 -------------------------------------------------

_HASH_LENGTH = 64


def _is_valid_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HASH_LENGTH
        and all(ch in "0123456789abcdef" for ch in value.lower())
    )


def validate_code_sha256(value: str) -> str:
    """校验并规范化代码包摘要（小写十六进制 64 位）。"""
    sha256 = str(value or "").strip().lower()
    if not _is_valid_sha256(sha256):
        raise UserOperatorError("code_hash_invalid", "用户代码包 SHA-256 摘要格式无效")
    return sha256


# --- 请求里的 userOperator 段 ---------------------------------------------

#: 依赖声明名允许的字符（PEP 508 发行名的保守子集；拒绝路径分隔符与空格）
_PACKAGE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: 用户侧可声明的上限天花板：请求只能收紧、不能放宽平台默认保护
USER_LIMIT_CEILINGS = {
    "maxEntries": 200,
    "maxFileBytes": 8 * 1024 * 1024,
    "maxTotalBytes": 32 * 1024 * 1024,
    "maxArchiveBytes": 64 * 1024 * 1024,
    "allowPackages": 32,
}


@dataclass(frozen=True)
class InlineCodeFile:
    """内联下发的一份源码（工作台调试与工作流节点走这条路，不经预签名 URL）。"""

    path: str
    content: str
    sha256: str = ""


@dataclass(frozen=True)
class UserOperatorSpec:
    """一次调用要装载执行的用户代码包及其资源上限。

    两种装载来源互斥：``code_url``（预签名归档包）或 ``files``（内联源码）。
    """

    code_url: str
    code_sha256: str
    entrypoint: str
    allow_packages: tuple[str, ...]
    timeout_seconds: float
    max_entries: int
    max_file_bytes: int
    max_total_bytes: int
    max_archive_bytes: int
    download_timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    files: tuple[InlineCodeFile, ...] = ()

    @property
    def inline(self) -> bool:
        return bool(self.files)


def _bounded_int(raw: Any, *, name: str, default: int, ceiling: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise UserOperatorError("user_spec_invalid", f"userOperator.{name} 必须是正整数")
    if raw > ceiling:
        raise UserOperatorError(
            "user_spec_invalid", f"userOperator.{name} 超过平台上限 {ceiling}"
        )
    return raw


def _bounded_float(raw: Any, *, name: str, default: float, ceiling: float) -> float:
    if raw is None:
        return default
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise UserOperatorError("user_spec_invalid", f"userOperator.{name} 必须是数字")
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise UserOperatorError("user_spec_invalid", f"userOperator.{name} 必须是正数")
    return min(value, ceiling)


def _package_names(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_ALLOW_PACKAGES
    if not isinstance(raw, list) or not raw:
        raise UserOperatorError("user_spec_invalid", "userOperator.allowPackages 必须是非空数组")
    if len(raw) > USER_LIMIT_CEILINGS["allowPackages"]:
        raise UserOperatorError(
            "user_spec_invalid",
            f"依赖声明数量超过上限 {USER_LIMIT_CEILINGS['allowPackages']}",
        )
    names: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if not _PACKAGE_NAME_PATTERN.fullmatch(name):
            raise UserOperatorError("user_spec_invalid", f"依赖名无效：{item!r}")
        if name not in names:
            names.append(name)
    return tuple(names)


def _code_url_of(raw: Mapping[str, Any]) -> str:
    """取归档包地址：``codeUrl``（userOperator 段）与 ``url``（Go 侧 userCode/code 段）同义。"""
    return str(raw.get("codeUrl") or raw.get("url") or "")


def _code_sha_of(raw: Mapping[str, Any]) -> str:
    """取代码摘要：``codeSha256`` 与 ``sha256`` 同义。"""
    return str(raw.get("codeSha256") or raw.get("sha256") or "")


def inline_code_hash(files: Iterable[InlineCodeFile]) -> str:
    """内联源码的稳定摘要，与 Go 侧 ``customOperatorCodeHash`` 逐字节一致。

    口径：按路径升序，把 ``路径\\0单文件摘要\\0`` 依次喂进同一个 SHA-256。
    文件自带的摘要缺失时按内容重算，因此调用方传不传都能对上。
    """
    items = sorted(files, key=lambda item: item.path)
    digest = hashlib.sha256()
    for item in items:
        per_file = item.sha256 or hashlib.sha256(item.content.encode("utf-8")).hexdigest()
        digest.update(f"{item.path}\x00{per_file}\x00".encode())
    return digest.hexdigest()


def parse_inline_code_files(
    raw: Any,
    *,
    max_entries: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[InlineCodeFile, ...]:
    """校验内联源码：路径安全 + 条目数/单文件/总字节上限，与解包归档同一套约束。"""
    if not isinstance(raw, list) or not raw:
        raise UserOperatorError("user_spec_invalid", "userOperator.files 必须是非空数组")
    if len(raw) > max_entries:
        raise UserOperatorError(
            "code_too_large", f"用户源码文件数 {len(raw)} 超过上限 {max_entries}"
        )
    files: list[InlineCodeFile] = []
    total = 0
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise UserOperatorError("user_spec_invalid", "用户源码条目必须是 JSON 对象")
        name = str(item.get("path") or "")
        parts = _safe_member_parts(name)  # 绝对路径 / .. / 反斜杠 / 控制字符一律拒绝
        rel = "/".join(parts)
        if rel in seen:
            raise UserOperatorError("code_path_unsafe", f"用户源码路径重复：{name!r}")
        content = item.get("content")
        if not isinstance(content, str):
            raise UserOperatorError("user_spec_invalid", f"用户源码内容必须是字符串：{name!r}")
        size = len(content.encode("utf-8"))
        if size > max_file_bytes:
            raise UserOperatorError(
                "code_too_large", f"用户源码单文件超过 {max_file_bytes} 字节：{name}"
            )
        if total + size > max_total_bytes:
            raise UserOperatorError(
                "code_too_large", f"用户源码总字节超过 {max_total_bytes} 字节"
            )
        total += size
        seen.add(rel)
        declared = str(item.get("sha256") or "").strip().lower()
        if declared and not _is_valid_sha256(declared):
            raise UserOperatorError("code_hash_invalid", f"用户源码摘要格式无效：{name!r}")
        files.append(InlineCodeFile(path=rel, content=content, sha256=declared))
    return tuple(files)


def write_inline_files(
    files: Sequence[InlineCodeFile],
    dest_dir: Path | str,
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> list[str]:
    """把内联源码写入本次调用独占的代码目录，返回写入的相对路径。"""
    dest_root = Path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    dest_root = dest_root.resolve()
    written: list[str] = []
    total = 0
    for item in files:
        parts = _safe_member_parts(item.path)
        data = item.content.encode("utf-8")
        if len(data) > max_file_bytes:
            raise UserOperatorError(
                "code_too_large", f"用户源码单文件超过 {max_file_bytes} 字节：{item.path}"
            )
        if total + len(data) > max_total_bytes:
            raise UserOperatorError("code_too_large", f"用户源码总字节超过 {max_total_bytes} 字节")
        target = dest_root.joinpath(*parts).resolve()
        if not target.is_relative_to(dest_root):
            raise UserOperatorError("code_path_unsafe", f"用户源码越过目标目录：{item.path!r}")
        if target.is_symlink():
            raise UserOperatorError(
                "code_path_unsafe", f"用户源码落在符号链接上：{item.path!r}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        total += len(data)
        written.append("/".join(parts))
    return written


def parse_user_operator_spec(raw: Any) -> UserOperatorSpec:
    """解析并校验一次调用的用户代码规格；任何字段不合法都在执行前拒绝。

    兼容三个调用方的字段拼写（值同义，只是键名不同）：

    - 工作流节点（Go ``userNodeExecutor``）：``request.userCode`` 里的
      ``entrypoint``/``sha256``/``files``/``allowPackages``。
    - 工作台调试（Go ``invokeUserOperator``）：顶层 ``entrypoint``/``code.sha256``/``code.files``。
    - 归档包方式：``codeUrl``（或 ``url``）+ ``codeSha256``（或 ``sha256``）。

    带 ``files`` 即内联模式（不拉包），否则必须是可下载的归档包。
    """
    if not isinstance(raw, dict):
        raise UserOperatorError("user_spec_invalid", "userOperator 必须是 JSON 对象")
    entrypoint = str(raw.get("entrypoint") or "").strip()
    split_entrypoint(entrypoint)  # 早失败：格式非法直接拒绝请求
    timeout_seconds = _bounded_float(
        raw.get("timeoutSeconds"),
        name="timeoutSeconds",
        default=DEFAULT_TIMEOUT_SECONDS,
        ceiling=MAX_TIMEOUT_SECONDS,
    )
    max_entries = _bounded_int(
        raw.get("maxEntries"),
        name="maxEntries",
        default=DEFAULT_MAX_ENTRIES,
        ceiling=USER_LIMIT_CEILINGS["maxEntries"],
    )
    max_file_bytes = _bounded_int(
        raw.get("maxFileBytes"),
        name="maxFileBytes",
        default=DEFAULT_MAX_FILE_BYTES,
        ceiling=USER_LIMIT_CEILINGS["maxFileBytes"],
    )
    max_total_bytes = _bounded_int(
        raw.get("maxTotalBytes"),
        name="maxTotalBytes",
        default=DEFAULT_MAX_TOTAL_BYTES,
        ceiling=USER_LIMIT_CEILINGS["maxTotalBytes"],
    )
    allow_packages = _package_names(raw.get("allowPackages"))
    files = (
        parse_inline_code_files(
            raw["files"],
            max_entries=max_entries,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
        if raw.get("files") is not None
        else ()
    )
    sha256 = _code_sha_of(raw)
    if files:
        # 内联模式的摘要由源码本身推出：调用方声称的摘要对不上就是被篡改，直接拒。
        actual = inline_code_hash(files)
        if sha256:
            sha256 = validate_code_sha256(sha256)
            if sha256 != actual:
                raise UserOperatorError(
                    "code_hash_mismatch",
                    f"用户源码摘要不匹配：期望 {sha256}，实际 {actual}",
                    {"expected": sha256, "actual": actual},
                )
        else:
            sha256 = actual
        return UserOperatorSpec(
            code_url="",
            code_sha256=sha256,
            entrypoint=entrypoint,
            allow_packages=allow_packages,
            timeout_seconds=timeout_seconds,
            max_entries=max_entries,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
            max_archive_bytes=DEFAULT_MAX_ARCHIVE_BYTES,
            files=files,
        )
    if not _code_url_of(raw):
        # 既不内联也没有地址：这是规格缺失（400 永久错误），不能留到下载阶段报「取包失败」。
        raise UserOperatorError(
            "user_spec_invalid", "userOperator 必须给出归档地址（codeUrl/url）或内联源码（files）"
        )
    return UserOperatorSpec(
        code_url=validate_code_url(_code_url_of(raw)),
        code_sha256=validate_code_sha256(sha256),
        entrypoint=entrypoint,
        allow_packages=allow_packages,
        timeout_seconds=timeout_seconds,
        max_entries=max_entries,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_archive_bytes=DEFAULT_MAX_ARCHIVE_BYTES,
    )


def validate_code_url(url: str) -> str:
    """用户代码地址必须是 HTTPS；仅本机回环允许 http（本地联调与测试）。"""
    text = str(url or "")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise UserOperatorError("code_fetch_failed", f"用户代码地址无效：{exc}") from exc
    if not parts.netloc or parts.username or parts.password:
        raise UserOperatorError("code_fetch_failed", "用户代码地址不能携带用户凭据")
    if parts.scheme == "https":
        return text
    if parts.scheme == "http" and (parts.hostname or "") in {"localhost", "127.0.0.1", "::1"}:
        return text
    raise UserOperatorError("code_fetch_failed", f"用户代码地址必须使用 HTTPS：{text}")


def default_code_transport(url: str, timeout_seconds: float) -> bytes:
    """经预签名 GET 下载代码包（httpx 惰性导入，隔离子进程不会加载它）。"""
    import httpx

    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, read=max(1.0, timeout_seconds)), follow_redirects=False
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content
    except httpx.HTTPError as exc:
        raise UserOperatorError("code_fetch_failed", f"下载用户算子代码包失败：{exc}") from exc


def _safe_member_parts(name: str) -> tuple[str, ...]:
    """把归档条目名归一为 dest 下的安全相对路径分量。"""
    raw = str(name or "")
    if not raw:
        raise UserOperatorError("code_path_unsafe", "用户代码包条目路径为空")
    if "\x00" in raw or "\\" in raw or ":" in raw:
        raise UserOperatorError("code_path_unsafe", f"用户代码包条目路径非法：{raw!r}")
    if raw.startswith("/"):
        raise UserOperatorError("code_path_unsafe", f"用户代码包条目是绝对路径：{raw!r}")
    while raw.startswith("./"):
        raw = raw[2:]
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts:
        raise UserOperatorError("code_path_unsafe", f"用户代码包条目路径无效：{raw!r}")
    for part in parts:
        if part == "..":
            raise UserOperatorError("code_path_unsafe", f"用户代码包条目越出目标目录：{raw!r}")
        if part.endswith((".", " ")):
            raise UserOperatorError("code_path_unsafe", f"用户代码包条目名非规范：{raw!r}")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in part):
            raise UserOperatorError("code_path_unsafe", f"用户代码包条目含控制字符：{raw!r}")
    return tuple(parts)


def _extract_entry(
    open_source: Callable[[], Any],
    dest_root: Path,
    parts: tuple[str, ...],
    *,
    name: str,
    max_file_bytes: int,
    max_total_bytes: int,
    total_written: int,
) -> int:
    """流式写出一个条目：真实字节同样受限（声明大小可被伪造，zip bomb 防护）。"""
    target = dest_root.joinpath(*parts)
    resolved = target.resolve()
    if not resolved.is_relative_to(dest_root.resolve()):
        raise UserOperatorError("code_path_unsafe", f"用户代码包条目越过目标目录：{name!r}")
    if resolved.is_symlink():
        raise UserOperatorError("code_path_unsafe", f"用户代码包条目落在符号链接上：{name!r}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open_source() as source, resolved.open("wb") as sink:
        while True:
            chunk = source.read(1 << 16)
            if not chunk:
                break
            written += len(chunk)
            if written > max_file_bytes:
                raise UserOperatorError(
                    "code_too_large", f"用户代码包单文件超过 {max_file_bytes} 字节：{name}"
                )
            if total_written + written > max_total_bytes:
                raise UserOperatorError(
                    "code_too_large", f"用户代码包解压总量超过 {max_total_bytes} 字节"
                )
            sink.write(chunk)
    return written


def _unpack_zip(
    raw: bytes,
    dest_root: Path,
    *,
    max_entries: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> list[str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise UserOperatorError("code_archive_invalid", f"用户代码包不是合法 zip：{exc}") from exc
    names: list[str] = []
    total = 0
    with archive:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise UserOperatorError(
                "code_too_large", f"用户代码包条目数 {len(infos)} 超过上限 {max_entries}"
            )
        for info in infos:
            parts = _safe_member_parts(info.filename)
            if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                raise UserOperatorError(
                    "code_path_unsafe", f"用户代码包不得包含符号链接：{info.filename}"
                )
            target = dest_root.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if info.file_size > max_file_bytes:
                raise UserOperatorError(
                    "code_too_large",
                    f"用户代码包单文件声明超过 {max_file_bytes} 字节：{info.filename}",
                )
            if total + info.file_size > max_total_bytes:
                raise UserOperatorError(
                    "code_too_large", f"用户代码包解压总量声明超过 {max_total_bytes} 字节"
                )
            total += _extract_entry(
                lambda info=info: archive.open(info),
                dest_root,
                parts,
                name=info.filename,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
                total_written=total,
            )
            names.append("/".join(parts))
    return names


def _unpack_tar(
    raw: bytes,
    dest_root: Path,
    *,
    max_entries: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> list[str]:
    names: list[str] = []
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > max_entries:
                raise UserOperatorError(
                    "code_too_large", f"用户代码包条目数 {len(members)} 超过上限 {max_entries}"
                )
            for member in members:
                parts = _safe_member_parts(member.name)
                if member.issym() or member.islnk():
                    raise UserOperatorError(
                        "code_path_unsafe", f"用户代码包不得包含链接条目：{member.name}"
                    )
                if member.isfifo() or member.isdev():
                    raise UserOperatorError(
                        "code_path_unsafe", f"用户代码包不得包含设备/FIFO 条目：{member.name}"
                    )
                target = dest_root.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise UserOperatorError(
                        "code_archive_invalid", f"不支持的归档条目类型：{member.name}"
                    )
                if member.size > max_file_bytes:
                    raise UserOperatorError(
                        "code_too_large",
                        f"用户代码包单文件声明超过 {max_file_bytes} 字节：{member.name}",
                    )
                if total + member.size > max_total_bytes:
                    raise UserOperatorError(
                        "code_too_large", f"用户代码包解压总量声明超过 {max_total_bytes} 字节"
                    )
                total += _extract_entry(
                    lambda member=member: archive.extractfile(member),
                    dest_root,
                    parts,
                    name=member.name,
                    max_file_bytes=max_file_bytes,
                    max_total_bytes=max_total_bytes,
                    total_written=total,
                )
                names.append("/".join(parts))
    except (tarfile.TarError, OSError) as exc:
        raise UserOperatorError("code_archive_invalid", f"用户代码包不是合法 tar.gz：{exc}") from exc
    return names


def unpack_archive(
    raw: bytes,
    dest_dir: Path | str,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> list[str]:
    """安全解包 zip 或 tar.gz，返回写出的相对路径列表。"""
    dest_root = Path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    dest_root = dest_root.resolve()
    if raw[:2] == b"PK":
        return _unpack_zip(
            raw,
            dest_root,
            max_entries=max_entries,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
    if raw[:2] == b"\x1f\x8b":
        return _unpack_tar(
            raw,
            dest_root,
            max_entries=max_entries,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
    raise UserOperatorError("code_archive_invalid", "用户代码包必须是 zip 或 tar.gz")


def fetch_user_code(
    code_url: str,
    expected_sha256: str,
    dest_dir: Path | str,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    download_timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    transport: Callable[[str, float], bytes] | None = None,
) -> list[str]:
    """拉取并校验、安全解包用户代码包；返回解出的相对路径列表。

    ``transport`` 仅供测试与本地联调注入（生产走预签名 HTTPS GET）。
    """
    url = validate_code_url(code_url)
    sha256 = validate_code_sha256(expected_sha256)
    fetch = transport or default_code_transport
    try:
        raw = fetch(url, download_timeout_seconds)
    except UserOperatorError:
        raise
    # 传输层异常统一归类为拉取失败（重抛即已记录语义，无需再吞细节）
    except Exception as exc:
        raise UserOperatorError("code_fetch_failed", f"下载用户算子代码包失败：{exc}") from exc
    if len(raw) > max_archive_bytes:
        raise UserOperatorError(
            "code_too_large", f"用户代码包超过 {max_archive_bytes} 字节压缩上限"
        )
    actual = hashlib.sha256(raw).hexdigest()
    if actual != sha256:
        raise UserOperatorError(
            "code_hash_mismatch",
            f"用户代码包摘要不匹配：期望 {sha256}，实际 {actual}",
            {"expected": sha256, "actual": actual},
        )
    return unpack_archive(
        raw,
        dest_dir,
        max_entries=max_entries,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )


def fetch_from_spec(
    spec: UserOperatorSpec,
    dest_dir: Path | str,
    *,
    transport: Callable[[str, float], bytes] | None = None,
) -> list[str]:
    """按 :class:`UserOperatorSpec` 把用户源码落盘（serve 侧唯一的装载入口）。

    内联规格直接写入独占代码目录，不产生任何网络请求；归档规格才走预签名 GET。
    """
    if spec.files:
        return write_inline_files(
            spec.files,
            dest_dir,
            max_file_bytes=spec.max_file_bytes,
            max_total_bytes=spec.max_total_bytes,
        )
    return fetch_user_code(
        spec.code_url,
        spec.code_sha256,
        dest_dir,
        max_entries=spec.max_entries,
        max_file_bytes=spec.max_file_bytes,
        max_total_bytes=spec.max_total_bytes,
        max_archive_bytes=spec.max_archive_bytes,
        download_timeout_seconds=spec.download_timeout_seconds,
        transport=transport,
    )


# --- 依赖白名单守卫 -------------------------------------------------------


def _canonical_package(name: str) -> str:
    """PEP 503 规范化发行名：大小写与 ``-``/``_``/``.`` 的差异不参与比较。"""
    text = str(name or "").strip().lower()
    return text.replace("_", "-").replace(".", "-")


def _normalize_package(name: str) -> str:
    """把声明名映射为顶层导入名（``scikit-learn`` → ``sklearn``），其余原样返回。"""
    text = str(name or "").strip()
    if not text:
        return ""
    return PACKAGE_IMPORT_ALIASES.get(_canonical_package(text)) or text


def _top_level(name: str) -> str:
    return str(name or "").partition(".")[0]


def _requirement_name(raw: str) -> str | None:
    """从 PEP 508 需求串取出发行名（规范化后）；带 extra 的可选依赖跳过。"""
    text = str(raw or "").strip()
    if not text:
        return None
    marker = ""
    if ";" in text:
        text, marker = text.split(";", 1)
    if "extra" in marker:
        return None
    name = ""
    for ch in text.strip():
        if ch.isalnum() or ch in "._-":
            name += ch
        else:
            break
    return _canonical_package(name) or None


def _build_package_index() -> tuple[dict[str, list[str]], dict[str, set[str]]] | None:
    """构造 ``模块名 -> 发行名列表`` 与 ``发行名 -> 顶层模块集合`` 双向索引。

    元数据不可用（冻结环境、损坏的 dist-info）时返回 ``None``，调用方退回显式声明集合。
    """
    try:
        packages = metadata.packages_distributions()
    except Exception:  # noqa: BLE001 - 元数据层任何异常都不该影响执行路径
        return None
    module_to_dists: dict[str, list[str]] = {}
    dist_to_modules: dict[str, set[str]] = {}
    for module_name, dist_names in packages.items():
        module_key = str(module_name).strip().lower()
        if not module_key:
            continue
        for raw_dist in dist_names:
            dist_key = _canonical_package(raw_dist)
            if not dist_key:
                continue
            module_to_dists.setdefault(module_key, []).append(dist_key)
            dist_to_modules.setdefault(dist_key, set()).add(module_key)
    return module_to_dists, dist_to_modules


def expand_allow_packages(allow_packages: Iterable[str]) -> set[str]:
    """把声明的依赖扩展为其安装期直接依赖对应的导入名（best-effort）。

    sklearn 需要 joblib/threadpoolctl、pandas 需要 python-dateutil 等：只按用户声明
    的名字放行会让预置依赖的二次 import 被误拦。可选依赖（extras）不展开，且只向下
    走 ``MAX_DEPENDENCY_DEPTH`` 层——展开的目的是免误伤，不是放弃白名单。
    """
    roots: set[str] = set()
    seeds: list[str] = []
    for item in allow_packages:
        name = _normalize_package(item)
        if not name:
            continue
        roots.add(name)
        seeds.append(_canonical_package(item))
    index = _build_package_index()
    if index is None:
        return roots
    module_to_dists, dist_to_modules = index

    pending: deque[tuple[str, int]] = deque()
    seen: set[str] = set()
    for seed in seeds:
        # 声明可能是导入名（sklearn / PIL）也可能是发行名（scikit-learn）：
        # 前者查 module_to_dists 反查发行，后者直接用自身作为展开根。
        dists = module_to_dists.get(seed) or ([seed] if seed in dist_to_modules else [])
        for dist in dists:
            if dist not in seen:
                seen.add(dist)
                pending.append((dist, 0))

    while pending:
        dist_name, depth = pending.popleft()
        if depth >= MAX_DEPENDENCY_DEPTH:
            continue
        try:
            requires = tuple(metadata.distribution(dist_name).requires or ())
        except Exception:  # noqa: BLE001, S112 - 未安装/元数据损坏的依赖直接跳过
            continue
        for raw in requires:
            target = _requirement_name(raw)
            if not target:
                continue
            for module_name in dist_to_modules.get(target) or (target,):
                normalized = _normalize_package(module_name)
                if normalized:
                    roots.add(normalized)
            if target not in seen:
                seen.add(target)
                pending.append((target, depth + 1))
    return roots


def make_import_guard(
    allow_packages: Iterable[str],
    *,
    extra_allowed: Iterable[str] = (),
    blocked: Iterable[str] = BLOCKED_STDLIB_ROOTS,
    available_hint: Sequence[str] | None = None,
) -> Callable[[str, tuple[Any, ...]], None]:
    """返回可注册进 ``sys.addaudithook`` 的 import 白名单守卫。

    规则（按优先级）：显式禁止集 → 标准库始终放行 → 白名单（含 SDK 与用户自身包）放行
    → 其余第三方包一律以 ImportError 中止。钩子内不做任何 import，避免递归触发审计事件。
    """
    declared = sorted({name for name in (_normalize_package(item) for item in allow_packages) if name})
    allowed = set(expand_allow_packages(declared))
    allowed |= {_normalize_package(item) for item in extra_allowed if _normalize_package(item)}
    allowed |= PLATFORM_IMPORT_ROOTS
    # 大小写不敏感比较：元数据里的顶层模块名可能是小写（pil），而 import 写作 PIL
    allowed_roots = {name.lower() for name in allowed if name}
    blocked_roots = {_top_level(item).lower() for item in blocked if item}
    stdlib = frozenset(getattr(sys, "stdlib_module_names", ()))
    hint = ", ".join(available_hint or declared) or "（无）"
    cache: dict[str, bool] = {}

    def guard(event: str, args: tuple[Any, ...]) -> None:
        if event != "import" or not args:
            return
        name = args[0]
        if not isinstance(name, str) or not name:
            return
        root = _top_level(name)
        key = root.lower()
        if key in blocked_roots:
            raise PlatformImportBlockedError(f"平台不允许用户算子导入 {root}")
        verdict = cache.get(key)
        if verdict is None:
            verdict = key in allowed_roots or root in stdlib or root.startswith("_")
            cache[key] = verdict
        if not verdict:
            raise DependencyBlockedError(
                f"依赖 {root} 不在平台预置白名单内，可用依赖：{hint}",
            )

    return guard


# --- 入口与影子模块检查 ---------------------------------------------------


def split_entrypoint(entrypoint: str) -> tuple[str, str]:
    """解析 ``module:Class``（支持 ``module:Outer.Inner``）。"""
    text = str(entrypoint or "").strip()
    if ":" not in text:
        raise UserOperatorError(
            "user_entrypoint_invalid", f"算子入口必须是 module:Class 形式：{entrypoint!r}"
        )
    module_name, _, attribute = text.partition(":")
    module_name, attribute = module_name.strip(), attribute.strip()
    if not module_name or not all(part.isidentifier() for part in module_name.split(".")):
        raise UserOperatorError("user_entrypoint_invalid", f"算子入口模块名无效：{entrypoint!r}")
    if not attribute or not all(part.isidentifier() for part in attribute.split(".")):
        raise UserOperatorError("user_entrypoint_invalid", f"算子入口类名无效：{entrypoint!r}")
    return module_name, attribute


def find_shadowed_modules(code_dir: Path | str) -> list[str]:
    """用户包不得覆盖平台模块或标准库顶层模块。"""
    root = Path(code_dir)
    stdlib = frozenset(getattr(sys, "stdlib_module_names", ()))
    shadowed: list[str] = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        top = name[:-3] if entry.is_file() and name.endswith(".py") else name
        if not top or not top.isidentifier():
            continue
        if top in PLATFORM_IMPORT_ROOTS or top in stdlib:
            shadowed.append(top)
    return sorted(set(shadowed))


def top_level_import_names(code_dir: Path | str) -> set[str]:
    """用户代码目录里可直接 import 的顶层模块/包名（守卫需放行）。"""
    names: set[str] = set()
    for entry in Path(code_dir).iterdir():
        if entry.is_dir():
            if (entry / "__init__.py").is_file() and entry.name.isidentifier():
                names.add(entry.name)
        elif entry.suffix == ".py" and entry.stem.isidentifier():
            names.add(entry.stem)
    return names


# --- 子进程执行 -----------------------------------------------------------


def encode_child_line(prefix: str, payload: Any) -> bytes:
    """把一行协议消息编码为字节（JSON 单行，无换行）。"""
    return (prefix + encode_runtime_json(payload).decode("utf-8") + "\n").encode("utf-8")


class _TailBuffer:
    """只保留尾部若干字节的日志缓冲（用户代码可能刷屏）。"""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.buffer = bytearray()
        self.dropped = 0

    def write(self, data: bytes) -> None:
        if not data:
            return
        self.buffer += data
        if len(self.buffer) > self.limit:
            overflow = len(self.buffer) - self.limit
            del self.buffer[:overflow]
            self.dropped += overflow

    def text(self) -> str:
        return self.buffer.decode("utf-8", errors="replace")


def _emit_user_log(
    on_log: Callable[[str, str], None] | None, stream: str, text: str
) -> None:
    """把一段用户输出增量转给 on_log；回调自身出错绝不能影响算子执行。"""
    if on_log is None or not text:
        return
    try:
        on_log(stream, text)
    except Exception:  # noqa: BLE001, S110 - 日志回传是旁路能力，失败即放弃该增量
        pass


#: 杀掉进程树后等待其真正退出的秒数（超时即认为孤儿，交由 OS 回收）
_KILL_WAIT_SECONDS = 5.0


def _cancel_requested(cancel_check: Callable[[], bool] | None) -> bool:
    """取消探测：状态查询本身出错时按“未取消”处理，绝不因此中断计算。"""
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:  # noqa: BLE001 - 取消检查失败不应放大成算子失败
        return False


def _terminate_process_tree(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """杀掉整棵进程树：用户代码自己派生的孙子进程不能留在实例里。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            # 固定可执行文件名与参数列表，不拼接命令串、不用 shell
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=grace,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _feed_stdin(proc: subprocess.Popen, data: bytes) -> None:
    try:
        if proc.stdin is not None:
            proc.stdin.write(data)
            proc.stdin.close()
    except (OSError, ValueError):
        # 子进程提前退出导致管道断裂：真实原因由 stdout/stderr/exitCode 呈现。
        pass


def _drain_stderr(
    proc: subprocess.Popen,
    tail: _TailBuffer,
    on_log: Callable[[str, str], None] | None = None,
) -> None:
    stream = proc.stderr
    if stream is None:
        return
    # 流式回传按块读，边界可能切在多字节字符中间：增量解码器兜住半个字符。
    decoder = codecs.getincrementaldecoder("utf-8")("replace") if on_log is not None else None
    try:
        while True:
            chunk = stream.read(1 << 13)
            if not chunk:
                break
            tail.write(chunk)
            if decoder is not None:
                _emit_user_log(on_log, "stderr", decoder.decode(chunk))
    except OSError:
        pass
    finally:
        if decoder is not None:
            _emit_user_log(on_log, "stderr", decoder.decode(b"", True))
        try:
            stream.close()
        except OSError:
            pass


def _drain_stdout(proc: subprocess.Popen, sink: queue.Queue[Any]) -> None:
    stream = proc.stdout
    if stream is None:
        return
    try:
        while True:
            line = stream.readline()
            if not line:
                break
            sink.put(line)
    except OSError:
        pass
    finally:
        sink.put(None)
        try:
            stream.close()
        except OSError:
            pass


def run_user_operator_isolated(
    *,
    request: dict[str, Any],
    code_dir: Path | str,
    workspace_root: Path | str,
    sandbox_dir: Path | str,
    entrypoint: str,
    allow_packages: Iterable[str] = DEFAULT_ALLOW_PACKAGES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    on_event: Callable[[str, dict], None] | None = None,
    on_log: Callable[[str, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    python_executable: str | None = None,
    base_env: Mapping[str, str] | None = None,
    max_log_bytes: int = DEFAULT_LOG_TAIL_BYTES,
) -> dict[str, Any]:
    """在独立子进程里执行一次用户算子，返回统一结果字典。

    永不抛异常表达执行失败（超时/崩溃/取消都以 status/error 返回）；
    调用方负责物化输入与导出输出——子进程只做计算，不持有传输凭据。
    ``on_log(stream, text)`` 按到达顺序回传用户输出增量（stream 为 stdout/stderr），
    工作台调试据此做实时日志；回调异常不会打断执行。
    """
    started = time.monotonic()
    logs_stdout = _TailBuffer(max_log_bytes)
    logs_stderr = _TailBuffer(max_log_bytes)
    payload = {
        "protocolVersion": 1,
        "request": request,
        "workspaceRoot": str(Path(workspace_root).resolve()),
        "userOperator": {
            "codeDir": str(Path(code_dir).resolve()),
            "entrypoint": str(entrypoint),
            "allowPackages": [str(item) for item in allow_packages],
        },
    }
    try:
        raw_payload = encode_runtime_json(payload)
    except (TypeError, ValueError) as exc:
        return {
            "status": "failed",
            "result": None,
            "writtenFiles": [],
            "error": {"code": "user_request_invalid", "message": str(exc), "details": {}},
            "logs": {"stdout": "", "stderr": ""},
            "exitCode": None,
            "timedOut": False,
            "cancelled": False,
            "durationMs": 0,
        }
    if len(raw_payload) > MAX_CHILD_STDIN_BYTES:
        return {
            "status": "failed",
            "result": None,
            "writtenFiles": [],
            "error": {
                "code": "code_too_large",
                "message": f"算子请求超过子进程 stdin 上限 {MAX_CHILD_STDIN_BYTES} 字节",
                "details": {},
            },
            "logs": {"stdout": "", "stderr": ""},
            "exitCode": None,
            "timedOut": False,
            "cancelled": False,
            "durationMs": 0,
        }

    try:
        timeout = max(1.0, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS

    env = build_scrubbed_env(
        code_dir=code_dir,
        sandbox_dir=sandbox_dir,
        base_env=base_env,
        sdk_parent=_sdk_parent(),
    )
    argv = [python_executable or sys.executable, "-m", "radstudio.user_execute"]
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": str(Path(sandbox_dir).resolve()),
        "env": env,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    lines: queue.Queue[Any] = queue.Queue()
    envelope: dict[str, Any] | None = None
    timed_out = False
    cancelled = False
    exit_code: int | None = None
    proc: subprocess.Popen | None = None
    try:
        # 固定参数列表 + 无 shell：argv 里没有一处来自用户代码或请求文本的拼接
        proc = subprocess.Popen(argv, **popen_kwargs)
    except OSError as exc:
        return {
            "status": "failed",
            "result": None,
            "writtenFiles": [],
            "error": {"code": "user_spawn_failed", "message": str(exc), "details": {}},
            "logs": {"stdout": "", "stderr": ""},
            "exitCode": None,
            "timedOut": False,
            "cancelled": False,
            "durationMs": 0,
        }

    feeder = threading.Thread(
        target=_feed_stdin, args=(proc, raw_payload), name="user-stdin", daemon=True
    )
    feeder.start()
    stderr_reader = threading.Thread(
        target=_drain_stderr,
        args=(proc, logs_stderr, on_log),
        name="user-stderr",
        daemon=True,
    )
    stderr_reader.start()
    stdout_reader = threading.Thread(
        target=_drain_stdout, args=(proc, lines), name="user-stdout", daemon=True
    )
    stdout_reader.start()

    deadline = started + timeout
    got_result = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = lines.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if _cancel_requested(cancel_check):
                    cancelled = True
                    break
                continue
            if line is None:
                break
            # 子进程行是 bytes：直接入缓冲（早期当 str 处理，print 一次即整次调用失败）
            if len(line) > MAX_CHILD_STDOUT_BYTES:
                logs_stdout.write(STDOUT_OVERFLOW_NOTE.encode("utf-8"))
                _emit_user_log(on_log, "stdout", STDOUT_OVERFLOW_NOTE)
                continue
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.startswith(EVENT_PREFIX):
                # 协议行（进度事件/结果）只服务于父进程，不进用户日志
                try:
                    event = decode_runtime_json(
                        text[len(EVENT_PREFIX) :].encode("utf-8"), max_bytes=MAX_CHILD_STDOUT_BYTES
                    )
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if isinstance(event, dict) and on_event is not None:
                    event_name = str(event.get("event") or "")
                    data = event.get("data")
                    if event_name:
                        try:
                            on_event(event_name, data if isinstance(data, dict) else {})
                        except Exception:  # noqa: BLE001, S110 - 进度回调不得影响执行
                            pass
            elif text.startswith(RESULT_PREFIX):
                try:
                    envelope = decode_runtime_json(
                        text[len(RESULT_PREFIX) :].encode("utf-8"),
                        max_bytes=MAX_CHILD_STDOUT_BYTES,
                    )
                except (ValueError, UnicodeError, RecursionError) as exc:
                    envelope = {"protocolError": f"结果行无法解析：{exc}"}
                    # 解析不了就保留原文（截断），否则调用方无从知道子进程到底写了什么
                    logs_stdout.write(line[:MAX_CHILD_STDOUT_BYTES])
                got_result = True
                break
            else:
                logs_stdout.write(line)
                _emit_user_log(on_log, "stdout", line.decode("utf-8", errors="replace"))
            if _cancel_requested(cancel_check):
                cancelled = True
                break
        if timed_out or cancelled:
            _terminate_process_tree(proc)
            try:
                exit_code = proc.wait(timeout=_KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                exit_code = None
        else:
            try:
                exit_code = proc.wait(timeout=max(0.5, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                # 结果已到手却收不了尾（用户代码起了非守护线程）：按超时处置，
                # 但保留结果，让调用方拿到可用产物而不是整次失败。
                _terminate_process_tree(proc)
                try:
                    exit_code = proc.wait(timeout=_KILL_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    exit_code = None
                if not got_result:
                    timed_out = True
    finally:
        feeder.join(timeout=2)
        stderr_reader.join(timeout=2)
        stdout_reader.join(timeout=2)

    logs = {"stdout": logs_stdout.text(), "stderr": logs_stderr.text()}
    child_usage = envelope.get("usage") if isinstance(envelope, dict) else None
    base = {
        "logs": logs,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "cancelled": cancelled,
        "durationMs": int((time.monotonic() - started) * 1000),
        # 子进程自报的峰值内存；超时/协议错误拿不到信封时如实为空，不由父进程猜。
        "usage": child_usage if isinstance(child_usage, dict) else {},
    }
    if timed_out:
        return {
            **base,
            "status": "failed",
            "result": None,
            "writtenFiles": [],
            "error": {
                "code": "user_timeout",
                "message": f"用户算子超过 {int(timeout)} 秒未结束，已终止隔离进程",
                "details": {"timeoutSeconds": int(timeout)},
            },
        }
    if cancelled:
        return {
            **base,
            "status": "cancelled",
            "result": None,
            "writtenFiles": [],
            "error": {
                "code": "operator.cancelled",
                "message": "用户算子被取消，隔离进程已终止",
                "details": {},
            },
        }
    if not isinstance(envelope, dict):
        return {
            **base,
            "status": "failed",
            "result": None,
            "writtenFiles": [],
            "error": {
                "code": "user_protocol_error",
                "message": (
                    envelope.get("protocolError")
                    if isinstance(envelope, dict)
                    else f"用户算子子进程退出码 {exit_code}，未返回结果"
                )
                or "用户算子子进程未返回结果",
                "details": {},
            },
        }
    operator_result = envelope.get("operatorResult")
    written = envelope.get("writtenFiles")
    status = str(envelope.get("status") or "failed")
    if status not in {"completed", "failed", "cancelled"}:
        status = "failed"
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else None
    if not isinstance(operator_result, dict):
        return {
            **base,
            "status": "failed",
            "result": None,
            "writtenFiles": [],
            "error": error or {"code": "user_protocol_error", "message": "缺少算子结果", "details": {}},
        }
    return {
        **base,
        "status": status,
        "result": operator_result,
        "writtenFiles": [str(item) for item in written] if isinstance(written, list) else [],
        "error": error,
    }


__all__ = [
    "BLOCKED_STDLIB_ROOTS",
    "DEFAULT_ALLOW_PACKAGES",
    "DEFAULT_LOG_TAIL_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "EVENT_PREFIX",
    "MAX_TIMEOUT_SECONDS",
    "RESULT_PREFIX",
    "USER_LIMIT_CEILINGS",
    "DependencyBlockedError",
    "InlineCodeFile",
    "PlatformImportBlockedError",
    "UserOperatorError",
    "UserOperatorSpec",
    "build_scrubbed_env",
    "encode_child_line",
    "expand_allow_packages",
    "fetch_from_spec",
    "fetch_user_code",
    "find_shadowed_modules",
    "inline_code_hash",
    "make_import_guard",
    "parse_inline_code_files",
    "parse_user_operator_spec",
    "run_user_operator_isolated",
    "split_entrypoint",
    "top_level_import_names",
    "unpack_archive",
    "validate_code_sha256",
    "validate_code_url",
    "write_inline_files",
]
