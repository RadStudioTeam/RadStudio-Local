"""数据准备算子：库内/跨库近重复影像检测与 ROI 裁剪。

``PerceptualHashDedup`` 自实现 pHash（DCT + 中位数二值化，不依赖 imagehash），
用汉明距离 + 并查集把近重复影像聚成簇，只保留输入顺序中的第一张代表样本；
``ROICrop`` 按掩膜 bbox 与 margin 外扩裁剪影像与 ROI，供「全图 vs ROI」对照实验。

模块只依赖 numpy/PIL/scipy，不进入 [dl] extra，任何运行环境都能注册与执行。
"""

from __future__ import annotations

import copy
import io

import numpy as np
from PIL import Image
from scipy.fft import dctn

from .artifact_boundary import parse_reference
from .execution_contract import execution_parameters
from .operator import Operator, operator
from .runner import read_image

# pHash 统一把影像缩放到 32×32 再取低频 DCT 系数（低频集中度足够，且与 hashSize 解耦）。
_HASH_INPUT = 32
_HASH_SIZE_RANGE = (4, 32)
_MAX_DISTANCE_RANGE = (0, 64)


def _resize_gray(image: np.ndarray, size: int) -> np.ndarray:
    """二维灰度数组 → PIL 双线性缩放到 size×size（浮点保留原始动态范围）。"""
    resized = Image.fromarray(np.asarray(image, dtype=np.float32), mode="F").resize(
        (size, size), resample=Image.Resampling.BILINEAR
    )
    return np.asarray(resized, dtype=float)


