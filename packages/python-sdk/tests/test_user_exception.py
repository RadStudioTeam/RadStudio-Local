"""结构化异常现场（:mod:`radstudio.user_exception`）测试。

纯文本 traceback 只能让人「读」，工作台要的是「点」：哪一行、当时变量是多少、怎么跳回编辑器。
分两层验证：

1. 纯函数——用测试进程里的活异常构造现场，逐条守住帧序、路径回映射、局部变量脱敏与预算裁剪。
2. 真实隔离子进程——用户代码报错后经 ``invoke`` → 结果信封 → ``run_debug_request`` 回传，
   证明 ``error.details.exception`` 整条链路真的落到工作台拿到的响应里（这是唯一硬证据）。
"""

from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from radstudio import user_exception, user_serve
from radstudio.user_exception import (
    MAX_LOCALS_PER_FRAME,
    MAX_MESSAGE_CHARS,
    MAX_VALUE_CHARS,
    describe_exception,
)

TEST_FILE = Path(__file__).name
HERE = Path(__file__).resolve().parent


def _raise_with_locals(answer: int, label: str):
    raise ValueError("除数不能为零")


def _wrap_and_raise():
    try:
        _raise_with_locals(42, "busi")
    except ValueError as exc:
        raise RuntimeError("算子执行失败") from exc


def _site_of_raiser() -> dict:
    """跑一次 ``_wrap_and_raise`` 并返回现场（异常本身不外泄给调用方）。"""
    try:
        _wrap_and_raise()
    except RuntimeError as exc:
        return describe_exception(exc, HERE)
    raise AssertionError("预期中的异常没有发生")


# --- 帧序、路径回映射与嵌套形状 --------------------------------------------


def test_head_is_the_exception_the_user_sees_and_cause_is_nested():
    site = _site_of_raiser()
    assert site["type"] == "RuntimeError"
    assert site["message"] == "算子执行失败"
    assert site["relation"] == "raised"
    # 顶层只描述自己，上游嵌在 cause 里：同一份帧不会既在 frames 又在 links 里各存一遍
    assert "links" not in site
    cause = site["cause"]
    assert cause["type"] == "ValueError"
    assert cause["relation"] == "cause"
    assert "cause" not in cause


def test_frames_run_from_the_error_outwards_with_relative_paths():
    site = _site_of_raiser()
    frames = site["cause"]["frames"]
    assert [frame["function"] for frame in frames[:2]] == ["_raise_with_locals", "_wrap_and_raise"]
    head = frames[0]
    assert head["path"] == TEST_FILE
    assert head["user"] is True
    assert head["code"] == 'raise ValueError("除数不能为零")'
    # 局部变量只属于出错那一帧能看见的名字
    assert {item["name"]: item["value"] for item in head["locals"]} == {"answer": "42", "label": "'busi'"}


def test_frames_outside_the_source_root_leak_neither_directories_nor_locals(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "boom.py"
    target.write_text("def boom(api_key='sk-live'):\n    raise KeyError('nope')\n", encoding="utf-8")
    namespace: dict = {}
    # 用例就是要一帧「不在 source_root 下的真实文件」，编译执行是最直接的造法
    exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), namespace)  # noqa: S102
    try:
        namespace["boom"]()
    except KeyError as exc:
        site = describe_exception(exc, tmp_path / "workspace")
    frame = site["frames"][0]
    assert frame["path"] == "boom.py"
    assert frame["user"] is False
    # 平台帧的 f_locals 可能含请求体与凭据派生值，一律不回显
    assert "locals" not in frame


def test_without_source_root_even_platform_frames_are_bare_file_names():
    try:
        importlib.import_module("this_module_does_not_exist_xyz")
    except ModuleNotFoundError as exc:
        site = describe_exception(exc)
    assert site["type"] == "ModuleNotFoundError"
    assert site["frames"], "有栈就逐帧给出位置"
    for frame in site["frames"]:
        assert frame["user"] is False
        assert "/" not in frame["path"] and "\\" not in frame["path"]


# --- 局部变量脱敏 ----------------------------------------------------------


def test_credential_like_and_private_locals_are_not_echoed():
    def leak(api_key, _cache, divisor):
        raise ZeroDivisionError("division by zero")

    try:
        leak("sk-live-123", {"a": 1}, 0)
    except ZeroDivisionError as exc:
        site = describe_exception(exc, HERE)
    names = {item["name"] for item in site["frames"][0]["locals"]}
    assert names == {"divisor"}


