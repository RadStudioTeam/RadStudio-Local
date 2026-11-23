"""把一次真实 Python 异常整理成可交给工作台渲染的现场描述。

用户代码报错时，``traceback.format_exc()`` 的纯文本对本地开发者够用，但在工作台里不够：
读者想知道「哪一行、当时 ``a`` 和 ``b`` 是多少」，还要能一步跳回编辑器对应文件。本模块只依赖
标准库（子进程里第三方依赖受白名单守卫约束），产出的结构是::

    {
      "type": "ZeroDivisionError",
      "message": "division by zero",
      "relation": "raised",
      "frames": [{"path": "probe.py", "line": 21, "function": "run", "code": "return a / b",
                  "user": true, "locals": [{"name": "a", "value": "3"}]}],
      "truncated": false,
      "cause": {"type": "ValueError", "relation": "context", "frames": [...]}   # 有上游才有
    }

顶层就是用户真正看到的那一次异常，上游异常按 ``__cause__`` / ``__context__`` 逐层嵌在
``cause`` 里（最多 ``MAX_LINKS`` 层，被截断时挂 ``chainTruncated``）：嵌套而不是平铺数组，
因为每个环节只会被读一次，平铺会让同一份帧在 ``frames`` 与 ``links`` 里各存一遍，白占日志预算。

三条口径：

1. **路径回映射**。子进程把用户源码解到临时沙箱，栈里的文件名是绝对路径；落在 ``source_root``
   下的帧一律换成相对路径（与编辑器文件树同名），前端才能按 ``path:line`` 定位。其余帧只留
   文件名，不把服务器目录结构透露给用户。
2. **局部变量只给用户自己的帧**。平台帧的 ``f_locals`` 可能含请求体与凭据派生值，对排障也没有
   价值；用户帧同样跳过私有名与凭据样名，单个值限长、每帧限量。
3. **任何情况都要能出结果**。取局部变量本身可能触发用户定义的 ``__repr__`` 并再次抛异常，所以
   每个值单独兜底；整体超预算时从尾部丢帧、再剥局部变量，而不是丢整份现场。

帧序与交互式调试器一致：**最靠近出错点的帧在前**（``frames[0]`` 就是报错那一行），与纯文本
traceback 的自上而下相反；工作台按这个顺序渲染并标注。
"""

from __future__ import annotations

import json
import linecache
import os
from pathlib import Path
from typing import Any

#: 一条链里最多保留几个异常环节（``raise ... from ...`` 常见两层，再多也说明不了问题）
MAX_LINKS = 4
#: 单个环节最多保留多少帧（上百帧多半是框架递归，尾部几帧才是现场）
MAX_FRAMES_PER_LINK = 20
#: 每帧最多几个局部变量、单个值/源码行与单条消息多长
MAX_LOCALS_PER_FRAME = 12
MAX_VALUE_CHARS = 160
MAX_SOURCE_CHARS = 200
MAX_MESSAGE_CHARS = 600
#: 整体序列化字节上限：超限后按 _clip 的顺序瘦身，上层再按日志预算裁剪
MAX_PAYLOAD_BYTES = 16 << 10

#: 名字命中这些片段的局部变量不回显——用户自己写在代码里的 token 也不该进运行记录
_DENIED_NAME_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "apikey",
    "api_key",
    "access_key",
    "accesskey",
    "private_key",
    "authorization",
    "cookie",
    "session_key",
)

#: 可直接读成一行文本的内置类型；其余对象只给截断后的 repr 或形态摘要
_SIMPLE_VALUE_TYPES = (bool, int, float, complex, str, bytes, bytearray, type(None))


