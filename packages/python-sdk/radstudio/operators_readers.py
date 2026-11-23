"""数据资源工作台读取节点（T5/T7）：ReadImages / ReadTable / ReadSingleCell。

节点从 environment 读取服务端快照 manifest（T6 生成），把不可变输入转换为
既有管线产物：
  * ReadImages → sample_collection（与 DataInput 下游算子兼容）
  * ReadTable → table（JSON 记录）
  * ReadSingleCell → adata + metrics（与单细胞下游算子兼容）

manifest 传输约定（T7）：大清单走引用传输，environment.inputManifestRef 是
工作区相对路径（由执行端暂存），environment.snapshot 记录快照 ID 与 manifest
哈希；小测试/预览仍可直接传 environment.inputManifest 对象。

旧 DataInput/ScDataInput 保持原行为；本模块只新增，不修改既有算子。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import numpy as np

from . import data_readers as dr
from .operator import Operator, operator

# 平台绑定字段：只用于服务端解析快照，不进入读取器参数。
_PLATFORM_PARAMETER_KEYS = ("dataCollectionId", "readerConfigId")


def load_environment_manifest(environment: dict | None, root: Path | str | None = None) -> dict:
    """从 environment 解析 manifest：支持内联对象或工作区相对路径引用。"""
    env = environment or {}
    manifest = env.get("inputManifest")
    if isinstance(manifest, dict):
        return manifest
    ref = env.get("inputManifestRef")
    if not isinstance(ref, str) or not ref.strip():
        raise TypeError(
            "读取节点需要输入快照 manifest（environment.inputManifest 或 inputManifestRef）；"
            "请通过数据集合绑定运行，而不是直接执行读取节点"
        )
    base = Path(env.get("workspaceRoot") or root or ".")
    path = base / ref
    if not path.is_file():
        raise dr.ReaderError("MANIFEST_NOT_FOUND", f"输入快照 manifest 不存在: {ref}")
    raw = path.read_bytes()
    expected = str((env.get("snapshot") or {}).get("manifestSha256") or "").lower()
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise dr.ReaderError(
            "MANIFEST_HASH_MISMATCH",
            "输入快照 manifest 内容与运行绑定的哈希不符",
            suggested_action="重新准备输入快照并重新创建运行",
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise dr.ReaderError("MANIFEST_INVALID", f"输入快照 manifest 无法解析: {exc}") from exc


def _require_manifest(ctx) -> dict:
    root = getattr(getattr(ctx, "store", None), "root", None)
    manifest = load_environment_manifest(ctx.request.environment, root)
    dr.validate_manifest(manifest)
    return manifest


def _resolver_for(ctx, manifest: dict) -> dr.FileResolver:
    env = ctx.request.environment or {}
    root = env.get("workspaceRoot") or getattr(getattr(ctx, "store", None), "root", None) or "."
    return dr.make_resolver(root, verify_sha256=True)


def _config_of(manifest: dict, parameters: dict) -> dict:
    # 服务端 manifest.config 已补默认值；工作流节点参数以最新声明优先，
    # 平台绑定字段不进入读取器参数校验。
    overrides = {
        key: value
        for key, value in (parameters or {}).items()
        if key not in _PLATFORM_PARAMETER_KEYS
    }
    return {**(manifest.get("config") or {}), **overrides}


def _workspace_relative(value: str, root: str | Path | None) -> str:
    """把工作区内绝对路径改写为工作区相对路径。

    服务端每节点独立工作区：worker 仅搬运清单中按工作区相对路径引用的
    imagePath/roiPath 文件（见 服务端 samplePathFields 约定），
    直接输出绝对路径会让下游节点拿不到影像字节。
    """
    if not value or not root:
        return value
    try:
        return Path(value).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return value


@operator("ReadImages", version="1.0.0")
class ReadImagesOperator(Operator):
    """二维图片读取：快照 manifest → sample_collection。"""

    def run(self, ctx):
        manifest = _require_manifest(ctx)
        config = _config_of(manifest, ctx.parameters)
        samples = dr.read_images(manifest, config, resolve=_resolver_for(ctx, manifest))
        root = (ctx.request.environment or {}).get("workspaceRoot")
        for sample in samples:
            for key in ("imagePath", "roiPath"):
                if sample.get(key):
                    sample[key] = _workspace_relative(str(sample[key]), root)
        ctx.progress(60, f"已映射 {len(samples)} 个样本")
        return {"samples": ctx.output_json("samples.json", samples, "sample_collection")}


@operator("ReadTable", version="1.0.0")
class ReadTableOperator(Operator):
    """临床表读取：快照 manifest → table 记录。"""

    def run(self, ctx):
        manifest = _require_manifest(ctx)
        config = _config_of(manifest, ctx.parameters)
        table = dr.read_table(manifest, config, resolve=_resolver_for(ctx, manifest))
        ctx.progress(60, f"已解析 {len(table['records'])} 行记录")
        return {"records": ctx.output_json("table.json", table, "table")}


@operator("ReadSingleCell", version="1.0.0")
class ReadSingleCellOperator(Operator):
    """单细胞文库读取：快照 manifest → adata + qc_summary。"""

    def run(self, ctx):
        # 复用单细胞算子的 h5ad 落盘产物登记（[sc] extra 由 _require_* 校验）。
        from .operators_sc import _write_adata

        manifest = _require_manifest(ctx)
        config = _config_of(manifest, ctx.parameters)
        adata = dr.read_single_cell(manifest, config, resolve=_resolver_for(ctx, manifest))
        qc = {
            "libraries": sorted({str(v) for v in adata.obs["library"]}) if "library" in adata.obs else [],
            "nObs": int(adata.n_obs),
            "nVars": int(adata.n_vars),
            "sourceFormat": str(config.get("sourceFormat", "")),
        }
        ctx.progress(70, f"已拼接种细胞矩阵 {adata.n_obs}x{adata.n_vars}")
        return {
            "adata": _write_adata(ctx, adata),
            "qc_summary": ctx.output_json("qc_summary.json", qc, "metrics"),
        }


@operator("ReadSingleCell", version="1.1.0")
class ReadSingleCellOperatorV11(Operator):
    """单细胞文库读取（基因符号 var_names）：10x MEX 第二列作 var_names。

    与 1.0.0 的差别仅在 10x MEX 的基因标识：var_names 使用 genes/features.tsv
    第二列基因符号，Ensembl ID 存入 ``adata.var["gene_ids"]``；H5AD 输入不变。
    """

    def run(self, ctx):
        from .operators_sc import _write_adata

        manifest = _require_manifest(ctx)
        config = _config_of(manifest, ctx.parameters)
        adata = dr.read_single_cell(
            manifest,
            config,
            resolve=_resolver_for(ctx, manifest),
            var_names="gene_symbols",
        )
        qc = {
            "libraries": sorted({str(v) for v in adata.obs["library"]}) if "library" in adata.obs else [],
            "nObs": int(adata.n_obs),
            "nVars": int(adata.n_vars),
            "sourceFormat": str(config.get("sourceFormat", "")),
        }
        ctx.progress(70, f"已拼接种细胞矩阵 {adata.n_obs}x{adata.n_vars}")
        return {
            "adata": _write_adata(ctx, adata),
            "qc_summary": ctx.output_json("qc_summary.json", qc, "metrics"),
        }


def _safe_artifact_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("._-")
    return (cleaned or fallback)[:80]


def _write_volume_collection(ctx, collection: dict, prefix: str) -> dict:
    """把内存数组写入 .npy 产物，并在清单中登记 dataRef（不丢行）。"""
    for index, volume in enumerate(collection.get("volumes") or []):
        array = volume.pop("array", None)
        if array is None:
            raise ValueError(f"volume[{index}] 缺少内存数组，无法生成产物")
        buffer = io.BytesIO()
        np.save(buffer, np.asarray(array, dtype=np.float32))
        name = _safe_artifact_name(str(volume.get("sampleId") or ""), f"volume-{index:05d}")
        volume["dataRef"] = ctx.output_bytes(f"{prefix}/{index:05d}-{name}.npy", buffer.getvalue(), "volume")
    return collection


@operator("ReadDicom", version="1.0.0")
class ReadDicomOperator(Operator):
    """DICOM 序列读取：快照 manifest → volume_collection。"""

    def run(self, ctx):
        manifest = _require_manifest(ctx)
        config = _config_of(manifest, ctx.parameters)
        collection = dr.read_dicom(manifest, config, resolve=_resolver_for(ctx, manifest))
        collection = _write_volume_collection(ctx, collection, "volumes")
        warnings = len(collection.get("diagnostics") or [])
        ctx.progress(70, f"已读取 {len(collection['volumes'])} 个体数据（{warnings} 条警告）")
        return {"volumes": ctx.output_json("volumes.json", collection, "volume_collection")}


@operator("ReadNifti", version="1.0.0")
class ReadNiftiOperator(Operator):
    """NIfTI 读取：快照 manifest → volume_collection。"""

    def run(self, ctx):
        manifest = _require_manifest(ctx)
        config = _config_of(manifest, ctx.parameters)
        collection = dr.read_nifti(manifest, config, resolve=_resolver_for(ctx, manifest))
        collection = _write_volume_collection(ctx, collection, "volumes")
        ctx.progress(70, f"已读取 {len(collection['volumes'])} 个体数据")
        return {"volumes": ctx.output_json("volumes.json", collection, "volume_collection")}