def test_values_are_summarised_and_capped():
    def wide(text, blob, table, items, helper):
        raise ValueError("看现场")

    try:
        wide("x" * (MAX_VALUE_CHARS + 50), b"\x00" * 4096, {f"k{i}": i for i in range(50)}, [1, 2, 3], wide)
    except ValueError as exc:
        site = describe_exception(exc, HERE)
    values = {item["name"]: item["value"] for item in site["frames"][0]["locals"]}
    assert values["blob"] == "bytes(len=4096)"
    assert values["items"] == "list(len=3)"
    assert values["table"].startswith("dict(len=50; keys=[")
    assert values["helper"].startswith("<callable ")
    assert len(values["text"]) == MAX_VALUE_CHARS and values["text"].endswith("…")


def test_hostile_repr_cannot_destroy_the_site():
    class Explodes:
        def __repr__(self):
            raise RuntimeError("repr 也要炸")

    def go(payload):
        raise ValueError("x")

    try:
        go(Explodes())
    except ValueError as exc:
        site = describe_exception(exc, HERE)
    values = {item["name"]: item["value"] for item in site["frames"][0]["locals"]}
    assert values["payload"] == "<Explodes repr failed>"


def test_locals_are_capped_per_frame():
    frame = SimpleNamespace(f_locals={f"v{i}": i for i in range(MAX_LOCALS_PER_FRAME + 6)})
    payload = user_exception._frame_payload(frame, __file__, TEST_FILE, 1, "run", True)
    assert len(payload["locals"]) == MAX_LOCALS_PER_FRAME
    assert payload["localsTruncated"] is True


# --- 异常链与预算 ----------------------------------------------------------


def test_context_without_cause_is_reported_as_context():
    def outer():
        try:
            _raise_with_locals(1, "x")
        except ValueError:
            raise IndexError("第二个错误")

    try:
        outer()
    except IndexError as exc:
        site = describe_exception(exc, HERE)
    assert site["cause"]["relation"] == "context"
    assert site["cause"]["type"] == "ValueError"


def test_suppressed_context_is_not_reported():
    try:
        try:
            _raise_with_locals(1, "x")
        except ValueError:
            raise IndexError("只剩这个") from None
    except IndexError as exc:
        site = describe_exception(exc, HERE)
    assert "cause" not in site


def _deep_chain(depth: int) -> BaseException:
    exc: BaseException = ValueError("根因")
    for index in range(depth):
        try:
            raise RuntimeError(f"第{index}层") from exc
        except RuntimeError as wrapped:
            exc = wrapped
    return exc


def test_chain_is_capped_and_flagged():
    site = describe_exception(_deep_chain(user_exception.MAX_LINKS + 2))
    links = user_exception._links_of(site)
    assert len(links) == user_exception.MAX_LINKS
    assert links[-1]["chainTruncated"] is True
    assert all(link["type"] for link in links)


def test_an_exception_without_a_traceback_still_describes_itself():
    site = describe_exception(ValueError("没跑过"))
    assert site["type"] == "ValueError"
    assert site["frames"] == []
    assert site["truncated"] is False


def test_message_is_capped():
    site = describe_exception(ValueError("长" * (MAX_MESSAGE_CHARS + 200)))
    assert len(site["message"]) == MAX_MESSAGE_CHARS


def test_oversize_site_drops_far_frames_then_locals(monkeypatch):
    monkeypatch.setattr(user_exception, "MAX_PAYLOAD_BYTES", 512)

    def deep(level: int, a: str, b: str, c: str, d: str, e: str, f: str):
        if level:
            return deep(level - 1, a, b, c, d, e, f)
        raise ValueError("太深了")

    wide = "y" * 200
    try:
        deep(12, wide, wide, wide, wide, wide, wide)
    except ValueError as exc:
        site = describe_exception(exc, HERE)
    assert site["truncated"] is True
    # 类型与消息一定留下：瘦身只动帧与局部变量
    assert site["type"] == "ValueError" and site["message"] == "太深了"
    # deep(12) 有 13 层递归帧再加本测试帧，先从链尾丢远处的帧
    assert len(site["frames"]) < 14
    # 剩下那一帧的局部变量自己就超预算，丢帧救不了 → 连栈顶也要剥
    assert all(not frame.get("locals") for frame in site["frames"])
    assert site["frames"][0]["localsTruncated"] is True


def test_notes_are_carried():
    try:
        raise ValueError("带注释")
    except ValueError as wrapped:
        wrapped.add_note("检查数据集版本是否为空")
        wrapped.add_note("检查阈值参数")
        site = describe_exception(wrapped, HERE)
    assert site["notes"] == ["检查数据集版本是否为空", "检查阈值参数"]


# --- 语法错误：没有可执行帧也要给一个可点的位置 ----------------------------


def test_syntax_error_gets_a_clickable_frame(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    broken = root / "broken.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")
    try:
        compile(broken.read_text(encoding="utf-8"), str(broken), "exec")
    except SyntaxError as exc:
        site = describe_exception(exc, root)
    frame = site["frames"][0]
    assert frame["path"] == "broken.py"
    assert frame["line"] == 1 and frame["user"] is True
    assert frame["code"] == "def f(:"


