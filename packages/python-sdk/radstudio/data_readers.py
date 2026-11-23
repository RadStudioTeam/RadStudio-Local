"""数据资源工作台读取器（T5，docs/dataset-resource-workbench-design.md §5）。

以不可变输入 manifest（schemaVersion=1）为唯一数据来源：manifest 由服务端在
快照准备阶段生成（T6），包含行成员、文件角色与内容哈希；读取器只按用户显式
声明的配置映射与解析，不做格式猜测，不静默丢行。

manifest 结构::

    {
        "schemaVersion": "1",
        "reader": {"key": "read-images", "version": "1.0.0"},
        "collection": {"id": "...", "revision": 3},
        "config": {...},                      # 读取器参数（默认值已由服务端补齐）
        "files": {"<fileId>": {"path": "相对路径", "sha256": "...",
                               "fileName": "上传时的原始文件名（可选）"}},
        "rows": [
            {"rowId": "...", "sampleId": "s1", "fields": {...},
             "assets": [{"role": "image", "fileId": "..."}]}
        ],
    }

诊断错误（ReaderError）携带稳定 code 与 rowId/field/assetId 定位信息，
与服务端预览诊断（code/severity/rowId/field/assetVersionId/checkedRevision）对齐。
"""

from __future__ import annotations

import codecs
import csv
import gzip
import hashlib
import io
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

MANIFEST_SCHEMA_VERSION = "1"

# 与 operators.json 默认值一致（单一来源，勿单独改动）。
READER_DEFAULTS: dict[str, dict[str, Any]] = {
    "read-images": {
        "imageRole": "image",
        "roiRole": "",
        "requireRoi": False,
        "patientField": "patientId",
        "roiNameContains": "",
        "labelField": "",
        "positiveValue": "",
        "negativeValue": "",
        "classLabels": {},
    },
    "read-table": {
        "tableRole": "table",
        "delimiter": "auto",
        "encoding": "utf-8",
        "primaryKeyField": "",
        "missingValues": ["", "NA", "N/A", "null"],
        "onDuplicateKey": "error",
    },
    "read-single-cell": {
        "sourceFormat": "10x_mex",
        "matrixRole": "matrix",
        "barcodesRole": "barcodes",
        "featuresRole": "features",
        "layerSource": "X",
        "layerName": "",
        "maxCellsPerLibrary": 50000,
    },
    "read-dicom": {
        "dicomRole": "dicom",
        "seriesGrouping": "series_instance_uid",
        "requireSingleSeries": True,
        "sortBy": "image_position",
        "onMissingSlices": "error",
        "maxSlicesPerSeries": 500,
        "patientField": "patientId",
    },
    "read-nifti": {
        "niftiRole": "nifti",
        "allow4D": False,
        "volumeIndex": 0,
        "patientField": "patientId",
    },
}

# 文件解析器：file entry → 本地路径。默认实现校验 sha256（manifest 可核验）。
FileResolver = Callable[[dict[str, Any]], Path]


