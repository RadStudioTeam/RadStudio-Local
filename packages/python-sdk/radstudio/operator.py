"""统一算子运行时框架。

每个算子是一个用 ``@operator("TypeName", version=...)`` 装饰的类，可同时被：
- serve 模式（``python -m radstudio.operator_serve``）按 type@version 经 HTTP 调用；
- 单体 runner（radstudio.runner）在容器内复现包回放时复用。

运行时契约对齐 packages/operator-schema 下 operator-request / operator-result schema：
请求携带 requestId/runId/nodeId/operatorType/parameters/inputs（产物引用字符串），
结果携带 status/outputs（产物引用字符串）/artifacts/error。

产物引用（artifact reference）字符串格式：
    file:<相对路径>#sha256=<hex>;type=<artifactType>
本地目录模式下相对路径以工作区根目录为基准；对象存储/云函数模式下由
BundleArtifactStore 在临时工作区解包后按同样规则解析。相同输入重算产出
相同内容寻址哈希，保证幂等。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import traceback
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

from .artifact_boundary import (
    REF_PATTERN as _REF_PATTERN,
)
from .artifact_boundary import (
    ArtifactBoundaryError,
    hash_file,
    parse_reference,
    validate_input_sets,
    validate_inputs,
    validate_kind,
    validate_outputs,
    workspace_path,
)
from .json_contract import encode_runtime_json
from .user_exception import describe_exception

# 样本清单中以工作区相对路径引用文件的字段（与 Go 侧 transferManifestRefs 对齐）。
_MANIFEST_PATH_FIELDS = ("imagePath", "roiPath")

# invoke(capture_traceback=True) 时保留的用户代码栈摘要上限（字符）。
_MAX_TRACEBACK_CHARS = 4000


def collect_manifest_refs(doc: Any) -> set[str]:
    """收集 JSON 清单间接引用的工作区文件（样本路径字段 + 产物 ref 字符串）。"""
    refs: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if (
                    isinstance(child, str)
                    and key in _MANIFEST_PATH_FIELDS
                    and child
                    and not Path(child).is_absolute()
                ):
                    refs.add(child.replace("\\", "/"))
                else:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, str):
            m = _REF_PATTERN.match(node)
            if m:
                refs.add(m.group("path").replace("\\", "/"))

    walk(doc)
    return refs


def parse_ref(ref: str) -> dict:
    """Parse a portable, bounded artifact reference."""
    return parse_reference(ref)


def make_ref(path: str, sha256: str, kind: str) -> str:
    return f"file:{path}#sha256={sha256};type={kind}"


def ref_to_legacy(ref: str) -> dict:
    """字符串引用 → 单体 runner Context 的 dict 引用（path/sha256/type）。"""
    return parse_ref(ref)


def legacy_to_ref(ref: dict) -> str:
    """单体 runner Context 的 dict 引用 → 字符串引用。"""
    return make_ref(ref["path"], ref["sha256"], ref["type"])


# ---------------------------------------------------------------------------
# 契约数据类（operator-request / operator-result schema）
# ---------------------------------------------------------------------------


@dataclass
class OperatorRequest:
    """算子执行请求（operator-request.schema.json）。"""

    requestId: str
    runId: str
    nodeId: str
    operatorType: str
    parameters: dict[str, Any]
    inputs: dict[str, str] = field(default_factory=dict)
    operatorVersion: str | None = None
    environment: dict[str, Any] | None = None
    transfer: dict[str, Any] | None = None  # 带外传输元数据（对象模式）；内联模式为 None

    parameterContract: dict | None = None
    executionControls: dict | None = None
    invocationContract: dict | None = None
    inputSets: dict[str, list[str]] = field(default_factory=dict)  # cardinality=many 端口（协议 3）

    @staticmethod
    def from_dict(data: dict) -> OperatorRequest:
        required = ["requestId", "runId", "nodeId", "operatorType", "parameters", "inputs"]
        missing = [k for k in required if k not in data]
        if missing:
            raise ValueError("OperatorRequest missing required fields: " + ", ".join(missing))
        for key in ("requestId", "runId", "nodeId", "operatorType"):
            if not isinstance(data[key], str) or not data[key]:
                raise TypeError(f"{key} must be a non-empty string")
        if data.get("operatorVersion") is not None and (
            not isinstance(data["operatorVersion"], str) or not data["operatorVersion"]
        ):
            raise TypeError("operatorVersion must be a non-empty string when provided")
        if not isinstance(data["parameters"], dict):
            raise TypeError("parameters must be an object")
        if not isinstance(data["inputs"], dict) or not all(
            isinstance(v, str) for v in data["inputs"].values()
        ):
            raise TypeError("inputs must be an object of string references")
        input_sets: dict[str, list[str]] = {}
        raw_sets = data.get("inputSets")
        if raw_sets is not None:
            if not isinstance(raw_sets, dict):
                raise TypeError("inputSets must be an object")
            for port, refs in raw_sets.items():
                if not isinstance(refs, list):
                    raise TypeError("inputSets values must be arrays")
                if not refs or not all(isinstance(ref, str) and ref for ref in refs):
                    raise TypeError("inputSets values must be arrays of string references")
                input_sets[port] = list(refs)
            repeated = sorted(set(input_sets) & set(data["inputs"]))
            if repeated:
                raise ValueError(
                    "inputSets port repeats single-value input: " + ", ".join(repeated)
                )
        if "invocationContract" in data and data["invocationContract"] is None:
            raise ValueError("invocationContract cannot be null")
        return OperatorRequest(
            requestId=data["requestId"],
            runId=data["runId"],
            nodeId=data["nodeId"],
            operatorType=data["operatorType"],
            parameters=data["parameters"],
            inputs=dict(data["inputs"]),
            operatorVersion=data.get("operatorVersion"),
            environment=data.get("environment"),
            transfer=data.get("transfer"),
            parameterContract=data.get("parameterContract"),
            executionControls=data.get("executionControls"),
            invocationContract=data.get("invocationContract"),
            inputSets=input_sets,
        )


@dataclass
class ArtifactMeta:
    """产物登记元数据（operator-result.schema.json ArtifactRef）。"""

    artifactId: str
    type: str
    storagePath: str
    fileHash: str
    fileSize: int

    def to_dict(self) -> dict:
        return {
            "artifactId": self.artifactId,
            "type": self.type,
            "storagePath": self.storagePath,
            "fileHash": self.fileHash,
            "fileSize": self.fileSize,
        }


@dataclass
class OperatorError:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass
class OperatorResult:
    """算子执行结果（operator-result.schema.json）。"""

    requestId: str
    runId: str
    nodeId: str
    status: str  # completed | failed | cancelled
    outputs: dict[str, str] = field(default_factory=dict)
    artifacts: list[ArtifactMeta] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    error: OperatorError | None = None
    outputBundle: str | None = None  # serve 扩展：输出工作区 tar.gz base64（内联模式）
    transfer: dict[str, Any] | None = None  # 带外传输回填（对象模式）：outputSha256/outputSize
    executionContract: dict | None = None
    implementation: dict[str, str] | None = None

    def to_dict(self) -> dict:
        out = {
            "requestId": self.requestId,
            "runId": self.runId,
            "nodeId": self.nodeId,
            "status": self.status,
            "outputs": self.outputs,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "metrics": self.metrics,
        }
        if self.executionContract is not None:
            out["executionContract"] = self.executionContract
        if self.implementation is not None:
            out["implementation"] = self.implementation
        if self.error is not None:
            out["error"] = self.error.to_dict()
        if self.outputBundle is not None:
            out["outputBundle"] = self.outputBundle
        if self.transfer is not None:
            out["transfer"] = self.transfer
        return out


# ---------------------------------------------------------------------------
# 产物存储抽象：本地目录 / 云函数 bundle 双模式
# ---------------------------------------------------------------------------


class ArtifactStore:
    """算子工作区产物存储。

    所有算子实现只经此接口读写产物与本地文件（如影像 imagePath），
    不感知自己是跑在单体 runner 工作区还是云函数解包的临时目录。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._registry: dict[str, ArtifactMeta] = {}

    # -- 读 --
    def read_bytes(self, ref: str) -> bytes:
        info = parse_ref(ref)
        path = workspace_path(self.root, info["path"])
        if not path.is_file():
            raise FileNotFoundError(f"Artifact not found in workspace: {info['path']}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise ValueError(f"Artifact hash mismatch: {info['path']}")
        return data

    def read_json(self, ref: str) -> Any:
        return json.loads(self.read_bytes(ref))

    def resolve_path(self, path: str) -> Path:
        """解析算子内引用的本地文件（samples.imagePath 等）。
        绝对路径原样返回（单体 runner 场景）；相对路径以工作区根为基准。"""
        p = Path(path)
        return (
            p
            if p.is_absolute()
            else workspace_path(self.root, path if isinstance(path, str) else p.as_posix())
        )

    # -- 写 --
    def write_bytes(self, name: str, data: bytes, kind: str) -> str:
        """写产物文件，返回内容寻址引用。相同内容重算得到相同哈希（幂等）。"""
        validate_kind(kind)
        sha256 = hashlib.sha256(data).hexdigest()
        path = workspace_path(self.root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ref = make_ref(name, sha256, kind)
        self._registry[name] = ArtifactMeta(
            artifactId=str(uuid.uuid4()),
            type=kind,
            storagePath=name,
            fileHash=sha256,
            fileSize=len(data),
        )
        return ref

    def write_json(self, name: str, value: Any, kind: str) -> str:
        return self.write_bytes(
            name,
            encode_runtime_json(value),
            kind,
        )

    def write_local(self, name: str, data: bytes) -> str:
        """写工作区本地文件（不登记为产物），返回可被 resolve_path 解析的相对路径。

        单体 runner 的适配存储会返回绝对路径以兼容下游直接打开的既有行为。
        """
        path = workspace_path(self.root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return name

    def register(self, name: str, kind: str) -> None:
        """登记已存在的产物文件（如算子直接写文件后补登记）。"""
        validate_kind(kind)
        path = workspace_path(self.root, name, must_exist=True)
        digest, size = hash_file(path)
        self._registry[name] = ArtifactMeta(
            artifactId=str(uuid.uuid4()),
            type=kind,
            storagePath=name,
            fileHash=digest,
            fileSize=size,
        )

    def artifacts(self) -> list[ArtifactMeta]:
        return list(self._registry.values())


class BundleArtifactStore(ArtifactStore):
    """云函数 bundle 模式：请求内联输入工作区（tar.gz base64），响应内联输出工作区。

    对象存储带外模式下改用 import_bytes / export_bytes 直接收发 tar.gz 原始字节
    （由 operator_serve 经预签名 URL 拉取/回传），import_bundle / export_bundle
    为其 base64 包装，保持内联模式契约不变。

    导出只包含本次执行写入的文件：输入经 materialize_file/import_bytes 落地时
    不登记，避免把整个输入工作区（影像、预处理 npz 可达数百 MB）再打包回传。
    """

    def __init__(self, root: Path):
        super().__init__(root)
        self._written: set[str] = set()

    def resolve_path(self, path: str) -> Path:
        return workspace_path(self.root, path)

    def write_bytes(self, name: str, data: bytes, kind: str) -> str:
        ref = super().write_bytes(name, data, kind)
        self._written.add(name)
        return ref

    def write_local(self, name: str, data: bytes) -> str:
        ref = super().write_local(name, data)
        self._written.add(name)
        return ref

    def register(self, name: str, kind: str) -> None:
        super().register(name, kind)
        self._written.add(name)

    def import_bytes(self, raw: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:

            def is_within_root(member: tarfile.TarInfo) -> bool:
                target = (self.root / member.name).resolve()
                return target.is_relative_to(self.root.resolve())

            for member in tar.getmembers():
                if not is_within_root(member):
                    raise ValueError(f"Bundle entry escapes workspace: {member.name}")
                if member.issym() or member.islnk():
                    raise ValueError(f"Bundle entry must not be a link: {member.name}")
            tar.extractall(self.root, filter="data")

    def _output_files(self) -> list[Path]:
        files: list[Path] = []
        for rel in sorted(self._written):
            path = self.root / rel
            if path.is_file():
                files.append(path)
        return files

    def written_names(self) -> set[str]:
        """本次执行登记写入的相对路径集合（隔离子进程回传产物的依据）。"""
        return set(self._written)

    def adopt_written(self, names: Iterable[str]) -> set[str]:
        """把外部（隔离子进程）写入工作区的文件纳入导出集合。

        用户算子在子进程里运行，父进程的 store 没见过这些写入；子进程回传文件名后
        由父进程重新按工作区边界校验并登记，路径逃逸与不存在的条目直接拒绝，
        保证导出集合里只有本次调用真正产出的文件。
        """
        adopted: set[str] = set()
        root = self.root.resolve()
        for name in names:
            rel = str(name).replace("\\", "/").strip("/")
            if not rel or rel in self._written:
                continue
            path = workspace_path(root, rel, must_exist=True)
            key = path.relative_to(root).as_posix()
            self._written.add(key)
            adopted.add(key)
        return adopted

    def track_referenced_outputs(self, skip: set[str] | None = None) -> set[str]:
        """把输出 JSON 间接引用的透传输入纳入导出集合（非挂载模式的兼容路径）。

        样本清单（samples.json）只重写路径不复制字节：下游节点解包输出工作区后
        仍要能读到 imagePath/roiPath 指向的文件。挂载模式下这些文件由 OSS 直读，
        通过 skip 排除，避免把挂载内容打包回传。
        """
        skip = {p.replace("\\", "/") for p in (skip or ())}
        pending: set[str] = set()
        for rel in list(self._written):
            if not rel.lower().endswith(".json"):
                continue
            path = self.root / rel
            try:
                pending |= collect_manifest_refs(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        added: set[str] = set()
        for rel in pending:
            if rel in skip or rel in self._written:
                continue
            if (self.root / rel).is_file():
                self._written.add(rel)
                added.add(rel)
        return added

    def mount_file(self, relpath: str, source: Path) -> None:
        """把 OSS 挂载点文件物化进工作区（流式复制，不整读入内存）。

        说明：OSS 挂载为只读。算子会以同名输出覆写部分输入清单（如
        QualityControl 重写 samples.json），若输入用符号链接指向挂载点，
        写输出会跟随链接写穿到只读挂载并报 EROFS；因此这里统一复制，字节
        仍直接来自 OSS 挂载，不经过 worker 传输。
        """
        target = (self.root / relpath).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError(f"Mount entry escapes workspace: {relpath}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        with source.open("rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)

    def export_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for path in self._output_files():
                tar.add(path, arcname=str(path.relative_to(self.root)))
        return buffer.getvalue()

    def export_to(self, path: Path) -> tuple[int, str]:
        """流式导出本次写入的产物 tar.gz 到文件，返回 (字节数, sha256)。

        对象存储带外模式专用：大输出不整包驻留内存，直接落盘供流式上传
        （云函数环境内存受限且按量计费）。只打包 _written 中登记的文件，
        输入工作区不参与输出传输。
        """
        hasher = hashlib.sha256()
        state = {"size": 0}

        with path.open("wb") as fp:

            def write(data: bytes) -> int:
                n = fp.write(data)
                hasher.update(data)
                state["size"] += n
                return n

            sink = SimpleNamespace(write=write, tell=lambda: state["size"], flush=fp.flush)
            with tarfile.open(fileobj=sink, mode="w:gz") as tar:
                for p in self._output_files():
                    tar.add(p, arcname=str(p.relative_to(self.root)))
        return state["size"], hasher.hexdigest()

    def materialize_file(self, relpath: str, data: bytes) -> None:
        """按输入清单物化一个工作区文件（带路径逃逸防护），供对象模式逐文件下载后落地。"""
        target = (self.root / relpath).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError(f"Manifest entry escapes workspace: {relpath}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def import_bundle(self, bundle_b64: str) -> None:
        self.import_bytes(base64.b64decode(bundle_b64))

    def export_bundle(self) -> str:
        return base64.b64encode(self.export_bytes()).decode()


def temp_store(prefix: str = "radstudio-op-") -> ArtifactStore:
    """创建临时工作区存储（serve 模式每次调用一个）。"""
    return ArtifactStore(Path(tempfile.mkdtemp(prefix=prefix)))


# ---------------------------------------------------------------------------
# 算子注册与调用
# ---------------------------------------------------------------------------

# key: "type@version" 与 "type"（后者指向最新注册版本）
OPERATOR_REGISTRY: dict[str, type[Operator]] = {}


def operator(operator_type: str, version: str = "1.0.0"):
    """注册算子实现类。同一实现可被 serve 模式与单体 runner 同时复用。"""

    def decorator(cls):
        if not issubclass(cls, Operator):
            raise TypeError(f"@operator can only decorate Operator subclasses: {cls}")
        cls.operator_type = operator_type
        cls.operator_version = version
        OPERATOR_REGISTRY[f"{operator_type}@{version}"] = cls
        OPERATOR_REGISTRY[operator_type] = cls
        return cls

    return decorator


class Operator:
    """算子基类。run 返回 {输出端口名: 产物引用字符串}。"""

    operator_type: str = ""
    operator_version: str = ""

    def run(self, ctx: OperatorContext) -> dict[str, str]:
        raise NotImplementedError


class OperatorCancelled(Exception):
    """算子执行被取消（异步任务收到 worker 的取消标记）。"""


class OperatorContext:
    """算子执行上下文：参数、输入产物引用、产物写出与进度。"""

    def __init__(
        self,
        request: OperatorRequest,
        store: ArtifactStore,
        emit: Callable[[str, dict], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ):
        self.request = request
        self.store = store
        self.parameters = request.parameters
        self.inputs = request.inputs
        self.emit = emit or (lambda event, payload: None)
        self._cancel_check = cancel_check or (lambda: False)

    def check_cancelled(self) -> None:
        """长耗时算子在样本/分片边界调用；取消后抛 OperatorCancelled。

        释放正在执行的算子（PyRadiomics 单样本）无法安全中断，最短的
        响应粒度是当前样本处理完成后的下一次检查。
        """
        if self._cancel_check():
            raise OperatorCancelled("operator execution cancelled by worker")

    # -- 输入 --
    def input_bytes(self, name: str) -> bytes:
        if name not in self.inputs:
            raise ValueError(f"Missing required input: {name}")
        return self.store.read_bytes(self.inputs[name])

    def input_json(self, name: str) -> Any:
        return json.loads(self.input_bytes(name))

    # -- 多值输入（cardinality=many，按声明顺序） --
    def input_set(self, name: str) -> list[bytes]:
        refs = self.request.inputSets.get(name)
        if not refs:
            raise ValueError(f"Missing required input set: {name}")
        return [self.store.read_bytes(ref) for ref in refs]

    def input_set_json(self, name: str) -> list[Any]:
        return [json.loads(data) for data in self.input_set(name)]

    # -- 输出 --
    def output_bytes(self, name: str, data: bytes, kind: str) -> str:
        ref = self.store.write_bytes(name, data, kind)
        self.emit(
            "artifact.created",
            {
                "nodeId": self.request.nodeId,
                "name": name,
                "storagePath": parse_ref(ref)["path"],
                "artifactType": kind,
            },
        )
        return ref

    def output_json(self, name: str, value: Any, kind: str) -> str:
        return self.output_bytes(
            name,
            encode_runtime_json(value),
            kind,
        )

    def progress(self, percent: float, message: str = "") -> None:
        self.emit(
            "node.progress",
            {"nodeId": self.request.nodeId, "percent": percent, "message": message},
        )


class OperatorResolutionError(KeyError):
    def __init__(self, operator_type: str, version: str | None):
        self.code = "operator.version_unavailable" if version else "operator.type_unavailable"
        self.details = {"operatorType": operator_type, "requestedVersion": version or ""}
        if version:
            message = f"此执行环境未提供算子版本 {operator_type}@{version}；请核对批准镜像与部署绑定，不会回退到其他版本"
        else:
            message = f"此执行环境未提供算子 {operator_type}；请核对批准镜像与部署绑定"
        super().__init__(message)

    def __str__(self):
        return str(self.args[0])


@lru_cache(maxsize=1)
def legacy_fc_aliases():
    document = json.loads(
        files("radstudio").joinpath("schemas/legacy-fc-aliases.json").read_text(encoding="utf-8")
    )
    if document.get("schemaVersion") != 1:
        raise ValueError("unsupported legacy FC alias schema")
    aliases = {}
    for item in document["aliases"]:
        key = (item["operatorType"], item["deploymentVersion"])
        target = item["implementationVersion"]
        if (
            key in aliases
            or not all(isinstance(value, str) and value for value in (*key, target))
            or key[1] != target + "-fc"
        ):
            raise ValueError("invalid or duplicate legacy FC alias")
        aliases[key] = target
    return MappingProxyType(aliases)


def _validate_operator_selection(operator_type: str, version: str | None) -> None:
    if not isinstance(operator_type, str) or not operator_type:
        raise TypeError("operatorType must be a non-empty string")
    if version is not None and (not isinstance(version, str) or not version):
        raise TypeError("operatorVersion must be a non-empty string when provided")


def resolve_invocation(operator_type: str, version: str | None, allow_legacy_fc_aliases: bool):
    _validate_operator_selection(operator_type, version)
    if version and f"{operator_type}@{version}" in OPERATOR_REGISTRY:
        return resolve_operator(operator_type, version), {
            "operatorType": operator_type,
            "requestedVersion": version,
            "resolvedVersion": version,
            "selection": "exact",
        }
    alias = (
        legacy_fc_aliases().get((operator_type, version))
        if allow_legacy_fc_aliases and version
        else None
    )
    if alias is not None:
        try:
            implementation = resolve_operator(operator_type, alias)
        except OperatorResolutionError as error:
            error.details.update(requestedVersion=version, aliasTargetVersion=alias)
            error.args = (
                f"部署版本 {operator_type}@{version} 映射到 {alias}，但此环境缺少该实现；请更新批准镜像，不会选择其他版本",
            )
            raise
        return implementation, {
            "operatorType": operator_type,
            "requestedVersion": version,
            "resolvedVersion": alias,
            "selection": "legacy-fc-alias",
        }
    implementation = resolve_operator(operator_type, version)
    return implementation, {
        "operatorType": operator_type,
        "requestedVersion": version or "",
        "resolvedVersion": implementation.operator_version,
        "selection": "exact" if version else "legacy-latest",
    }


def resolve_operator(operator_type: str, version: str | None = None) -> type[Operator]:
    """按 type[@version] 查找算子实现类；未指定版本时用该类型最新注册版本。

    指定版本但未命中时直接失败，绝不静默回退到其他版本（可复现性要求）。
    """
    _validate_operator_selection(operator_type, version)
    if version:
        cls = OPERATOR_REGISTRY.get(f"{operator_type}@{version}")
        if cls is None:
            raise OperatorResolutionError(operator_type, version)
        return cls
    cls = OPERATOR_REGISTRY.get(operator_type)
    if cls is not None:
        return cls
    raise OperatorResolutionError(operator_type, version)


def invoke(
    request: OperatorRequest | dict,
    store: ArtifactStore,
    emit: Callable[[str, dict], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    *,
    allow_legacy_fc_aliases: bool = False,
    capture_traceback: bool = False,
    traceback_source_root: str | os.PathLike | None = None,
    workspace_root: str | None = None,
) -> OperatorResult:
    """执行一次算子调用：定位实现 → 运行 → 组装 OperatorResult。

    失败被捕获为 status=failed 的结果（不抛出），符合契约的 error 结构；
    取消被捕获为 status=cancelled，由 worker 侧按运行取消处理。

    ``capture_traceback`` 供用户算子隔离进程使用：内置算子的栈信息对平台没有排障价值，
    而用户代码的报错必须让用户看得见调用栈摘要（截断后进 error.details）。
    ``traceback_source_root`` 是用户源码在沙箱里的目录；给了它，除了纯文本栈还会额外
    附一份结构化现场（``error.details.exception``），让工作台能按 ``文件:行`` 跳回编辑器。
    """
    if isinstance(request, dict):
        request = OperatorRequest.from_dict(request)

    # ctx 必须在 try 之前构造，保证异常分支（含 resolve_operator 失败）也能 emit
    # Publish only verified artifacts; operator-emitted artifact events cannot bypass registration.
    def publish(event, payload):
        if emit is not None:
            emit(event, payload)

    def guarded_emit(event, payload):
        if event not in {
            "artifact.created",
            "node.started",
            "node.completed",
            "node.failed",
            "node.cancelled",
            "node.accepted",
        } and not event.startswith("run."):
            publish(event, payload)

    before = {(meta.storagePath, meta.artifactId) for meta in store.artifacts()}
    ctx = OperatorContext(request, store, guarded_emit, cancel_check)
    result = OperatorResult(
        requestId=request.requestId,
        runId=request.runId,
        nodeId=request.nodeId,
        status="failed",
    )
    from .execution_contract import (
        ExecutionContractError,
        invocation_artifact_ports,
        validate_execution_contract,
    )

    try:
        result.executionContract = validate_execution_contract(request)
        # 执行端工作区根属于平台布局，不参与调用契约冻结；必须在校验之后注入，
        # 否则同一请求的后续校验会因 environment 与冻结摘要不一致而失败。
        if workspace_root:
            if request.environment is None:
                request.environment = {}
            request.environment.setdefault("workspaceRoot", workspace_root)
        cls, result.implementation = resolve_invocation(
            request.operatorType, request.operatorVersion, allow_legacy_fc_aliases
        )
        input_ports, output_ports, verify_content = invocation_artifact_ports(request)
        validate_inputs(request.inputs, store.root, input_ports, verify_content=verify_content)
        validate_input_sets(
            request.inputSets, store.root, input_ports, verify_content=verify_content
        )
        publish("node.started", {"nodeId": request.nodeId})
        outputs = cls().run(ctx)
        artifacts = [
            meta for meta in store.artifacts() if (meta.storagePath, meta.artifactId) not in before
        ]
        validate_outputs(
            outputs,
            artifacts,
            store.root,
            request.inputs,
            output_ports,
            verify_content=verify_content,
            input_sets=request.inputSets,
        )
        result.status = "completed"
        result.outputs = dict(outputs)
        result.artifacts = artifacts
        if emit is not None:
            for meta in artifacts:
                emit(
                    "artifact.created",
                    {
                        "nodeId": request.nodeId,
                        "name": meta.storagePath,
                        "storagePath": meta.storagePath,
                        "artifactType": meta.type,
                    },
                )
        publish("node.completed", {"nodeId": request.nodeId, "outputs": outputs})
    except ArtifactBoundaryError as exc:
        result.error = OperatorError(code=exc.code, message=str(exc), details=exc.details)
        publish(
            "node.failed",
            {"nodeId": request.nodeId, "errorCode": exc.code, "errorMessage": str(exc)},
        )
    except ExecutionContractError as exc:
        result.error = OperatorError(code=exc.code, message=str(exc), details=exc.details)
        publish(
            "node.failed",
            {"nodeId": request.nodeId, "errorCode": exc.code, "errorMessage": str(exc)},
        )
    except OperatorResolutionError as exc:
        result.error = OperatorError(code=exc.code, message=str(exc), details=exc.details)
        publish(
            "node.failed",
            {"nodeId": request.nodeId, "errorCode": exc.code, "errorMessage": str(exc)},
        )
    except OperatorCancelled as exc:
        result.status = "cancelled"
        result.error = OperatorError(code="operator.cancelled", message=str(exc))
        publish("node.failed", {"nodeId": request.nodeId, "errorMessage": str(exc)})
    except Exception as exc:  # noqa: BLE001 - 契约要求错误作为结果返回
        details: dict[str, Any] = {}
        if capture_traceback:
            raw = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            details["traceback"] = raw[-_MAX_TRACEBACK_CHARS:]
            # 纯文本栈给人眼看，结构化现场给工作台点：两者都留，缺一个都会退化成
            # 「用户只能复制日志去问人」。取结构化现场自身出错时不影响这次调用已有结果。
            try:
                details["exception"] = describe_exception(exc, traceback_source_root)
            except Exception:  # noqa: BLE001, S110 - 现场采集失败不能吃掉这次调用已有的报错
                pass
        result.error = OperatorError(code="operator.failed", message=str(exc), details=details)
        publish("node.failed", {"nodeId": request.nodeId, "errorMessage": str(exc)})
    return result