def test_syntax_frame_is_not_duplicated_when_the_stack_already_points_there(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    target = root / "dup.py"
    target.write_text("print('第一行')\nraise SyntaxError('重复帧')\n", encoding="utf-8")
    try:
        # 栈顶已指向出错那一行时不该再补一帧，需要真实 exec 栈才测得到
        exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), {})  # noqa: S102
    except SyntaxError as exc:
        exc.lineno = 2
        link = user_exception._exception_link(exc, root, "raised")
    places = [(frame["path"], frame["line"]) for frame in link["frames"]]
    assert places.count(("dup.py", 2)) == 1


# --- 真实隔离子进程：现场要真的落到工作台响应里 ----------------------------


def _go_code_hash(files: list[dict]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry["path"]):
        per_file = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
        digest.update(f"{item['path']}\x00{per_file}\x00".encode())
    return digest.hexdigest()


_FAIL_SOURCE = '''"""工作台调试用报错算子：真实除零，栈与局部变量都该能点回编辑器。"""

from radstudio.operator import Operator, operator


@operator("WorkbenchFail", version="1.0.0")
class WorkbenchFail(Operator):
    def run(self, ctx):
        samples = ctx.input_json("samples")
        divisor = ctx.parameters.get("threshold", 0)
        report = {"count": len(samples), "spread": len(samples) / divisor}
        return {"report": ctx.output_json("report", report, "application/json")}
'''

_OK_SOURCE = _FAIL_SOURCE.replace("len(samples) / divisor", "divisor + 0")

_BAD_SOURCE = '''"""工作台调试用坏源码：方法体漏了冒号，import 期就编译不过。"""

from radstudio.operator import Operator, operator


class WorkbenchFail(Operator):
    def run(self, ctx)
        return {}
'''


def _envelope(module: str, source: str, **overrides) -> dict:
    files = [{"path": module, "content": source}]
    envelope = {
        "requestId": "dbg-exc",
        "operatorType": "WorkbenchFail",
        "operatorVersion": "1.0.0",
        "entrypoint": f"{module[:-3]}:WorkbenchFail",
        "parameters": {"threshold": 0},
        "code": {"files": files, "sha256": _go_code_hash(files)},
        "inputs": {"samples": {"json": [{"id": "a"}, {"id": "b"}]}},
        "allowPackages": ["numpy"],
        "timeoutSeconds": 90,
    }
    envelope.update(overrides)
    return envelope


@pytest.fixture(scope="module")
def failing_run() -> dict:
    return user_serve.run_debug_request(_envelope("fail_op.py", _FAIL_SOURCE))


def test_debug_run_carries_the_structured_site_from_a_real_subprocess(failing_run):
    assert failing_run["status"] == "failed"
    details = failing_run["error"]["details"]
    # 纯文本栈继续保留：日志导出与「复制去问人」仍然需要它
    assert "ZeroDivisionError" in details["traceback"]
    site = details["exception"]
    assert site["type"] == "ZeroDivisionError"
    head = site["frames"][0]
    assert head["path"] == "fail_op.py" and head["user"] is True
    assert head["line"] > 0
    assert {"samples", "divisor"} <= {item["name"] for item in head["locals"]}
    assert failing_run["outputs"] == []


@pytest.mark.skipif(sys.platform.startswith("win"), reason="resource 模块只在 POSIX 提供")
def test_debug_run_reports_peak_memory_from_a_real_subprocess(failing_run):
    assert failing_run["metrics"]["userOperator"]["peakRssBytes"] > 0


def test_success_and_peak_memory_travel_together():
    body = user_serve.run_debug_request(
        _envelope("ok_op.py", _OK_SOURCE, parameters={"threshold": 2})
    )
    assert body["status"] == "completed"
    metrics = body["metrics"]["userOperator"]
    assert metrics["exitCode"] == 0
    if sys.platform.startswith("win"):
        assert "peakRssBytes" not in metrics
    else:
        assert metrics["peakRssBytes"] > 0


def test_import_time_syntax_error_points_at_the_user_file():
    body = user_serve.run_debug_request(_envelope("broken_op.py", _BAD_SOURCE))
    assert body["status"] == "failed"
    assert body["error"]["code"] == "user_import_failed"
    site = body["error"]["details"]["exception"]
    user_frames = [
        frame for link in user_exception._links_of(site) for frame in link["frames"] if frame["user"]
    ]
    # 用户代码 import 期就炸：栈里只有 importlib 的平台帧，靠 SyntaxError 自己补出位置
    assert [(frame["path"], frame["user"]) for frame in user_frames] == [("broken_op.py", True)]
    assert user_frames[0]["line"] > 0 and user_frames[0]["code"]
