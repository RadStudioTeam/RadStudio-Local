"""Physical artifact identity and safe workspace boundaries; no scientific content claim."""

import hashlib
import json
import re
import stat
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

REF_PATTERN = re.compile(r"^file:(?P<path>.+)#sha256=(?P<sha256>[0-9a-f]{64});type=(?P<type>.+)$")


class ArtifactBoundaryError(ValueError):
    def __init__(self, code, message, details=None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


@lru_cache(maxsize=1)
def reference_rules():
    return json.loads(
        files("radstudio")
        .joinpath("schemas/artifact-reference.rules.json")
        .read_text(encoding="utf-8")
    )


def validate_relative_path(name):
    rules = reference_rules()
    if (
        not isinstance(name, str)
        or not name
        or len(name.encode("utf-8")) > rules["maxPathBytes"]
        or "\\" in name
        or ":" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ArtifactBoundaryError("artifact.path_invalid", "产物必须使用有界的POSIX相对路径")
    for part in name.split("/"):
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or part.split(".")[0].upper() in rules["reservedBasenames"]
        ):
            raise ArtifactBoundaryError(
                "artifact.path_invalid", "产物路径包含越界、非规范或保留名称"
            )
    return name


def validate_kind(kind):
    rules = reference_rules()
    if (
        not isinstance(kind, str)
        or len(kind.encode("utf-8")) > rules["maxTypeBytes"]
        or re.fullmatch(rules["typePattern"], kind) is None
    ):
        raise ArtifactBoundaryError("artifact.type_invalid", "产物物理类型标识无效")


def parse_reference(reference):
    rules = reference_rules()
    if (
        not isinstance(reference, str)
        or len(reference.encode("utf-8")) > rules["maxPathBytes"] + rules["maxTypeBytes"] + 96
    ):
        raise ArtifactBoundaryError("artifact.ref_invalid", "产物引用不是有界文本")
    match = REF_PATTERN.fullmatch(reference)
    if match is None:
        raise ArtifactBoundaryError("artifact.ref_invalid", "产物引用格式或SHA-256无效")
    result = match.groupdict()
    validate_relative_path(result["path"])
    validate_kind(result["type"])
    return result


def workspace_path(root, name, *, must_exist=False):
    validate_relative_path(name)
    root = Path(root).resolve()
    path = root.joinpath(*name.split("/"))
    cursor = root
    for part in name.split("/"):
        cursor /= part
        if cursor.is_symlink():
            raise ArtifactBoundaryError("artifact.path_invalid", "产物路径不能包含符号链接")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ArtifactBoundaryError("artifact.path_invalid", "产物越过工作区边界")
    if must_exist and (not resolved.exists() or not stat.S_ISREG(resolved.stat().st_mode)):
        raise ArtifactBoundaryError(
            "artifact.file_missing", "产物文件不存在或不是普通文件：" + name
        )
    return resolved


def hash_file(path):
    digest, size = hashlib.sha256(), 0
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def validate_inputs(inputs, root, ports=None, *, verify_content=False):
    try:
        _ports(inputs, ports, "输入")
        cache = {}
        for port, reference in inputs.items():
            info = parse_reference(reference)
            path = workspace_path(root, info["path"], must_exist=True)
            if path not in cache:
                cache[path] = hash_file(path)
            if cache[path][0] != info["sha256"]:
                raise ValueError("输入产物哈希不匹配：" + port)
            if verify_content:
                _content(path, info["type"], port, ports, root)
    except (ValueError, OSError, TypeError) as error:
        raise ArtifactBoundaryError(
            "operator.input_invalid", str(error), getattr(error, "details", {})
        ) from error


def validate_input_sets(input_sets, root, ports=None, *, verify_content=False):
    """校验多值端口（cardinality=many）的输入集合：类型、哈希与内容契约。"""
    try:
        if not isinstance(input_sets, dict) or any(
            not isinstance(key, str) or not key for key in input_sets
        ):
            raise ValueError("输入集合必须是非空名称到引用列表的对象")
        declared = None if ports is None else {port["name"]: port for port in ports}
        if declared is not None:
            if set(input_sets) - set(declared):
                raise ValueError(
                    "输入集合包含未声明端口："
                    + ", ".join(sorted(set(input_sets) - set(declared)))
                )
            for name, port in declared.items():
                if port.get("cardinality") != "many":
                    if name in input_sets:
                        raise ValueError("输入集合只能用于多值端口：" + name)
                    continue
                if port.get("required", False) and name not in input_sets:
                    raise ValueError("输入集合缺少必需端口：" + name)
        cache = {}
        for port, references in input_sets.items():
            definition = (declared or {}).get(port) or {}
            if not isinstance(references, list) or not references:
                raise ValueError("输入集合的端口必须是至少一个引用的列表：" + port)
            for index, reference in enumerate(references, start=1):
                info = parse_reference(reference)
                if definition and info["type"] != definition["type"]:
                    raise ValueError(
                        "输入集合物理类型与声明不一致：" + port + " 第 " + str(index) + " 项"
                    )
                path = workspace_path(root, info["path"], must_exist=True)
                if path not in cache:
                    cache[path] = hash_file(path)
                if cache[path][0] != info["sha256"]:
                    raise ValueError(
                        "输入集合产物哈希不匹配：" + port + " 第 " + str(index) + " 项"
                    )
                if verify_content and definition:
                    _content(path, info["type"], port, ports, root)
    except (ValueError, OSError, TypeError) as error:
        raise ArtifactBoundaryError(
            "operator.input_invalid", str(error), getattr(error, "details", {})
        ) from error


def _ports(values, ports, label):
    if not isinstance(values, dict) or any(not isinstance(key, str) or not key for key in values):
        raise ValueError(label + "端口必须是非空名称到引用的对象")
    if ports is None:
        return
    declared = {port["name"]: port for port in ports}
    if set(values) - set(declared):
        raise ValueError(
            label + "包含未声明端口：" + ", ".join(sorted(set(values) - set(declared)))
        )
    for name, port in declared.items():
        if port.get("cardinality") == "many":
            if name in values:
                raise ValueError(label + "多值端口必须经输入集合提供：" + name)
            continue
        if port.get("required", False) and name not in values:
            raise ValueError(label + "缺少必需端口：" + name)
        if name in values and parse_reference(values[name])["type"] != port["type"]:
            raise ValueError(label + "物理类型与声明不一致：" + name)


def validate_outputs(
    outputs,
    artifacts,
    root,
    inputs=None,
    ports=None,
    *,
    verify_content=False,
    input_sets=None,
):
    try:
        _ports(outputs, ports, "输出")
        cache, declared = {}, {}
        for artifact in artifacts:
            value = artifact.to_dict() if hasattr(artifact, "to_dict") else artifact
            name, kind = value["storagePath"], value["type"]
            validate_kind(kind)
            path = workspace_path(root, name, must_exist=True)
            if name in declared:
                raise ValueError("重复产物声明：" + name)
            if path not in cache:
                cache[path] = hash_file(path)
            digest, size = cache[path]
            if (
                value["fileHash"] != digest
                or isinstance(value["fileSize"], bool)
                or not isinstance(value["fileSize"], int)
                or value["fileSize"] != size
            ):
                raise ValueError("产物哈希/大小与实际文件不一致：" + name)
            declared[name] = value
        passthrough = set((inputs or {}).values())
        for references in (input_sets or {}).values():
            passthrough.update(references)
        for port, reference in outputs.items():
            info = parse_reference(reference)
            path = workspace_path(root, info["path"], must_exist=True)
            if path not in cache:
                cache[path] = hash_file(path)
            if cache[path][0] != info["sha256"]:
                raise ValueError("输出引用哈希不匹配：" + port)
            if verify_content:
                _content(path, info["type"], port, ports, root)
            meta = declared.get(info["path"])
            if meta is None and reference not in passthrough:
                raise ValueError("输出未登记为产物，也不是显式输入透传：" + port)
            if meta is not None and (
                meta["type"] != info["type"] or meta["fileHash"] != info["sha256"]
            ):
                raise ValueError("输出引用与产物声明不一致：" + port)
        return {
            "rules": "artifact-boundary/1",
            "portsVerified": ports is not None,
            "outputs": len(outputs),
            "artifacts": len(artifacts),
        }
    except (ValueError, OSError, TypeError, KeyError) as error:
        raise ArtifactBoundaryError(
            "operator.output_invalid", str(error), getattr(error, "details", {})
        ) from error


def _content(path, kind, name, ports, root):
    from .content_contract import validate_content_file

    definition = next((port for port in (ports or []) if port["name"] == name), {})
    if definition.get("contract") is not None:
        validate_content_file(path, kind, definition["contract"], reference_roots=[root])