def _perceptual_hash(image: np.ndarray, hash_size: int) -> tuple[str, int]:
    """自实现 pHash：DCT 左上 hashSize² 去 DC 后按中位数二值化，返回 (hex, 位整数)。"""
    coefficients = dctn(_resize_gray(image, _HASH_INPUT), norm="ortho")[
        :hash_size, :hash_size
    ]
    # 丢弃 DC 项：整体亮度不参与感知哈希，否则同图不同窗宽会被判成不同影像。
    values = coefficients.reshape(-1)[1:]
    bits = values > np.median(values)
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{(bits.size + 3) // 4}x}", value


class _UnionFind:
    """并查集：近重复样本合并到同一簇；根取簇内最小下标，代表样本即输入顺序第一张。"""

    def __init__(self, size: int):
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[max(left_root, right_root)] = min(left_root, right_root)


def _unique_in_order(values):
    """按首次出现顺序去重（datasetId 可为 None，不能直接排序）。"""
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique


@operator("PerceptualHashDedup", version="1.0.0")
class PerceptualHashDedupOperator(Operator):
    """库内/跨库近重复影像检测：pHash 汉明距离 + 并查集聚簇，只保留代表样本。"""

    def run(self, ctx):
        p = execution_parameters(ctx)
        hash_size = int(p.get("hashSize", 8))
        if not _HASH_SIZE_RANGE[0] <= hash_size <= _HASH_SIZE_RANGE[1]:
            raise ValueError(
                f"hashSize must be in [{_HASH_SIZE_RANGE[0]}, {_HASH_SIZE_RANGE[1]}]"
            )
        max_distance = int(p.get("maxDistance", 6))
        if not _MAX_DISTANCE_RANGE[0] <= max_distance <= _MAX_DISTANCE_RANGE[1]:
            raise ValueError(
                f"maxDistance must be in [{_MAX_DISTANCE_RANGE[0]}, {_MAX_DISTANCE_RANGE[1]}]"
            )
        cross_dataset = bool(p.get("crossDataset", True))
        parameters = {
            "hashSize": hash_size,
            "maxDistance": max_distance,
            "crossDataset": cross_dataset,
        }

        samples = copy.deepcopy(ctx.input_json("samples"))
        count = len(samples)
        if count == 0:
            return {
                "samples": ctx.output_json("samples.json", [], "sample_collection"),
                "report": ctx.output_json(
                    "dedup-report.json",
                    {
                        "inputCount": 0,
                        "keptCount": 0,
                        "duplicateCount": 0,
                        "intraDatasetDuplicates": 0,
                        "crossDatasetDuplicates": 0,
                        "clusters": [],
                        "unreadable": [],
                        "parameters": parameters,
                    },
                    "report",
                ),
            }

        # datasetId 缺失统一记为 None：彼此视为同一数据集，同时保留在报告里。
        datasets = [sample.get("datasetId") for sample in samples]
        hashes: list[str | None] = [None] * count
        values: list[int | None] = [None] * count
        unreadable: list[str] = []
        for position, sample in enumerate(samples):
            ctx.check_cancelled()
            path = sample.get("imagePath")
            if not path:
                unreadable.append(sample.get("imageId"))
                continue
            try:
                hashes[position], values[position] = _perceptual_hash(
                    read_image(ctx.store.resolve_path(path)), hash_size
                )
            except (OSError, ValueError):
                # 读图失败不中断整库去重：该样本按原样保留，报告里单独列出。
                unreadable.append(sample.get("imageId"))
                continue
            ctx.progress(
                40.0 * (position + 1) / count, f"hash {position + 1}/{count}"
            )

        union = _UnionFind(count)
        compared = 0
        for left in range(count):
            ctx.check_cancelled()
            if values[left] is None:
                continue
            for right in range(left + 1, count):
                if values[right] is None:
                    continue
                if not cross_dataset and datasets[left] != datasets[right]:
                    continue
                if (values[left] ^ values[right]).bit_count() <= max_distance:
                    union.union(left, right)
            compared += 1
            ctx.progress(
                40.0 + 50.0 * compared / count, f"compare {compared}/{count}"
            )

        components: dict[int, list[int]] = {}
        for position in range(count):
            if values[position] is None:
                continue
            components.setdefault(union.find(position), []).append(position)

        kept: list[dict] = []
        clusters: list[dict] = []
        intra = cross = 0
        cluster_ids: dict[int, str] = {}
        for position, sample in enumerate(samples):
            if values[position] is None:
                # 无法计算哈希的样本直接保留（不加去重字段），不计入近重复。
                kept.append(sample)
                continue
            root = union.find(position)
            if root not in cluster_ids:
                cluster_ids[root] = f"c{len(cluster_ids) + 1}"
            members = components[root]
            if position == members[0]:
                sample["perceptualHash"] = hashes[position]
                sample["dedupClusterId"] = cluster_ids[root]
                kept.append(sample)
                cluster_distance = max(
                    (values[left] ^ values[right]).bit_count()
                    for left in members
                    for right in members
                )
                clusters.append(
                    {
                        "clusterId": cluster_ids[root],
                        "representative": samples[root]["imageId"],
                        "members": [samples[index]["imageId"] for index in members],
                        "maxDistance": cluster_distance,
                        "datasets": _unique_in_order([datasets[index] for index in members]),
                    }
                )
            elif datasets[position] == datasets[root]:
                intra += 1
            else:
                cross += 1

        ctx.progress(100.0, "dedup done")
        report = {
            "inputCount": count,
            "keptCount": len(kept),
            "duplicateCount": count - len(kept),
            "intraDatasetDuplicates": intra,
            "crossDatasetDuplicates": cross,
            "clusters": clusters,
            "unreadable": unreadable,
            "parameters": parameters,
        }
        return {
            "samples": ctx.output_json("samples.json", kept, "sample_collection"),
            "report": ctx.output_json("dedup-report.json", report, "report"),
        }


def _axis_bounds(start: int, stop: int, minimum: int, limit: int) -> tuple[int, int]:
    """把 [start, stop) 夹回 [0, limit]，边长不足 minimum 时以其中点扩到 minimum。

    图像本身比 minimum 更小时直接取全图，保证裁剪框永远落在图像内。
    """
    start, stop = min(max(start, 0), limit), min(max(stop, 0), limit)
    if stop - start >= minimum:
        return start, stop
    target = min(minimum, limit)
    center = (start + stop) / 2.0
    begin = min(max(int(np.floor(center - target / 2.0)), 0), limit - target)
    return begin, begin + target


def _crop_box(mask: np.ndarray, margin: float, min_size: int) -> list[int] | None:
    """ROI 前景 bbox 按长边比例外扩，再夹到图像内并补足 minSize；空掩膜返回 None。"""
    rows = np.flatnonzero(np.any(mask > 0, axis=1))
    cols = np.flatnonzero(np.any(mask > 0, axis=0))
    if rows.size == 0 or cols.size == 0:
        return None
    height, width = mask.shape
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    # margin 是相对 bbox 长边的外扩比例：长边 out 的影像得到同样绝对外扩量。
    pad = float(margin) * max(x1 - x0, y1 - y0)
    x0, x1 = _axis_bounds(int(np.floor(x0 - pad)), int(np.ceil(x1 + pad)), min_size, width)
    y0, y1 = _axis_bounds(int(np.floor(y0 - pad)), int(np.ceil(y1 + pad)), min_size, height)
    return [x0, y0, x1, y1]


def _png_bytes(values: np.ndarray, binary: bool = False) -> bytes:
    """裁剪结果存 PNG：掩膜二值化到 0/255，影像在原 [0,255] 内保持原值否则做 min-max。"""
    array = np.asarray(values, dtype=float)
    if binary:
        payload = (array > 0).astype(np.uint8) * 255
    elif array.size and array.min() >= 0.0 and array.max() <= 255.0:
        payload = np.clip(array, 0, 255).astype(np.uint8)
    else:
        low, high = (float(array.min()), float(array.max())) if array.size else (0.0, 0.0)
        payload = np.clip((array - low) / max(high - low, 1e-12) * 255.0, 0, 255).astype(
            np.uint8
        )
    buffer = io.BytesIO()
    Image.fromarray(payload).save(buffer, format="PNG")
    return buffer.getvalue()


@operator("ROICrop", version="1.0.0")
class ROICropOperator(Operator):
    """按掩膜 bbox + margin 裁剪影像与 ROI，边界贴边外扩也不会越界。"""

    def run(self, ctx):
        p = execution_parameters(ctx)
        margin = float(p.get("margin", 0.2))
        if not 0.0 <= margin <= 1.0:
            raise ValueError("margin must be in [0, 1]")
        min_size = int(p.get("minSize", 4))
        if not 2 <= min_size <= 64:
            raise ValueError("minSize must be in [2, 64]")
        parameters = {"margin": margin, "minSize": min_size}

        samples = copy.deepcopy(ctx.input_json("samples"))
        cropped = 0
        blocked: list[dict] = []
        boxes: list[dict] = []

        def block(sample, reason):
            sample["qcStatus"] = "blocked"
            sample["cropStatus"] = reason
            blocked.append({"imageId": sample.get("imageId"), "reason": reason})

        for position, sample in enumerate(samples):
            ctx.check_cancelled()
            image_path = sample.get("imagePath")
            roi_path = sample.get("roiPath")
            if not roi_path or not ctx.store.resolve_path(roi_path).is_file():
                block(sample, "missing_roi")
                continue
            if not image_path or not ctx.store.resolve_path(image_path).is_file():
                block(sample, "missing_image")
                continue
            try:
                image = read_image(ctx.store.resolve_path(image_path))
                roi = read_image(ctx.store.resolve_path(roi_path))
            except (OSError, ValueError):
                block(sample, "unreadable")
                continue
            if image.shape != roi.shape:
                block(sample, "shape_mismatch")
                continue
            box = _crop_box(roi, margin, min_size)
            if box is None:
                block(sample, "empty_roi")
                continue

            x0, y0, x1, y1 = box
            image_ref = ctx.output_bytes(
                f"crops/{sample['imageId']}-image.png",
                _png_bytes(image[y0:y1, x0:x1]),
                "image",
            )
            roi_ref = ctx.output_bytes(
                f"crops/{sample['imageId']}-roi.png",
                _png_bytes(roi[y0:y1, x0:x1], binary=True),
                "mask",
            )
            sample["originalImagePath"] = image_path
            sample["originalRoiPath"] = roi_path
            sample["imagePath"] = parse_reference(image_ref)["path"]
            sample["roiPath"] = parse_reference(roi_ref)["path"]
            sample["cropBox"] = box
            sample["cropStatus"] = "cropped"
            cropped += 1
            boxes.append(
                {
                    "imageId": sample["imageId"],
                    "cropBox": box,
                    "width": x1 - x0,
                    "height": y1 - y0,
                }
            )
            ctx.progress(
                100.0 * (position + 1) / len(samples), f"crop {position + 1}/{len(samples)}"
            )

        report = {
            "inputCount": len(samples),
            "croppedCount": cropped,
            "blockedCount": len(blocked),
            "blocked": blocked,
            "boxes": boxes,
            "parameters": parameters,
        }
        return {
            "samples": ctx.output_json("samples.json", samples, "sample_collection"),
            "report": ctx.output_json("crop-report.json", report, "report"),
        }