class ReaderError(ValueError):
    """带稳定诊断码的读取错误；不应被上游吞掉或转换成无定位的 500。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        row_id: str = "",
        field: str = "",
        asset_id: str = "",
        suggested_action: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.row_id = row_id
        self.field = field
        self.asset_id = asset_id
        self.suggested_action = suggested_action

    @property
    def diagnostic(self) -> dict[str, Any]:
        """与服务端诊断结构对齐的字典形态。"""
        payload: dict[str, Any] = {"code": self.code, "severity": "error", "message": str(self)}
        if self.row_id:
            payload["rowId"] = self.row_id
        if self.field:
            payload["field"] = self.field
        if self.asset_id:
            payload["assetId"] = self.asset_id
        if self.suggested_action:
            payload["suggestedAction"] = self.suggested_action
        return payload


def merged_config(reader_key: str, config: dict[str, Any] | None) -> dict[str, Any]:
    """合并默认值；未知参数拒绝（与服务端 schema 校验同口径）。"""
    defaults = READER_DEFAULTS[reader_key]
    merged = dict(defaults)
    for key, value in (config or {}).items():
        if key not in defaults:
            raise ReaderError("CONFIG_INVALID", f"未知读取参数: {key}")
        merged[key] = value
    return merged


def validate_manifest(manifest: dict[str, Any]) -> None:
    """manifest 结构校验；快照必须自描述且可核验。"""
    if not isinstance(manifest, dict):
        raise ReaderError("MANIFEST_INVALID", "manifest 必须是对象")
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        raise ReaderError(
            "MANIFEST_INVALID",
            f"schemaVersion 不支持: {manifest.get('schemaVersion')!r}",
        )
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ReaderError("MANIFEST_INVALID", "manifest.files 缺失")
    rows = manifest.get("rows")
    if not isinstance(rows, list):
        raise ReaderError("MANIFEST_INVALID", "manifest.rows 缺失")
    for row in rows:
        if not isinstance(row, dict) or not row.get("rowId"):
            raise ReaderError("MANIFEST_INVALID", "manifest.rows 元素缺少 rowId")
        for asset in row.get("assets", []) or []:
            if asset.get("fileId") not in files:
                raise ReaderError(
                    "MANIFEST_INVALID",
                    f"行 {row.get('rowId')} 引用了不存在的 fileId {asset.get('fileId')!r}",
                    row_id=str(row.get("rowId")),
                )
    entries = manifest.get("entries")
    if entries is not None:
        if not isinstance(entries, list):
            raise ReaderError("MANIFEST_INVALID", "manifest.entries 必须是列表")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ReaderError("MANIFEST_INVALID", "manifest.entries 元素必须是对象")
            if str(entry.get("assetVersionId") or "") not in files:
                raise ReaderError(
                    "MANIFEST_INVALID",
                    f"资源条目 {entry.get('relativePath') or entry.get('assetVersionId')!r} 没有对应的文件记录",
                )


def make_resolver(root: Path | str | None = None, verify_sha256: bool = True) -> FileResolver:
    """构造文件解析器：file entry → 本地路径（可选 sha256 校验）。"""

    def resolve(entry: dict[str, Any]) -> Path:
        raw = entry.get("path")
        if not raw:
            raise ReaderError("MANIFEST_INVALID", "文件条目缺少 path")
        path = Path(raw)
        if root is not None and not path.is_absolute():
            path = Path(root) / path
        if not path.is_file():
            raise ReaderError("FILE_NOT_FOUND", f"文件不存在: {raw}")
        if verify_sha256:
            expected = str(entry.get("sha256") or "").lower()
            if expected:
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise ReaderError(
                        "FILE_HASH_MISMATCH",
                        f"文件 {raw} 内容与 manifest 哈希不符",
                        suggested_action="重新上传该文件或重新生成快照",
                    )
        return path

    return resolve


def _row_assets_by_role(row: dict[str, Any]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for asset in row.get("assets") or []:
        grouped.setdefault(str(asset.get("role")), []).append(str(asset.get("fileId")))
    return grouped


def _files_of(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return manifest.get("files") or {}


# 资源快照（目录/文件选择）没有样本表，读取器按文件清单合成行。
# 无法归属到本读取器角色的文件按扩展名忽略（同一目录可同时绑定表格与影像节点）；
# 认不出归属又可能承载数据的扩展名直接报错，避免静默丢输入。
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".npy"}
_TABLE_SUFFIXES = {".csv", ".tsv", ".txt"}
_SINGLE_CELL_SUFFIXES = {".h5ad", ".mtx", ".tsv", ".gz", ".txt"}


def _resource_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    source = manifest.get("source") or {}
    if str(source.get("kind") or "") != "resources":
        return []
    entries = manifest.get("entries") or []
    return [entry for entry in entries if isinstance(entry, dict)]


def _relative_path(entry: dict[str, Any]) -> str:
    rel = str(entry.get("relativePath") or "").strip().strip("/")
    return rel or str(entry.get("name") or entry.get("assetVersionId") or "")


def _parent_path(rel: str) -> str:
    parent = PurePosixPath(rel).parent.as_posix()
    return "" if parent in ("", ".") else parent


def _top_directory(rel: str) -> str:
    parts = PurePosixPath(rel).parts
    return parts[0] if len(parts) > 1 else ""


def _sample_id_of(rel: str, entry: dict[str, Any]) -> str:
    """资源样本标识：相对路径去扩展名，保证同一目录内唯一且可回溯。"""
    if rel:
        return PurePosixPath(rel).with_suffix("").as_posix()
    return str(entry.get("assetVersionId") or "")


def _resource_row(
    entry: dict[str, Any],
    rel: str,
    assets: list[tuple[str, dict[str, Any]]],
    *,
    fields: dict[str, Any] | None = None,
    sample_id: str = "",
) -> dict[str, Any]:
    return {
        "rowId": str(entry.get("assetVersionId") or rel),
        "sampleId": sample_id or _sample_id_of(rel, entry),
        "fields": fields or {},
        "assets": [{"role": role, "fileId": str(item.get("assetVersionId"))} for role, item in assets],
    }


def _resource_rows_images(entries: list[dict[str, Any]], merged: dict[str, Any]) -> list[dict[str, Any]]:
    """二维图片：每张图一行；掩膜按 roiNameContains 标记与同目录同主名配对。"""
    image_role = str(merged["imageRole"])
    roi_role = str(merged["roiRole"])
    marker = str(merged.get("roiNameContains") or "")
    if roi_role and not marker:
        raise ReaderError(
            "CONFIG_INVALID",
            "资源模式下 roiRole 需要同时设置 roiNameContains（如 _mask）才能配对掩膜",
            field="roiNameContains",
            suggested_action="设置掩膜文件名的共同片段，或改用集合/样本表绑定明确角色",
        )
    masks: dict[tuple[str, str], dict[str, Any]] = {}
    images: list[tuple[dict[str, Any], str, str]] = []
    for entry in entries:
        rel = _relative_path(entry)
        name = PurePosixPath(rel).name
        suffix = _uncompressed_suffix(name)
        if roi_role and marker and marker in name:
            masks[(_parent_path(rel), name.replace(marker, "", 1))] = entry
            continue
        if suffix in _IMAGE_SUFFIXES:
            images.append((entry, rel, name))
    if not images:
        raise ReaderError(
            "RESOURCE_NO_IMAGES",
            "所选资源中没有二维图片（支持 PNG/JPEG/TIFF/BMP/NPY）",
            suggested_action="检查目录或过滤条件，或改用集合/样本表绑定",
        )
    rows: list[dict[str, Any]] = []
    for entry, rel, name in images:
        sample_id = _sample_id_of(rel, entry)
        top = _top_directory(rel)
        # 资源目录没有样本表：主目录名承载标签与患者分组，字段名与集合列保持一致。
        fields: dict[str, Any] = {
            "relativePath": rel,
            "fileName": name,
            "directory": _parent_path(rel),
        }
        if top:
            fields["labelValue"] = top
            fields["patientId"] = top
        assets = [(image_role, entry)]
        if roi_role:
            mask = masks.get((_parent_path(rel), name))
            if mask is not None:
                assets.append((roi_role, mask))
            elif merged["requireRoi"]:
                raise ReaderError(
                    "ROW_MISSING_ROLE",
                    f"样本 {sample_id} 缺少掩膜（匹配标记 {marker!r}）",
                    row_id=sample_id,
                    field=roi_role,
                    suggested_action=f"提供同目录同主名且文件名含 {marker!r} 的掩膜",
                )
        rows.append(_resource_row(entry, rel, assets, fields=fields, sample_id=sample_id))
    return rows


def _resource_rows_table(entries: list[dict[str, Any]], merged: dict[str, Any]) -> list[dict[str, Any]]:
    table_role = str(merged["tableRole"])
    tables = [
        (entry, _relative_path(entry))
        for entry in entries
        if _uncompressed_suffix(PurePosixPath(_relative_path(entry)).name) in _TABLE_SUFFIXES
    ]
    if not tables:
        raise ReaderError(
            "RESOURCE_NO_TABLES",
            "所选资源中没有临床表文件（CSV/TSV）",
            suggested_action="检查目录或过滤条件，或改用集合/样本表绑定",
        )
    return [_resource_row(entry, rel, [(table_role, entry)]) for entry, rel in tables]


def _resource_rows_single_cell(entries: list[dict[str, Any]], merged: dict[str, Any]) -> list[dict[str, Any]]:
    """单细胞：每个目录/每个 h5ad 文件一个文库，角色按标准文件名识别。"""
    matrix_role = str(merged["matrixRole"])
    barcodes_role = str(merged["barcodesRole"])
    features_role = str(merged["featuresRole"])
    source_format = str(merged["sourceFormat"])
    groups: dict[str, dict[str, Any]] = {}
    for entry in entries:
        rel = _relative_path(entry)
        name = PurePosixPath(rel).name.lower()
        parent = _parent_path(rel)
        suffix = _uncompressed_suffix(name)
        if source_format == "h5ad" and suffix == ".h5ad":
            key = parent or PurePosixPath(rel).with_suffix("").as_posix()
            group = groups.setdefault(key, {"entry": entry, "assets": []})
            group["assets"] = [(matrix_role, entry)]
            continue
        if source_format != "10x_mex" or suffix not in _SINGLE_CELL_SUFFIXES:
            continue
        if "matrix" in name:
            role = matrix_role
        elif "barcode" in name:
            role = barcodes_role
        elif "feature" in name or "gene" in name:
            role = features_role
        else:
            continue
        key = parent or PurePosixPath(rel).with_suffix("").as_posix()
        group = groups.setdefault(key, {"entry": entry, "assets": []})
        group["assets"].append((role, entry))
    if not groups:
        raise ReaderError(
            "RESOURCE_NO_LIBRARIES",
            "所选资源中没有可识别的单细胞文库（10x MEX 三件套或 H5AD）",
            suggested_action="检查 sourceFormat 与目录结构，或改用集合/样本表绑定",
        )
    rows: list[dict[str, Any]] = []
    for key, group in groups.items():
        entry = group["entry"]
        sample_id = PurePosixPath(key).name or key
        rows.append(_resource_row(entry, key, group["assets"], sample_id=sample_id))
    return rows


def _resource_rows_dicom(entries: list[dict[str, Any]], merged: dict[str, Any]) -> list[dict[str, Any]]:
    role = str(merged["dicomRole"])
    if not entries:
        raise ReaderError("RESOURCE_NO_SERIES", "所选资源中没有 DICOM 文件")
    # 资源选择没有样本表：整份选择视为同一序列（requireSingleSeries 再行核对）。
    head = entries[0]
    rel = _relative_path(head)
    sample_id = PurePosixPath(_parent_path(rel)).name or PurePosixPath(rel).with_suffix("").as_posix()
    return [_resource_row(head, rel, [(role, entry) for entry in entries], sample_id=sample_id)]


def _resource_rows_nifti(entries: list[dict[str, Any]], merged: dict[str, Any]) -> list[dict[str, Any]]:
    role = str(merged["niftiRole"])
    volumes = [
        (entry, _relative_path(entry))
        for entry in entries
        if PurePosixPath(_relative_path(entry)).name.lower().endswith((".nii", ".nii.gz"))
    ]
    if not volumes:
        raise ReaderError(
            "RESOURCE_NO_VOLUMES",
            "所选资源中没有 NIfTI 文件（.nii/.nii.gz）",
            suggested_action="检查目录或过滤条件，或改用集合/样本表绑定",
        )
    return [_resource_row(entry, rel, [(role, entry)]) for entry, rel in volumes]


_RESOURCE_ROW_BUILDERS = {
    "read-images": _resource_rows_images,
    "read-table": _resource_rows_table,
    "read-single-cell": _resource_rows_single_cell,
    "read-dicom": _resource_rows_dicom,
    "read-nifti": _resource_rows_nifti,
}


def manifest_rows(
    manifest: dict[str, Any], reader_key: str, config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """读取器实际使用行：集合快照直接用 rows；资源快照按文件清单合成。

    资源快照不携带样本表，行由读取器按角色/命名约定从 entries 合成；两种来源
    共用同一套文件解析与内容校验，下游算子无需区分。
    """
    rows = manifest.get("rows") or []
    if rows:
        return rows
    entries = _resource_entries(manifest)
    if not entries:
        return []
    merged = merged_config(reader_key, config)
    builder = _RESOURCE_ROW_BUILDERS[reader_key]
    return builder(entries, merged)


def _is_gzip(path: Path) -> bool:
    """按魔数判断 gzip：快照对象键是内容寻址的，不能依赖扩展名。"""
    with path.open("rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


def _display_name(entry: dict[str, Any], path: Path) -> str:
    """用户可见文件名：manifest.fileName 优先（对象键没有扩展名）。"""
    return str(entry.get("fileName") or path.name)


def _uncompressed_suffix(name: str) -> str:
    """去掉 gzip 外壳后的扩展名（``table.csv.gz`` → ``.csv``）。"""
    lower = name.lower()
    return Path(lower[:-3]).suffix if lower.endswith(".gz") else Path(lower).suffix


def read_images(
    manifest: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    resolve: FileResolver | None = None,
) -> list[dict[str, Any]]:
    """二维图片读取：行 → sample_collection 样本（imageId/patientId/imagePath/roiPath）。

    sample_collection 每个样本只表达一张图：多图行不可表达，显式报错
    （预览允许聚合检查，执行端不做静默拆分）。
    """
    validate_manifest(manifest)
    merged = merged_config("read-images", config)
    resolve = resolve or make_resolver(None)
    files = _files_of(manifest)
    image_role = str(merged["imageRole"])
    roi_role = str(merged["roiRole"])
    patient_field = str(merged["patientField"])
    labeler = _label_mapper(merged)

    samples: list[dict[str, Any]] = []
    for row in manifest_rows(manifest, "read-images", merged):
        row_id = str(row.get("rowId"))
        assets = _row_assets_by_role(row)
        image_ids = assets.get(image_role, [])
        if not image_ids:
            raise ReaderError(
                "ROW_MISSING_ROLE",
                f"行 {row.get('sampleId') or row_id} 缺少角色 {image_role!r} 的文件",
                row_id=row_id,
                field=image_role,
                suggested_action="在样本表该行的文件单元格中绑定图片",
            )
        if len(image_ids) > 1:
            raise ReaderError(
                "ROW_ROLE_OVERFLOW",
                f"行 {row.get('sampleId') or row_id} 有 {len(image_ids)} 张图片，sample_collection 每样本只能一张；请拆分行或改用 DICOM 读取器（未交付）",
                row_id=row_id,
                field=image_role,
                suggested_action="每行只保留一张图片，其余拆分为新行",
            )
        image_path = resolve(files[image_ids[0]])

        roi_path = ""
        if roi_role:
            roi_ids = assets.get(roi_role, [])
            if len(roi_ids) > 1:
                raise ReaderError(
                    "ROW_ROLE_OVERFLOW",
                    f"行 {row.get('sampleId') or row_id} 的 ROI 文件多于一个",
                    row_id=row_id,
                    field=roi_role,
                )
            if roi_ids:
                roi_path = str(resolve(files[roi_ids[0]]))
            elif merged["requireRoi"]:
                raise ReaderError(
                    "ROW_MISSING_ROLE",
                    f"行 {row.get('sampleId') or row_id} 缺少 ROI（requireRoi=true）",
                    row_id=row_id,
                    field=roi_role,
                )

        sample_id = str(row.get("sampleId") or row_id)
        fields = row.get("fields") or {}
        patient = fields.get(patient_field) if patient_field else None
        samples.append(
            {
                "imageId": sample_id,
                "patientId": str(patient) if patient not in (None, "") else sample_id,
                "label": labeler(fields) if labeler else None,
                "imagePath": str(image_path),
                "roiPath": roi_path,
            }
        )
    return samples


def _label_mapper(merged: dict[str, Any]):
    """按读取参数生成标签映射函数；未声明 labelField 时不映射（label=null）。"""
    label_field = str(merged.get("labelField") or "")
    if not label_field:
        return None
    classes = merged.get("classLabels") or {}
    if classes:
        if not isinstance(classes, dict):
            raise ReaderError("CONFIG_INVALID", "classLabels 必须是对象")
        mapping: dict[str, int] = {}
        for key, value in classes.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ReaderError("CONFIG_INVALID", f"classLabels[{key!r}] 必须是非负整数")
            mapping[str(key)] = value
        if sorted(mapping.values()) != list(range(len(mapping))):
            raise ReaderError("CONFIG_INVALID", "classLabels 的取值必须是 0..n-1 连续整数")
        return lambda fields: mapping.get(str(fields.get(label_field, "")))
    positive = str(merged.get("positiveValue") or "")
    negative = str(merged.get("negativeValue") or "")
    if positive == negative:
        raise ReaderError(
            "CONFIG_INVALID",
            "设置 labelField 时必须给出不同的 positiveValue/negativeValue，或提供 classLabels",
            field="positiveValue",
        )
    return lambda fields: (
        1
        if str(fields.get(label_field, "")) == positive
        else 0
        if str(fields.get(label_field, "")) == negative
        else None
    )


def _decode_text_head(head: bytes, encoding: str) -> str:
    """按声明编码解码（头部字节已足够判断表头；尾部截断序列可忽略）。"""
    if encoding == "utf-8-sig":
        head = head.lstrip(codecs.BOM_UTF8)
        encoding = "utf-8"
    try:
        return head.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise ReaderError(
            "CSV_ENCODING_INVALID",
            f"文件头无法按 {encoding} 解码: {exc}",
            suggested_action="确认文件编码（支持 utf-8/utf-8-sig/gbk）或重新导出",
        ) from exc


def _detect_delimiter(header_line: str) -> str:
    counts = {",": 0, "\t": 0, ";": 0}
    for ch in header_line:
        if ch in counts:
            counts[ch] += 1
    return max(counts, key=lambda key: counts[key])


def _open_text(path: Path, encoding: str):
    # 返回未关闭的 TextIOWrapper；调用方统一 with 管理（SIM115 抑制）。
    raw = gzip.open(path, "rb") if _is_gzip(path) else path.open("rb")  # noqa: SIM115
    # utf-8-sig 先剥 BOM；codecs 流式解码器自己处理其余部分。
    if encoding == "utf-8-sig":
        return io.TextIOWrapper(raw, encoding="utf-8-sig")
    return io.TextIOWrapper(raw, encoding=encoding, errors="strict")


def read_table(
    manifest: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    resolve: FileResolver | None = None,
) -> dict[str, Any]:
    """临床表读取：返回 {"columns": [...], "records": [...]}。

    重复主键、空主键、缺主键列都显式报错；missingValues 归一为 None，
    不静默转换类型。
    """
    validate_manifest(manifest)
    merged = merged_config("read-table", config)
    resolve = resolve or make_resolver(None)
    files = _files_of(manifest)
    table_role = str(merged["tableRole"])
    encoding = str(merged["encoding"])
    if encoding not in ("utf-8", "utf-8-sig", "gbk"):
        raise ReaderError("CONFIG_INVALID", f"不支持的编码: {encoding}")
    missing = {str(value) for value in (merged["missingValues"] or [])}
    on_duplicate = str(merged["onDuplicateKey"])
    if on_duplicate not in ("error", "keep_first"):
        raise ReaderError("CONFIG_INVALID", f"onDuplicateKey 不支持: {on_duplicate}")
    primary_key = str(merged["primaryKeyField"] or "")

    columns: list[str] = []
    records: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for row in manifest_rows(manifest, "read-table", merged):
        row_id = str(row.get("rowId"))
        assets = _row_assets_by_role(row)
        table_ids = assets.get(table_role, [])
        if len(table_ids) != 1:
            raise ReaderError(
                "ROW_MISSING_ROLE" if not table_ids else "ROW_ROLE_OVERFLOW",
                f"行 {row.get('sampleId') or row_id} 需要恰好一个 {table_role!r} 文件，实际 {len(table_ids)} 个",
                row_id=row_id,
                field=table_role,
            )
        path = resolve(files[table_ids[0]])
        display = _display_name(files[table_ids[0]], path)
        if _uncompressed_suffix(display) not in (".csv", ".tsv", ".txt"):
            raise ReaderError(
                "FILE_EXTENSION_UNSUPPORTED",
                f"文件 {display} 不是受支持的临床表格式（CSV/TSV）",
                row_id=row_id,
                field=table_role,
            )

        with _open_text(path, encoding) as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            if not any((name or "").strip() for name in header):
                raise ReaderError(
                    "CSV_HEADER_INVALID",
                    f"文件 {path.name} 表头为空",
                    row_id=row_id,
                    field=table_role,
                )
            if not columns:
                columns = [str(name) for name in header]
            elif [str(name) for name in header] != columns:
                raise ReaderError(
                    "CSV_HEADER_INVALID",
                    f"文件 {path.name} 表头与首个文件不一致: {header} vs {columns}",
                    row_id=row_id,
                    field=table_role,
                    suggested_action="多个表文件必须使用相同列结构",
                )
            if primary_key and primary_key not in columns:
                raise ReaderError(
                    "CSV_PRIMARY_KEY_MISSING",
                    f"表头缺少声明的主键列 {primary_key!r}（现有列：{columns}）",
                    row_id=row_id,
                    field=primary_key,
                )
            for index, record in enumerate(reader, start=2):  # 数据行从第 2 行计
                normalized: dict[str, Any] = {}
                for name in columns:
                    value = record.get(name)
                    if value is None or str(value) in missing:
                        normalized[name] = None
                    else:
                        normalized[name] = value
                if primary_key:
                    key_value = normalized.get(primary_key)
                    if key_value is None:
                        raise ReaderError(
                            "CSV_PRIMARY_KEY_EMPTY",
                            f"文件 {path.name} 第 {index} 行主键 {primary_key!r} 为空或缺失值",
                            row_id=row_id,
                            field=primary_key,
                            suggested_action="补齐主键或在样本表中移除该行",
                        )
                    key_str = str(key_value)
                    if key_str in seen_keys:
                        if on_duplicate == "error":
                            raise ReaderError(
                                "CSV_DUPLICATE_PRIMARY_KEY",
                                f"主键 {key_str!r} 在 {path.name} 第 {index} 行重复出现",
                                row_id=row_id,
                                field=primary_key,
                                suggested_action="修正重复主键或将 onDuplicateKey 设为 keep_first",
                            )
                        continue
                    seen_keys.add(key_str)
                records.append(normalized)

    if primary_key and not columns:
        raise ReaderError("MANIFEST_INVALID", "集合没有任何表行")
    return {"columns": columns, "records": records}


def _require_scipy():
    try:
        from scipy import io as scipy_io
        from scipy import sparse as scipy_sparse

        return scipy_io, scipy_sparse
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise ImportError("单细胞读取器需要 [sc] extra：uv sync --extra sc") from exc


def _require_anndata():
    try:
        import anndata

        return anndata
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise ImportError("单细胞读取器需要 [sc] extra：uv sync --extra sc") from exc


def _read_lines(path: Path) -> list[str]:
    """读取每行首列（barcodes/features 第一列），跳过表头注释与空行。"""
    opener = gzip.open if _is_gzip(path) else open
    values: list[str] = []
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n").rstrip("\r")
            if not line or line.startswith(("#", "%%")):
                continue
            first = line.split("\t")[0]
            if first:
                values.append(first)
    return values


def _read_feature_table(path: Path) -> list[tuple[str, str]]:
    """读取 features/genes.tsv：第一列基因 ID，第二列基因符号（缺失时回落 ID）。"""
    opener = gzip.open if _is_gzip(path) else open
    rows: list[tuple[str, str]] = []
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n").rstrip("\r")
            if not line or line.startswith(("#", "%%")):
                continue
            parts = line.split("\t")
            gene_id = parts[0].strip()
            if not gene_id:
                continue
            symbol = parts[1].strip() if len(parts) > 1 and parts[1].strip() else gene_id
            rows.append((gene_id, symbol))
    return rows


def _make_unique(names: list[str]) -> list[str]:
    """同 scanpy make_unique=True：重复名追加 -1/-2 后缀，保持首次出现顺序。"""
    used: set[str] = set()
    counters: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        if name not in used:
            used.add(name)
            result.append(name)
            continue
        index = counters.get(name, 1)
        candidate = f"{name}-{index}"
        while candidate in used:
            index += 1
            candidate = f"{name}-{index}"
        counters[name] = index + 1
        used.add(candidate)
        result.append(candidate)
    return result


def _read_10x_library(
    matrix_path: Path,
    barcodes_path: Path,
    features_path: Path,
    row_id: str,
    sample_id: str,
    max_cells: int,
    var_names: str = "gene_ids",
):
    """读取单个 10x MEX 文库 → AnnData（cells × genes，方向显式核验）。

    var_names="gene_ids"（旧行为）取 features 第一列作 var_names；
    var_names="gene_symbols" 取第二列作 var_names，并把第一列存入 var.gene_ids。
    """
    if var_names not in ("gene_ids", "gene_symbols"):
        raise ValueError("var_names 仅支持 gene_ids 或 gene_symbols")
    anndata = _require_anndata()
    scipy_io, scipy_sparse = _require_scipy()

    barcodes = _read_lines(barcodes_path)
    gene_ids: list[str] | None = None
    if var_names == "gene_symbols":
        feature_rows = _read_feature_table(features_path)
        gene_ids = [gene_id for gene_id, _symbol in feature_rows]
        features = _make_unique([symbol for _gene_id, symbol in feature_rows])
    else:
        features = _read_lines(features_path)
    with gzip.open(matrix_path, "rb") if _is_gzip(matrix_path) else matrix_path.open("rb") as handle:
        matrix = scipy_io.mmread(handle)
    # MEX 是 features × cells；AnnData 约定 cells × genes。按实际形状判定方向，
    # 不一致（既不是 (n_features, n_cells) 也不是 (n_cells, n_features)）报错定位文库。
    n_cells, n_features = len(barcodes), len(features)
    if matrix.shape == (n_features, n_cells):
        matrix = matrix.T
    elif matrix.shape != (n_cells, n_features):
        raise ReaderError(
            "SC_DIMENSION_MISMATCH",
            f"文库 {sample_id} 矩阵形状 {matrix.shape} 与 barcodes({n_cells})/features({n_features}) 不符",
            row_id=row_id,
            field="matrix",
            suggested_action="确认三件套来自同一文库且未混用旧版/新版 MEX",
        )
    if isinstance(matrix, np.ndarray):
        matrix = scipy_sparse.csr_matrix(matrix)
    else:
        matrix = matrix.tocsr()
    if n_cells > max_cells:
        raise ReaderError(
            "SC_LIBRARY_TOO_LARGE",
            f"文库 {sample_id} 细胞数 {n_cells} 超过 maxCellsPerLibrary={max_cells}",
            row_id=row_id,
            field="matrix",
            suggested_action="按文库拆分集合或调大 maxCellsPerLibrary",
        )
    var = {"gene_ids": gene_ids} if gene_ids is not None else None
    adata = anndata.AnnData(
        X=matrix.tocsr(),
        obs={"library": [sample_id] * n_cells},
        var=var,
    )
    adata.obs_names = barcodes
    adata.var_names = features
    return adata


def _apply_layer_selection(adata, merged: dict[str, Any], row_id: str, sample_id: str):
    """显式矩阵选择：X/raw/layers[:name]，无静默默认。"""
    source = str(merged["layerSource"])
    if source == "X":
        return adata
    if source == "raw":
        if adata.raw is None:
            raise ReaderError(
                "SC_LAYER_MISSING",
                f"文库 {sample_id} 没有 raw 层（layerSource=raw）",
                row_id=row_id,
                field="raw",
                suggested_action="把 layerSource 改为 X/layer，或选择包含 raw 的 H5AD",
            )
        return adata.raw.to_adata()
    # layer
    name = str(merged["layerName"] or "")
    if not name:
        raise ReaderError(
            "CONFIG_INVALID",
            "layerSource=layer 时必须提供 layerName",
            row_id=row_id,
        )
    if name not in adata.layers:
        # 某些 anndata 版本 round-trip 后 layers 会带 None 伪键，展示时归一为字符串。
        existing = sorted(str(key) for key in adata.layers)
        raise ReaderError(
            "SC_LAYER_MISSING",
            f"文库 {sample_id} 缺少 layer {name!r}（现有：{existing}）",
            row_id=row_id,
            field=f"layers.{name}",
        )
    import anndata as _anndata

    selected = _anndata.AnnData(X=adata.layers[name].copy(), obs=adata.obs.copy(), var=adata.var.copy())
    selected.obs_names = adata.obs_names
    selected.var_names = adata.var_names
    return selected


def read_single_cell(
    manifest: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    resolve: FileResolver | None = None,
    var_names: str = "gene_ids",
):
    """单细胞读取：10x MEX 三件套或 H5AD → 拼接 AnnData。

    每行一个文库；缺件定位到行与角色，维度不一致显式报错。
    var_names="gene_symbols" 时 10x MEX 使用第二列基因符号作 var_names，
    Ensembl ID 存入 var.gene_ids（H5AD 保持原有 var_names）。
    """
    validate_manifest(manifest)
    merged = merged_config("read-single-cell", config)
    resolve = resolve or make_resolver(None)
    files = _files_of(manifest)
    source_format = str(merged["sourceFormat"])
    if source_format not in ("10x_mex", "h5ad"):
        raise ReaderError("CONFIG_INVALID", f"sourceFormat 不支持: {source_format}")
    matrix_role = str(merged["matrixRole"])
    max_cells = int(merged["maxCellsPerLibrary"])

    libraries = []
    for row in manifest_rows(manifest, "read-single-cell", merged):
        row_id = str(row.get("rowId"))
        sample_id = str(row.get("sampleId") or row_id)
        assets = _row_assets_by_role(row)

        matrix_ids = assets.get(matrix_role, [])
        if len(matrix_ids) != 1:
            raise ReaderError(
                "ROW_MISSING_ROLE" if not matrix_ids else "ROW_ROLE_OVERFLOW",
                f"文库 {sample_id} 需要恰好一个 {matrix_role!r} 文件，实际 {len(matrix_ids)} 个",
                row_id=row_id,
                field=matrix_role,
            )
        matrix_path = resolve(files[matrix_ids[0]])
        matrix_name = _display_name(files[matrix_ids[0]], matrix_path)

        if source_format == "h5ad":
            if _uncompressed_suffix(matrix_name) != ".h5ad":
                raise ReaderError(
                    "FILE_EXTENSION_UNSUPPORTED",
                    f"sourceFormat=h5ad 时矩阵必须是 .h5ad，得到 {matrix_name}",
                    row_id=row_id,
                    field=matrix_role,
                )
            anndata = _require_anndata()
            adata = anndata.io.read_h5ad(matrix_path)
            adata = _apply_layer_selection(adata, merged, row_id, sample_id)
            if adata.n_obs > max_cells:
                raise ReaderError(
                    "SC_LIBRARY_TOO_LARGE",
                    f"文库 {sample_id} 细胞数 {adata.n_obs} 超过 maxCellsPerLibrary={max_cells}",
                    row_id=row_id,
                    field="matrix",
                )
            libraries.append(adata)
            continue

        # 10x_mex：三件套缺一即报错，定位行与角色。
        barcodes_path = None
        features_path = None
        barcodes_name = ""
        features_name = ""
        for role, attr in (("barcodesRole", "barcodes"), ("featuresRole", "features")):
            role_name = str(merged[role])
            ids = assets.get(role_name, [])
            if len(ids) != 1:
                raise ReaderError(
                    "ROW_MISSING_ROLE" if not ids else "ROW_ROLE_OVERFLOW",
                    f"文库 {sample_id} 需要恰好一个 {role_name!r} 文件，实际 {len(ids)} 个",
                    row_id=row_id,
                    field=role_name,
                    suggested_action="上传 10x 标准命名三件套（matrix/barcodes/features）",
                )
            if attr == "barcodes":
                barcodes_path = resolve(files[ids[0]])
                barcodes_name = _display_name(files[ids[0]], barcodes_path)
            else:
                features_path = resolve(files[ids[0]])
                features_name = _display_name(files[ids[0]], features_path)
        for label, name in (("barcodes", barcodes_name), ("features", features_name)):
            if _uncompressed_suffix(name) != ".tsv":
                raise ReaderError(
                    "FILE_EXTENSION_UNSUPPORTED",
                    f"文库 {sample_id} 的 {label} 文件 {name} 不是 .tsv(.gz)",
                    row_id=row_id,
                    field=label,
                )
        libraries.append(
            _read_10x_library(
                matrix_path,
                barcodes_path,
                features_path,
                row_id,
                sample_id,
                max_cells,
                var_names=var_names,
            )
        )

    if not libraries:
        raise ReaderError("MANIFEST_INVALID", "集合没有任何文库行")
    anndata = _require_anndata()
    if len(libraries) == 1:
        return libraries[0]
    return anndata.concat(libraries, axis=0, join="outer")


def _patient_id(row: dict[str, Any], patient_field: str, sample_id: str) -> str:
    fields = row.get("fields") or {}
    patient = fields.get(patient_field) if patient_field else None
    return str(patient) if patient not in (None, "") else sample_id


def _require_reader_key(manifest: dict[str, Any], expected: str) -> None:
    """快照声明的读取器必须与节点一致，避免用错读取器解释文件。"""
    declared = str((manifest.get("reader") or {}).get("key") or expected)
    if declared != expected:
        raise ReaderError(
            "READER_KEY_MISMATCH",
            f"输入快照由 {declared} 生成，不能用于 {expected} 读取节点",
            suggested_action="在读取节点重新绑定匹配的读取配置并重新准备快照",
        )


def read_dicom(
    manifest: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    resolve: FileResolver | None = None,
) -> dict[str, Any]:
    """DICOM 序列读取：行 → volume_collection（体数据 entries）。

    返回 ``{"volumes": [...], "diagnostics": [...]}``；每个 volume 的 ``array``
    为内存中的 numpy 数组，由执行端写入 .npy 产物。序列/几何/缺片策略错误
    直接抛出 ReaderError，不静默丢行。
    """
    from . import imaging  # 避免 imaging → data_readers 的循环导入

    validate_manifest(manifest)
    _require_reader_key(manifest, "read-dicom")
    merged = merged_config("read-dicom", config)
    resolve = resolve or make_resolver(None)
    files = _files_of(manifest)
    role = str(merged["dicomRole"])
    series_grouping = str(merged["seriesGrouping"])
    if series_grouping not in ("series_instance_uid", "study_instance_uid"):
        raise ReaderError("CONFIG_INVALID", f"seriesGrouping 不支持: {series_grouping}")
    sort_by = str(merged["sortBy"])
    if sort_by not in ("image_position", "instance_number"):
        raise ReaderError("CONFIG_INVALID", f"sortBy 不支持: {sort_by}")
    on_missing = str(merged["onMissingSlices"])
    if on_missing not in ("error", "report"):
        raise ReaderError("CONFIG_INVALID", f"onMissingSlices 不支持: {on_missing}")
    max_slices = int(merged["maxSlicesPerSeries"])
    if max_slices < 1 or max_slices > 2000:
        raise ReaderError("CONFIG_INVALID", "maxSlicesPerSeries 必须在 1..2000")
    patient_field = str(merged["patientField"])

    volumes: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for row in manifest_rows(manifest, "read-dicom", merged):
        row_id = str(row.get("rowId"))
        sample_id = str(row.get("sampleId") or row_id)
        assets = _row_assets_by_role(row)
        dicom_ids = assets.get(role, [])
        if not dicom_ids:
            raise ReaderError(
                "ROW_MISSING_ROLE",
                f"样本 {sample_id} 缺少角色 {role!r} 的 DICOM 切片",
                row_id=row_id,
                field=role,
                suggested_action="在样本表该行绑定同一序列的 DICOM 切片",
            )
        paths = [resolve(files[file_id]) for file_id in dicom_ids]
        names = [
            str(files[file_id].get("fileName") or "") for file_id in dicom_ids
        ]
        result = imaging.load_dicom_series(
            paths,
            row_id=row_id,
            sample_id=sample_id,
            require_single_series=bool(merged["requireSingleSeries"]),
            sort_by=sort_by,
            on_missing_slices=on_missing,
            asset_ids=dicom_ids,
            names=names,
        )
        diagnostics.extend(result.warnings)
        volumes.append(
            imaging.build_dicom_volume(
                result,
                sample_id=sample_id,
                patient_id=_patient_id(row, patient_field, sample_id),
                max_slices=max_slices,
            )
        )
    if not volumes:
        raise ReaderError("MANIFEST_INVALID", "集合没有任何 DICOM 行")
    return {
        "schemaVersion": "1",
        "reader": {"key": "read-dicom", "version": "1.0.0"},
        "volumes": volumes,
        "diagnostics": diagnostics,
    }


def read_nifti(
    manifest: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    resolve: FileResolver | None = None,
) -> dict[str, Any]:
    """NIfTI 读取：每行恰好一个 .nii/.nii.gz → volume_collection。"""
    from . import imaging

    validate_manifest(manifest)
    _require_reader_key(manifest, "read-nifti")
    merged = merged_config("read-nifti", config)
    resolve = resolve or make_resolver(None)
    files = _files_of(manifest)
    role = str(merged["niftiRole"])
    allow_4d = bool(merged["allow4D"])
    volume_index = int(merged["volumeIndex"])
    if volume_index < 0:
        raise ReaderError("CONFIG_INVALID", "volumeIndex 必须 >= 0")
    patient_field = str(merged["patientField"])

    volumes: list[dict[str, Any]] = []
    for row in manifest_rows(manifest, "read-nifti", merged):
        row_id = str(row.get("rowId"))
        sample_id = str(row.get("sampleId") or row_id)
        assets = _row_assets_by_role(row)
        nifti_ids = assets.get(role, [])
        if len(nifti_ids) != 1:
            raise ReaderError(
                "ROW_MISSING_ROLE" if not nifti_ids else "ROW_ROLE_OVERFLOW",
                f"样本 {sample_id} 需要恰好一个 {role!r} 文件，实际 {len(nifti_ids)} 个",
                row_id=row_id,
                field=role,
                suggested_action="每行绑定一个 NIfTI 文件；多体积请使用不同样本行",
            )
        path = resolve(files[nifti_ids[0]])
        volumes.append(
            imaging.read_nifti_volume(
                path,
                row_id=row_id,
                asset_id=nifti_ids[0],
                sample_id=sample_id,
                patient_id=_patient_id(row, patient_field, sample_id),
                allow_4d=allow_4d,
                volume_index=volume_index,
            )
        )
    if not volumes:
        raise ReaderError("MANIFEST_INVALID", "集合没有任何 NIfTI 行")
    return {
        "schemaVersion": "1",
        "reader": {"key": "read-nifti", "version": "1.0.0"},
        "volumes": volumes,
        "diagnostics": [],
    }
