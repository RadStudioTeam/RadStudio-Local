"""本地数据目录 → ReadImages 快照 manifest / runner request。

开源版 CLI 的最小数据约定（不依赖云端控制面）：

    data/
      images/            # 图片（png/jpg/tif/npy/nii/nii.gz）
      masks/             # 掩膜，默认按同名配对；也支持 rois/ 或 _mask 后缀
      labels.csv         # sample_id,label[,patient_id]

也支持图片与掩膜混在同一目录（按 `_mask`/`_roi`/`_seg` 后缀识别）。
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
    ".npy",
    ".nii",
    ".nii.gz",
}
MASK_TOKENS = ("_mask", "-mask", "_roi", "-roi", "_seg", "-seg")
DEFAULT_ID_COLUMNS = ("sample_id", "sampleId", "sampleid", "id", "image", "image_id", "case_id", "name")
DEFAULT_LABEL_COLUMNS = ("label", "labelvalue", "class", "target")
DEFAULT_PATIENT_COLUMNS = ("patient_id", "patientId", "patientid", "patient")


class LocalDataError(ValueError):
    """本地数据/工作流不满足 CLI 约定；带稳定错误码，便于测试与提示。"""

    code = "local.data_invalid"

    def __init__(self, message: str, *, field: str = "", suggested_action: str = ""):
        super().__init__(message)
        self.field = field
        self.suggested_action = suggested_action


def _suffix(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".nii.gz"):
        return ".nii.gz"
    return path.suffix.lower()


def _is_image(path: Path) -> bool:
    return _suffix(path) in IMAGE_SUFFIXES


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_key(value: str, token: str = "") -> str:
    key = value.strip().lower()
    if token:
        key = key.replace(token.strip().lower(), "")
    for item in MASK_TOKENS:
        key = key.replace(item, "")
    return key.strip(" _-.")


def _is_mask_name(stem: str, roi_token: str = "") -> bool:
    lower = stem.lower()
    if roi_token and roi_token.lower() in lower:
        return True
    return any(token in lower for token in MASK_TOKENS)


def _reader_parameters(ir: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "imageRole": "image",
        "roiRole": "roi",
        "requireRoi": True,
        "patientField": "patientId",
        "labelField": "labelValue",
        "positiveValue": "1",
        "negativeValue": "0",
        "roiNameContains": "",
    }
    defaults.update(node.get("parameters") or {})
    return defaults


def find_reader_node(
    ir: dict[str, Any], *, node_id: str | None = None, node_type: str = "ReadImages"
) -> dict[str, Any]:
    nodes = ir.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise LocalDataError("工作流没有节点", field="nodes")
    matched = [n for n in nodes if isinstance(n, dict) and n.get("type") == node_type]
    if node_id:
        for node in matched:
            if node.get("id") == node_id:
                return node
        raise LocalDataError(
            f"找不到读取节点 {node_id!r}（类型 {node_type}）", field="readerNode"
        )
    if not matched:
        raise LocalDataError(
            f"工作流里没有 {node_type} 节点；本地 v0.1 只支持以 {node_type} 开头的影像工作流",
            field="nodes",
            suggested_action="在云端/模板里新建一个以 ReadImages 开头的工作流，或使用 examples 中的模板",
        )
    if len(matched) > 1:
        ids = ", ".join(str(n.get("id")) for n in matched)
        raise LocalDataError(
            f"工作流有多个 {node_type} 节点（{ids}），请用 --reader-node 指定一个",
            field="readerNode",
        )
    return matched[0]


def load_workflow(path: Path) -> dict[str, Any]:
    """接受原始 Workflow IR、云端导出的 {ir: ...}、或 {irJson: "..."}。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LocalDataError(f"读不到工作流文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LocalDataError(f"工作流不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LocalDataError("工作流 JSON 顶层必须是对象")
    if isinstance(payload.get("nodes"), list) and isinstance(payload.get("edges"), list):
        return payload
    if isinstance(payload.get("ir"), dict):
        return payload["ir"]
    if isinstance(payload.get("irJson"), str):
        try:
            inner = json.loads(payload["irJson"])
        except json.JSONDecodeError as exc:
            raise LocalDataError(f"irJson 不是合法 JSON: {exc}") from exc
        if isinstance(inner, dict):
            return inner
    raise LocalDataError(
        "无法识别工作流格式：需要 Workflow IR（nodes/edges），或云端导出的 {ir: ...}",
        field="workflow",
    )


def _read_labels(
    labels_path: Path,
    *,
    id_column: str | None,
    label_column: str | None,
    patient_column: str | None,
) -> tuple[dict[str, dict[str, str]], str, str, str]:
    delimiter = "\t" if labels_path.suffix.lower() in {".tsv", ".tab"} else ","
    try:
        with labels_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            if not reader.fieldnames:
                raise LocalDataError("标签表没有表头", field="labels")
            fields = [str(name).strip() for name in reader.fieldnames]
            normalized = {name.lower(): name for name in fields}

            def pick(explicit: str | None, candidates: tuple[str, ...], label: str, required: bool) -> str:
                if explicit:
                    if explicit not in fields:
                        raise LocalDataError(f"标签表缺少列 {explicit!r}", field=label)
                    return explicit
                for candidate in candidates:
                    if candidate.lower() in normalized:
                        return normalized[candidate.lower()]
                if required:
                    raise LocalDataError(
                        f"标签表找不到 {label} 列（可用 --{label.replace('_', '-')} 指定）",
                        field=label,
                    )
                return ""

            id_key = pick(id_column, DEFAULT_ID_COLUMNS, "id_column", True)
            label_key = pick(label_column, DEFAULT_LABEL_COLUMNS, "label_column", True)
            patient_key = pick(patient_column, DEFAULT_PATIENT_COLUMNS, "patient_column", False)
            rows: dict[str, dict[str, str]] = {}
            for raw in reader:
                sample_id = str(raw.get(id_key) or "").strip()
                if not sample_id:
                    raise LocalDataError("标签表存在空的 sample id", field="labels")
                label = str(raw.get(label_key) or "").strip()
                patient = str(raw.get(patient_key) or "").strip() if patient_key else ""
                keys = {sample_id.lower(), Path(sample_id).stem.lower()}
                for key in keys:
                    if key in rows:
                        raise LocalDataError(f"标签表重复的 sample id: {sample_id}", field="labels")
                for key in keys:
                    rows[key] = {"label": label, "patient": patient}
            return rows, id_key, label_key, patient_key
    except OSError as exc:
        raise LocalDataError(f"读不到标签表: {labels_path}") from exc


def _infer_classes(
    rows: dict[str, dict[str, str]], positive: str, negative: str
) -> tuple[str, str]:
    values = sorted({row["label"] for row in rows.values()})
    if positive and negative:
        if positive == negative:
            raise LocalDataError("positive 与 negative 不能相同", field="positive")
        unknown = [v for v in values if v not in {positive, negative}]
        if unknown:
            raise LocalDataError(
                f"标签值 {unknown} 既不是 positive={positive!r} 也不是 negative={negative!r}",
                field="label_column",
            )
        return positive, negative
    if set(values) <= {"0", "1"} and len(values) == 2:
        return "1", "0"
    if len(values) == 2:
        return values[1], values[0]
    raise LocalDataError(
        f"标签需要恰好两个类别，当前为 {values}；可用 --positive/--negative 指定",
        field="label_column",
    )


def discover_dataset(
    data_dir: Path,
    *,
    ir: dict[str, Any],
    reader_node: dict[str, Any],
    images_dir: Path | None = None,
    masks_dir: Path | None = None,
    labels_path: Path | None = None,
    id_column: str | None = None,
    label_column: str | None = None,
    patient_column: str | None = None,
    positive: str = "",
    negative: str = "",
    allow_missing_roi: bool = False,
) -> dict[str, Any]:
    """扫描目录并生成 ReadImages 可消费的 manifest。"""
    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        raise LocalDataError(f"数据目录不存在: {data_dir}", field="data")
    params = _reader_parameters(ir, reader_node)
    roi_token = str(params.get("roiNameContains") or "")
    image_role = str(params.get("imageRole") or "image")
    roi_role = str(params.get("roiRole") or "roi")
    require_roi = bool(params.get("requireRoi", True)) and not allow_missing_roi
    patient_field = str(params.get("patientField") or "patientId")
    label_field = str(params.get("labelField") or "labelValue")

    images_root = (images_dir or (data_dir / "images")).resolve()
    if not images_root.is_dir():
        images_root = data_dir
    masks_root: Path | None
    if masks_dir:
        masks_root = masks_dir.resolve()
    else:
        for name in ("masks", "rois", "roi"):
            candidate = data_dir / name
            if candidate.is_dir():
                masks_root = candidate
                break
        else:
            masks_root = None

    labels_file = labels_path or next(
        (data_dir / name for name in ("labels.csv", "labels.tsv") if (data_dir / name).is_file()),
        None,
    )
    if labels_file is None:
        raise LocalDataError(
            f"找不到标签表：请把 labels.csv 放在 {data_dir}，或用 --labels 指定",
            field="labels",
        )

    label_rows, _, _, _ = _read_labels(
        labels_file,
        id_column=id_column,
        label_column=label_column,
        patient_column=patient_column,
    )

    # 图片/掩膜分类
    image_candidates: list[Path] = []
    mask_candidates: list[Path] = []
    for path in sorted(images_root.rglob("*")):
        if not path.is_file() or not _is_image(path):
            continue
        resolved = path.resolve()
        if masks_root is not None and resolved.is_relative_to(masks_root):
            continue
        if masks_root is None and _is_mask_name(path.stem, roi_token):
            mask_candidates.append(path)
        else:
            image_candidates.append(path)
    if masks_root is not None:
        for path in sorted(masks_root.rglob("*")):
            if path.is_file() and _is_image(path):
                mask_candidates.append(path)

    if not image_candidates:
        raise LocalDataError(
            f"{images_root} 下没有找到图片（支持 {', '.join(sorted(IMAGE_SUFFIXES))}）",
            field="data",
        )

    masks: dict[str, Path] = {}
    for path in mask_candidates:
        key = _clean_key(path.stem, roi_token)
        if key in masks:
            raise LocalDataError(f"掩膜文件名重复: {path.name}", field="masks")
        masks[key] = path

    selected: list[tuple[str, Path, Path | None, dict[str, str]]] = []
    missing: list[str] = []
    for image in image_candidates:
        key = _clean_key(image.stem)
        label_row = label_rows.get(image.stem.lower()) or label_rows.get(key)
        if label_row is None:
            raise LocalDataError(
                f"{image.name} 在标签表里没有对应行（按文件名去扩展名匹配）",
                field="labels",
            )
        mask = masks.get(key)
        if mask is None and require_roi:
            missing.append(image.name)
            continue
        selected.append((image.stem, image, mask, label_row))

    if missing:
        preview = "、".join(missing[:5])
        raise LocalDataError(
            f"{len(missing)} 张图片没有配到掩膜（例如 {preview}）；"
            "请按同名 / _mask 后缀配对，或加 --allow-missing-roi",
            field="masks",
        )
    if not selected:
        raise LocalDataError("没有可运行的样本", field="data")

    positive_value, negative_value = _infer_classes(
        {sample_id: row for sample_id, _, _, row in selected}, positive, negative
    )

    files: dict[str, dict[str, str]] = {}
    rows: list[dict[str, Any]] = []
    for index, (sample_id, image, mask, label_row) in enumerate(selected):
        image_id = f"{index:04d}-image{_suffix(image)}"
        files[image_id] = {"path": str(image), "sha256": sha256_file(image)}
        assets = [{"role": image_role, "fileId": image_id}]
        if mask is not None:
            roi_id = f"{index:04d}-roi{_suffix(mask)}"
            files[roi_id] = {"path": str(mask), "sha256": sha256_file(mask)}
            assets.append({"role": roi_role, "fileId": roi_id})
        patient = label_row.get("patient") or sample_id
        rows.append(
            {
                "rowId": sample_id,
                "sampleId": sample_id,
                "fields": {patient_field: patient, label_field: label_row["label"]},
                "assets": assets,
            }
        )

    config = {
        "imageRole": image_role,
        "roiRole": roi_role,
        "requireRoi": require_roi,
        "patientField": patient_field,
        "labelField": label_field,
        "positiveValue": positive_value,
        "negativeValue": negative_value,
    }
    if roi_token:
        config["roiNameContains"] = roi_token
    return {
        "schemaVersion": "1",
        "reader": {"key": "read-images", "version": "1.0.0", "implVersion": "1.0.0"},
        "collection": {"id": data_dir.name or "local", "revision": 1},
        "config": config,
        "files": files,
        "rows": rows,
    }


def write_manifest(manifest: dict[str, Any], out_dir: Path) -> tuple[Path, str]:
    inputs = out_dir / "_inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    path = inputs / "read-images-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, sha256_file(path)


def build_runner_request(
    ir: dict[str, Any],
    *,
    reader_node_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    run_id: str,
    target_node: str | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "runId": run_id,
        "ir": ir,
        "snapshotInputs": {
            reader_node_id: {
                "manifestPath": str(manifest_path.resolve()),
                "manifestSha256": manifest_sha256,
            }
        },
        "imageDigest": "sha256:local",
    }
    if target_node:
        request["targetNodeId"] = target_node
    return request
