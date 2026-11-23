"""体数据处理节点（T9）：转换 / 筛选 / 合并 / 掩膜配对 / 标签关联。

输入与输出均为 volume_collection（schemaVersion=1，见 radstudio.imaging）：

    {
        "schemaVersion": "1",
        "reader": {"key": "read-dicom", "version": "1.0.0"},
        "volumes": [
            {"sampleId": "s1", "patientId": "p1", "format": "dicom",
             "geometry": {"shape": [z,y,x], "spacing": [z,y,x],
                          "origin": [x,y,z], "direction": [9 floats], ...},
             "dataRef": "file:...#sha256=...;type=volume"}
        ],
        "diagnostics": [...]
    }

处理原则（设计 §5）：
  * 掩膜配对比较尺寸、层间距、原点与方向，不只比较数组大小；
  * 重复主键、缺失标签、未命中行与被筛除样本必须显式处理并写入诊断，
    禁止静默丢行；
  * 转换不做隐式重采样，只改变数值类型/缩放并记录参数。
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np

from .operator import Operator, operator

COLLECTION_SCHEMA_VERSION = "1"

# 与 operators.json 默认值一致；参数缺失时补齐（节点由编辑器写入默认值）。
_DEFAULTS: dict[str, dict[str, Any]] = {
    "VolumeConversion": {"targetType": "float32", "scaling": "none"},
    "VolumeFilter": {"minSlices": 0, "maxSlices": 0, "modalities": [], "requireMask": False, "onExcluded": "error"},
    "VolumeMerge": {"onDuplicateSampleId": "error", "requireSameGeometry": False},
    "PairMasks": {
        "pairBy": "sampleId",
        "requireGeometryMatch": True,
        "geometryTolerance": 0.01,
        "onMissingMask": "error",
        "onExtraMask": "ignore_report",
    },
    "LabelAssociation": {
        "volumeKeyField": "patientId",
        "tableKeyField": "patientId",
        "labelField": "label",
        "onDuplicateKey": "error",
        "onMissingLabel": "error",
        "onUnmatchedRow": "error",
    },
}


class VolumeCollectionError(ValueError):
    """带稳定诊断码的体数据处理错误。"""

    def __init__(self, code: str, message: str, *, sample_id: str = "", suggested_action: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.sample_id = sample_id
        self.suggested_action = suggested_action

    def diagnostic(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "severity": "error", "message": str(self)}
        if self.sample_id:
            payload["sampleId"] = self.sample_id
        if self.suggested_action:
            payload["suggestedAction"] = self.suggested_action
        return payload


def _params(operator_type: str, parameters: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(_DEFAULTS[operator_type])
    for key, value in (parameters or {}).items():
        if key not in merged:
            raise VolumeCollectionError("CONFIG_INVALID", f"未知参数: {key}")
        merged[key] = value
    return merged


def _require_collection(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VolumeCollectionError("VOLUME_COLLECTION_INVALID", f"{label} 必须是 volume_collection 对象")
    if value.get("schemaVersion") != COLLECTION_SCHEMA_VERSION:
        raise VolumeCollectionError(
            "VOLUME_COLLECTION_INVALID",
            f"{label} schemaVersion 不支持: {value.get('schemaVersion')!r}",
        )
    volumes = value.get("volumes")
    if not isinstance(volumes, list):
        raise VolumeCollectionError("VOLUME_COLLECTION_INVALID", f"{label}.volumes 缺失")
    for entry in volumes:
        if not isinstance(entry, dict) or not entry.get("sampleId"):
            raise VolumeCollectionError("VOLUME_COLLECTION_INVALID", f"{label} 存在缺少 sampleId 的条目")
    return value


def _sample_id(entry: dict[str, Any]) -> str:
    return str(entry.get("sampleId") or "")


def _key_value(entry: dict[str, Any], field: str) -> str:
    raw = entry.get(field)
    if raw in (None, ""):
        raw = entry.get("sampleId")
    return str(raw).strip() if raw not in (None, "") else ""


def _geometry(entry: dict[str, Any]) -> dict[str, Any]:
    geometry = entry.get("geometry")
    if not isinstance(geometry, dict):
        raise VolumeCollectionError(
            "VOLUME_GEOMETRY_MISSING",
            f"样本 {_sample_id(entry)} 缺少 geometry，无法校验空间一致性",
            sample_id=_sample_id(entry),
            suggested_action="用 DICOM/NIfTI 读取节点重新生成体数据",
        )
    return geometry


def _load_array(ctx, entry: dict[str, Any]) -> np.ndarray:
    ref = str(entry.get("dataRef") or "")
    if not ref:
        raise VolumeCollectionError(
            "VOLUME_DATA_MISSING",
            f"样本 {_sample_id(entry)} 缺少 dataRef",
            sample_id=_sample_id(entry),
        )
    data = ctx.store.read_bytes(ref)
    array = np.load(io.BytesIO(data))
    return np.asarray(array, dtype=np.float32)


def _write_array(ctx, name: str, array: np.ndarray) -> str:
    buffer = io.BytesIO()
    np.save(buffer, np.asarray(array))
    return ctx.output_bytes(name, buffer.getvalue(), "volume")


def _geometries_match(a: dict[str, Any], b: dict[str, Any], tolerance: float) -> tuple[bool, str]:
    if list(a.get("shape") or []) != list(b.get("shape") or []):
        return False, f"shape {a.get('shape')} != {b.get('shape')}"
    for key in ("spacing", "origin", "direction"):
        left = [float(v) for v in a.get(key) or []]
        right = [float(v) for v in b.get(key) or []]
        if len(left) != len(right):
            return False, f"{key} 维度不同（{len(left)} vs {len(right)}）"
        if any(abs(x - y) > tolerance for x, y in zip(left, right, strict=False)):
            return False, f"{key} 差异超过容差 {tolerance}: {left} vs {right}"
    return True, ""


def _diagnostics(collection: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = collection.get("diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = []
        collection["diagnostics"] = diagnostics
    return diagnostics


@operator("VolumeConversion", version="1.0.0")
class VolumeConversionOperator(Operator):
    """显式数值类型/缩放转换；不做隐式重采样。"""

    def run(self, ctx):
        params = _params("VolumeConversion", ctx.parameters)
        target = str(params["targetType"])
        scaling = str(params["scaling"])
        if target not in ("float32", "int16", "uint8"):
            raise VolumeCollectionError("CONFIG_INVALID", f"targetType 不支持: {target}")
        if scaling not in ("none", "minmax", "zscore"):
            raise VolumeCollectionError("CONFIG_INVALID", f"scaling 不支持: {scaling}")
        collection = _require_collection(ctx.input_json("volumes"), "volumes")
        for index, entry in enumerate(collection["volumes"]):
            array = _load_array(ctx, entry)
            array = _scale(array, scaling)
            array = _cast(array, target)
            entry["dataRef"] = _write_array(ctx, f"converted/{index:05d}-{_sample_id(entry)}.npy", array)
            entry["conversion"] = {"targetType": target, "scaling": scaling}
        ctx.progress(70, f"已转换 {len(collection['volumes'])} 个体数据")
        return {"volumes": ctx.output_json("volumes.json", collection, "volume_collection")}


def _scale(array: np.ndarray, scaling: str) -> np.ndarray:
    if scaling == "minmax":
        low, high = float(np.min(array)), float(np.max(array))
        if high - low <= 1e-12:
            return np.zeros_like(array, dtype=np.float32)
        return ((array - low) / (high - low)).astype(np.float32)
    if scaling == "zscore":
        mean, std = float(np.mean(array)), float(np.std(array))
        if std <= 1e-12:
            return np.zeros_like(array, dtype=np.float32)
        return ((array - mean) / std).astype(np.float32)
    return np.asarray(array, dtype=np.float32)


def _cast(array: np.ndarray, target: str) -> np.ndarray:
    if target == "float32":
        return np.asarray(array, dtype=np.float32)
    if target == "int16":
        return np.clip(np.rint(array), -32768, 32767).astype(np.int16)
    return np.clip(np.rint(array), 0, 255).astype(np.uint8)


@operator("VolumeFilter", version="1.0.0")
class VolumeFilterOperator(Operator):
    """按显式条件筛选；排除样本必须记录或直接失败。"""

    def run(self, ctx):
        params = _params("VolumeFilter", ctx.parameters)
        collection = _require_collection(ctx.input_json("volumes"), "volumes")
        min_slices = int(params["minSlices"])
        max_slices = int(params["maxSlices"])
        modalities = {str(value) for value in (params["modalities"] or [])}
        require_mask = bool(params["requireMask"])
        on_excluded = str(params["onExcluded"])
        if on_excluded not in ("error", "report"):
            raise VolumeCollectionError("CONFIG_INVALID", f"onExcluded 不支持: {on_excluded}")

        kept: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for entry in collection["volumes"]:
            shape = list(_geometry(entry).get("shape") or [])
            slices = int(shape[0]) if shape else 0
            reasons: list[str] = []
            if min_slices and slices < min_slices:
                reasons.append(f"切片数 {slices} 小于 minSlices={min_slices}")
            if max_slices and slices > max_slices:
                reasons.append(f"切片数 {slices} 大于 maxSlices={max_slices}")
            if modalities and str(entry.get("modality") or "") not in modalities:
                reasons.append(f"模态 {entry.get('modality')!r} 不在 {sorted(modalities)}")
            if require_mask and not (entry.get("maskRef") or entry.get("maskDataRef")):
                reasons.append("缺少配对掩膜")
            if reasons:
                excluded.append({"sampleId": _sample_id(entry), "reasons": reasons})
            else:
                kept.append(entry)

        if excluded:
            summary = "；".join(f"{item['sampleId']}: {','.join(item['reasons'])}" for item in excluded)
            if on_excluded == "error":
                raise VolumeCollectionError(
                    "VOLUME_EXCLUDED",
                    f"{len(excluded)} 个样本不满足筛选条件：{summary}",
                    suggested_action="修正数据，或显式把 onExcluded 改为 report 并接受诊断记录",
                )
            diagnostics = _diagnostics(collection)
            for item in excluded:
                diagnostics.append(
                    {
                        "code": "VOLUME_EXCLUDED",
                        "severity": "warning",
                        "sampleId": item["sampleId"],
                        "message": "样本被筛选排除：" + "；".join(item["reasons"]),
                    }
                )
        collection["volumes"] = kept
        ctx.progress(60, f"保留 {len(kept)} 个样本，排除 {len(excluded)} 个")
        return {"volumes": ctx.output_json("volumes.json", collection, "volume_collection")}


@operator("VolumeMerge", version="1.0.0")
class VolumeMergeOperator(Operator):
    """合并多个 volume_collection；重复 sampleId 显式处理。"""

    def run(self, ctx):
        params = _params("VolumeMerge", ctx.parameters)
        on_duplicate = str(params["onDuplicateSampleId"])
        require_same_geometry = bool(params["requireSameGeometry"])
        if on_duplicate not in ("error", "keep_first"):
            raise VolumeCollectionError("CONFIG_INVALID", f"onDuplicateSampleId 不支持: {on_duplicate}")
        collections = ctx.input_set_json("volumes")
        if not collections:
            raise VolumeCollectionError("VOLUME_COLLECTION_INVALID", "没有可合并的 volume_collection 输入")

        merged: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        diagnostics: list[dict[str, Any]] = []
        reference_geometry: dict[str, Any] | None = None
        for collection in collections:
            _require_collection(collection, "volumes")
            for entry in collection["volumes"]:
                sid = _sample_id(entry)
                if require_same_geometry:
                    geometry = _geometry(entry)
                    if reference_geometry is None:
                        reference_geometry = geometry
                    else:
                        matched, reason = _geometries_match(reference_geometry, geometry, 0.01)
                        if not matched:
                            raise VolumeCollectionError(
                                "VOLUME_GEOMETRY_MISMATCH",
                                f"合并要求相同几何，但样本 {sid} 与首个集合不一致：{reason}",
                                sample_id=sid,
                                suggested_action="关闭 requireSameGeometry 或按几何分组后分开合并",
                            )
                if sid in seen:
                    if on_duplicate == "error":
                        raise VolumeCollectionError(
                            "VOLUME_DUPLICATE_SAMPLE_ID",
                            f"合并出现重复 sampleId {sid!r}",
                            sample_id=sid,
                            suggested_action="修正 sampleId，或将 onDuplicateSampleId 设为 keep_first",
                        )
                    diagnostics.append(
                        {
                            "code": "VOLUME_DUPLICATE_SAMPLE_ID",
                            "severity": "warning",
                            "sampleId": sid,
                            "message": "重复 sampleId 已按 keep_first 丢弃后续条目",
                        }
                    )
                    continue
                seen[sid] = entry
                merged.append(entry)
        result = {
            "schemaVersion": COLLECTION_SCHEMA_VERSION,
            "reader": {"key": "volume-merge", "version": "1.0.0"},
            "volumes": merged,
            "diagnostics": diagnostics,
        }
        ctx.progress(60, f"已合并 {len(merged)} 个样本")
        return {"volumes": ctx.output_json("volumes.json", result, "volume_collection")}


@operator("PairMasks", version="1.0.0")
class PairMasksOperator(Operator):
    """按稳定主键配对影像与掩膜；几何不符直接报错。"""

    def run(self, ctx):
        params = _params("PairMasks", ctx.parameters)
        pair_by = str(params["pairBy"])
        if pair_by not in ("sampleId", "patientId"):
            raise VolumeCollectionError("CONFIG_INVALID", f"pairBy 不支持: {pair_by}")
        require_geometry = bool(params["requireGeometryMatch"])
        tolerance = float(params["geometryTolerance"])
        on_missing = str(params["onMissingMask"])
        on_extra = str(params["onExtraMask"])
        if on_missing not in ("error", "report"):
            raise VolumeCollectionError("CONFIG_INVALID", f"onMissingMask 不支持: {on_missing}")
        if on_extra not in ("error", "ignore_report"):
            raise VolumeCollectionError("CONFIG_INVALID", f"onExtraMask 不支持: {on_extra}")

        volumes = _require_collection(ctx.input_json("volumes"), "volumes")
        masks = _require_collection(ctx.input_json("masks"), "masks")
        mask_index: dict[str, dict[str, Any]] = {}
        for mask in masks["volumes"]:
            key = _key_value(mask, pair_by)
            if not key:
                raise VolumeCollectionError(
                    "VOLUME_KEY_MISSING",
                    f"掩膜样本 {_sample_id(mask)} 缺少配对字段 {pair_by}",
                    sample_id=_sample_id(mask),
                )
            if key in mask_index:
                raise VolumeCollectionError(
                    "VOLUME_DUPLICATE_KEY",
                    f"掩膜集合的配对字段 {pair_by}={key!r} 重复",
                    sample_id=_sample_id(mask),
                    suggested_action="确保每个影像只有一份掩膜",
                )
            mask_index[key] = mask

        diagnostics = _diagnostics(volumes)
        paired: list[dict[str, Any]] = []
        for entry in volumes["volumes"]:
            key = _key_value(entry, pair_by)
            mask = mask_index.pop(key, None) if key else None
            if mask is None:
                if on_missing == "error":
                    raise VolumeCollectionError(
                        "MASK_MISSING",
                        f"样本 {_sample_id(entry)}（{pair_by}={key or '-'}）缺少掩膜",
                        sample_id=_sample_id(entry),
                        suggested_action="补齐掩膜，或显式把 onMissingMask 改为 report",
                    )
                diagnostics.append(
                    {
                        "code": "MASK_MISSING",
                        "severity": "warning",
                        "sampleId": _sample_id(entry),
                        "message": f"样本 {_sample_id(entry)} 缺少掩膜，已按 onMissingMask=report 跳过",
                    }
                )
                continue
            if require_geometry:
                matched, reason = _geometries_match(_geometry(entry), _geometry(mask), tolerance)
                if not matched:
                    raise VolumeCollectionError(
                        "MASK_GEOMETRY_MISMATCH",
                        f"样本 {_sample_id(entry)} 的掩膜几何不符：{reason}",
                        sample_id=_sample_id(entry),
                        suggested_action="重建掩膜使其与影像共享 shape/spacing/origin/direction",
                    )
            entry["maskRef"] = mask.get("dataRef")
            entry["maskSampleId"] = _sample_id(mask)
            paired.append(entry)

        if mask_index:
            extra = sorted(mask_index)
            if on_extra == "error":
                raise VolumeCollectionError(
                    "MASK_UNMATCHED",
                    f"有 {len(extra)} 个掩膜未匹配到影像：{extra[:10]}",
                    suggested_action="补齐影像行，或把 onExtraMask 改为 ignore_report",
                )
            diagnostics.append(
                {
                    "code": "MASK_UNMATCHED",
                    "severity": "warning",
                    "message": f"{len(extra)} 个掩膜未匹配到影像（已忽略）：{extra[:10]}",
                }
            )
        volumes["volumes"] = paired
        ctx.progress(60, f"已配对 {len(paired)} 个样本")
        return {"volumes": ctx.output_json("volumes.json", volumes, "volume_collection")}


@operator("LabelAssociation", version="1.0.0")
class LabelAssociationOperator(Operator):
    """按稳定主键把临床表标签关联到体数据；重复键/缺标签/未命中行显式处理。"""

    def run(self, ctx):
        params = _params("LabelAssociation", ctx.parameters)
        volume_field = str(params["volumeKeyField"])
        table_field = str(params["tableKeyField"])
        label_field = str(params["labelField"])
        on_duplicate = str(params["onDuplicateKey"])
        on_missing = str(params["onMissingLabel"])
        on_unmatched = str(params["onUnmatchedRow"])
        if on_duplicate not in ("error", "keep_first"):
            raise VolumeCollectionError("CONFIG_INVALID", f"onDuplicateKey 不支持: {on_duplicate}")
        if on_missing not in ("error", "report"):
            raise VolumeCollectionError("CONFIG_INVALID", f"onMissingLabel 不支持: {on_missing}")
        if on_unmatched not in ("error", "report"):
            raise VolumeCollectionError("CONFIG_INVALID", f"onUnmatchedRow 不支持: {on_unmatched}")
        if not label_field:
            raise VolumeCollectionError("CONFIG_INVALID", "labelField 不能为空")

        volumes = _require_collection(ctx.input_json("volumes"), "volumes")
        table = ctx.input_json("table")
        if not isinstance(table, dict) or not isinstance(table.get("records"), list):
            raise VolumeCollectionError("TABLE_INVALID", "table 输入缺少 records")
        columns = [str(name) for name in table.get("columns") or []]
        if label_field not in columns:
            raise VolumeCollectionError(
                "TABLE_LABEL_FIELD_MISSING",
                f"临床表缺少标签列 {label_field!r}（现有列：{columns}）",
                suggested_action="在样本表/表中补齐标签列或修改 labelField",
            )
        if table_field not in columns:
            raise VolumeCollectionError(
                "TABLE_KEY_FIELD_MISSING",
                f"临床表缺少主键列 {table_field!r}（现有列：{columns}）",
                suggested_action="修改 tableKeyField 或补齐临床表主键列",
            )

        table_index: dict[str, dict[str, Any]] = {}
        diagnostics = _diagnostics(volumes)
        for record in table["records"]:
            raw_key = record.get(table_field)
            if raw_key in (None, ""):
                if on_duplicate == "error":
                    raise VolumeCollectionError(
                        "TABLE_KEY_EMPTY",
                        f"临床表存在空主键 {table_field!r} 的记录",
                        suggested_action="补齐主键，或在读取表时排除空主键",
                    )
                continue
            key = str(raw_key).strip()
            if key in table_index:
                if on_duplicate == "error":
                    raise VolumeCollectionError(
                        "TABLE_DUPLICATE_KEY",
                        f"临床表主键 {table_field}={key!r} 重复",
                        suggested_action="修正重复主键，或将 onDuplicateKey 设为 keep_first",
                    )
                diagnostics.append(
                    {
                        "code": "TABLE_DUPLICATE_KEY",
                        "severity": "warning",
                        "message": f"临床表主键 {key!r} 重复，已按 keep_first 保留首条",
                    }
                )
                continue
            table_index[key] = record

        matched_keys: set[str] = set()
        for entry in volumes["volumes"]:
            key = _key_value(entry, volume_field)
            record = table_index.get(key) if key else None
            if record is None:
                if on_missing == "error":
                    raise VolumeCollectionError(
                        "LABEL_MISSING",
                        f"样本 {_sample_id(entry)}（{volume_field}={key or '-'}）在临床表中没有匹配标签",
                        sample_id=_sample_id(entry),
                        suggested_action="补齐临床表行，或显式把 onMissingLabel 改为 report",
                    )
                diagnostics.append(
                    {
                        "code": "LABEL_MISSING",
                        "severity": "warning",
                        "sampleId": _sample_id(entry),
                        "message": f"样本 {_sample_id(entry)} 未匹配到标签",
                    }
                )
                entry[label_field] = None
                continue
            matched_keys.add(key)
            label = record.get(label_field)
            if label in (None, ""):
                if on_missing == "error":
                    raise VolumeCollectionError(
                        "LABEL_EMPTY",
                        f"样本 {_sample_id(entry)} 匹配到临床表行但标签 {label_field!r} 为空",
                        sample_id=_sample_id(entry),
                        suggested_action="补齐标签值，或显式把 onMissingLabel 改为 report",
                    )
                diagnostics.append(
                    {
                        "code": "LABEL_EMPTY",
                        "severity": "warning",
                        "sampleId": _sample_id(entry),
                        "message": f"样本 {_sample_id(entry)} 的标签 {label_field!r} 为空",
                    }
                )
                entry[label_field] = None
                continue
            entry[label_field] = label

        unmatched = sorted(set(table_index) - matched_keys)
        if unmatched:
            if on_unmatched == "error":
                raise VolumeCollectionError(
                    "LABEL_UNMATCHED_ROW",
                    f"临床表有 {len(unmatched)} 行未匹配到任何样本：{unmatched[:10]}",
                    suggested_action="补齐影像行，或把 onUnmatchedRow 改为 report",
                )
            diagnostics.append(
                {
                    "code": "LABEL_UNMATCHED_ROW",
                    "severity": "warning",
                    "message": f"{len(unmatched)} 行临床表未匹配到样本（已记录）：{unmatched[:10]}",
                }
            )
        ctx.progress(60, f"已关联 {len(volumes['volumes'])} 个样本的标签")
        return {"volumes": ctx.output_json("volumes.json", volumes, "volume_collection")}