def _is_denied_name(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith("_"):
        return True
    return any(hint in lowered for hint in _DENIED_NAME_HINTS)


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _describe_value(value: Any) -> str:
    """尽力把局部变量写成一行可读文本；``__repr__`` 自己抛异常也不能拖垮整份现场。"""
    if isinstance(value, _SIMPLE_VALUE_TYPES):
        if isinstance(value, (bytes, bytearray)):
            return f"{type(value).__name__}(len={len(value)})"
        if isinstance(value, str):
            return _shorten(repr(value), MAX_VALUE_CHARS)
        return _shorten(str(value), MAX_VALUE_CHARS)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _shorten(f"{type(value).__name__}(len={len(value)})", MAX_VALUE_CHARS)
    if isinstance(value, dict):
        keys = ", ".join(_shorten(str(key), 32) for key in list(value.keys())[:5])
        suffix = ", …" if len(value) > 5 else ""
        return _shorten(f"dict(len={len(value)}; keys=[{keys}{suffix}])", MAX_VALUE_CHARS)
    if callable(value):
        name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None) or ""
        return _shorten(f"<callable {name}>" if name else "<callable>", MAX_VALUE_CHARS)
    try:
        return _shorten(repr(value), MAX_VALUE_CHARS)
    except BaseException:  # noqa: BLE001 - 用户对象的 __repr__ 可以为所欲为
        return f"<{type(value).__name__} repr failed>"


def _frame_location(filename: str, source_root: Path | None) -> tuple[str, bool]:
    """返回 (展示用路径, 是否用户源码帧)。"""
    if not filename:
        return "", False
    normalized = os.path.normpath(filename)
    if source_root is not None:
        try:
            relative = os.path.relpath(normalized, str(source_root))
        except ValueError:  # 跨盘符（Windows）等，必然不是用户源码
            relative = ""
        if relative and not relative.startswith(".."):
            return relative.replace(os.sep, "/"), True
    return os.path.basename(normalized), False


def _source_line(filename: str, lineno: int) -> str:
    if not filename or lineno <= 0:
        return ""
    return linecache.getline(filename, lineno).strip()


def _frame_payload(
    frame: Any, raw_filename: str, path: str, lineno: int, function: str, is_user: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": path,
        "line": lineno,
        "function": function,
        # 源码行按沙箱里的真实文件名读；path 只用来在编辑器里定位到同名文件
        "code": _shorten(_source_line(raw_filename, lineno), MAX_SOURCE_CHARS),
        "user": is_user,
    }
    if not is_user:
        return payload
    locals_: list[dict[str, str]] = []
    for key, value in list(getattr(frame, "f_locals", {}).items()):
        if len(locals_) >= MAX_LOCALS_PER_FRAME:
            payload["localsTruncated"] = True
            break
        if _is_denied_name(str(key)):
            continue
        locals_.append({"name": str(key), "value": _describe_value(value)})
    if locals_:
        payload["locals"] = locals_
    return payload


def _link_frames(trace: Any, source_root: Path | None) -> list[dict[str, Any]]:
    """按交互式调试器的顺序展开栈：**最靠近出错点的帧在前**（``frames[0]`` 就是报错那一行）。"""
    collected: list[tuple[Any, str, int, str]] = []
    current = trace
    while current is not None:
        frame = current.tb_frame
        code = getattr(frame, "f_code", None)
        filename = str(getattr(code, "co_filename", "") or "")
        function = str(getattr(code, "co_name", "") or "<anonymous>")
        collected.append((frame, filename, int(current.tb_lineno or 0), function))
        current = current.tb_next
    frames: list[dict[str, Any]] = []
    for frame, filename, lineno, function in reversed(collected):
        path, is_user = _frame_location(filename, source_root)
        frames.append(_frame_payload(frame, filename, path, lineno, function, is_user))
    return frames


def _same_place(frame: dict[str, Any], other: dict[str, Any]) -> bool:
    return frame.get("path") == other.get("path") and frame.get("line") == other.get("line")


def _syntax_frame(exc: BaseException, source_root: Path | None) -> dict[str, Any] | None:
    """语法错误没有可执行帧：出错位置在异常对象自己身上，补一帧给用户点。

    用户的代码在 ``import`` 期就编译不过，栈里只有 importlib 的平台帧，真正要看的
    ``文件:行`` 只存在于 ``SyntaxError.filename/lineno``——不补这一帧，编辑器里跳不过去。
    """
    filename = str(getattr(exc, "filename", "") or "")
    lineno = int(getattr(exc, "lineno", 0) or 0)
    if not filename or lineno <= 0:
        return None
    path, is_user = _frame_location(filename, source_root)
    code = _source_line(filename, lineno) or str(getattr(exc, "text", "") or "").strip()
    return {
        "path": path,
        "line": lineno,
        "function": "",
        "code": _shorten(code, MAX_SOURCE_CHARS),
        "user": is_user,
    }


def _exception_link(exc: BaseException, source_root: Path | None, relation: str) -> dict[str, Any]:
    frames = _link_frames(exc.__traceback__, source_root)
    if isinstance(exc, SyntaxError):
        head = _syntax_frame(exc, source_root)
        # 栈顶已经是同一处位置（错误出在已编译的调用方）就不重复插一帧
        if head is not None and not (frames and _same_place(frames[0], head)):
            frames.insert(0, head)
    payload: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": _shorten(str(exc) or type(exc).__name__, MAX_MESSAGE_CHARS),
        "relation": relation,
        "frames": frames[:MAX_FRAMES_PER_LINK],
    }
    if len(frames) > MAX_FRAMES_PER_LINK:
        payload["framesTruncated"] = True
    notes = [str(note) for note in (getattr(exc, "__notes__", None) or []) if str(note)][:5]
    if notes:
        payload["notes"] = notes
    return payload


def _upstream(exc: BaseException) -> tuple[str, BaseException | None]:
    """``relation`` 描述「下一个环节相对当前异常是怎么来的」；没有上游时返回 ``("raised", None)``。"""
    if exc.__cause__ is not None:
        return "cause", exc.__cause__
    if exc.__context__ is not None and not exc.__suppress_context__:
        return "context", exc.__context__
    return "raised", None


def _chain(exc: BaseException, source_root: Path | None) -> list[dict[str, Any]]:
    """沿 ``__cause__`` / ``__context__`` 展开：用户真正看到的异常排在最前，上游依次排后。"""
    links: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    relation = "raised"
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        links.append(_exception_link(current, source_root, relation))
        relation, nxt = _upstream(current)
        if nxt is None:
            break
        current = nxt
        if len(links) >= MAX_LINKS:
            links[-1]["chainTruncated"] = True
            break
    return links


def _nest(links: list[dict[str, Any]]) -> dict[str, Any]:
    """把有序的链折叠成 ``cause`` 嵌套：每个环节只出现一次，UI 顺着 ``cause`` 就能走到根因。"""
    node: dict[str, Any] = {}
    for link in reversed(links):
        if node:
            link["cause"] = node
        node = link
    return node


def _links_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """按嵌套顺序展开各环节（含顶层），供裁剪时遍历。"""
    links: list[dict[str, Any]] = []
    node: dict[str, Any] | None = payload
    while node is not None:
        links.append(node)
        nxt = node.get("cause")
        node = nxt if isinstance(nxt, dict) else None
    return links


def _encoded_bytes(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _clip(payload: dict) -> None:
    """超预算时从链尾开始丢帧、再剥局部变量；顺序保证「异常类型与消息」一定留下。"""
    if _encoded_bytes(payload) <= MAX_PAYLOAD_BYTES:
        payload["truncated"] = False
        return
    for link in reversed(_links_of(payload)):
        frames = link.get("frames") or []
        while len(frames) > 1 and _encoded_bytes(payload) > MAX_PAYLOAD_BYTES:
            frames.pop()
    for link in _links_of(payload):
        for frame in link.get("frames") or []:
            if frame.get("locals") and _encoded_bytes(payload) > MAX_PAYLOAD_BYTES:
                frame["locals"] = []
                frame["localsTruncated"] = True
    payload["truncated"] = True


def describe_exception(exc: BaseException, source_root: str | os.PathLike | None = None) -> dict:
    """生成一次异常的结构化现场；``source_root`` 把沙箱绝对路径换回源码相对路径。"""
    root: Path | None = None
    if source_root is not None:
        try:
            root = Path(os.fspath(source_root)).resolve()
        except (OSError, ValueError):
            root = None
    payload = _nest(_chain(exc, root))
    _clip(payload)
    return payload
