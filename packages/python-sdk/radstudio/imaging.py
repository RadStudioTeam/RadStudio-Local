"""DICOM / NIfTI-1 读取（T9，docs/dataset-resource-workbench-design.md §5）。

只实现判定序列、切片顺序、缺片与空间几何所需的最小解析：
  * DICOM：未压缩 Explicit/Implicit VR Little Endian；压缩传输语法与
    Explicit VR Big Endian 明确拒绝；
  * NIfTI：NIfTI-1 单文件（n+1）与 .nii.gz，支持三维/四维，按显式序号选取体积。

解析失败一律抛出 data_readers.ReaderError（稳定 code + 定位信息），
调用方不得静默跳过切片或行。
"""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

from .data_readers import ReaderError

# DICOM 传输语法
_TS_IMPLICIT_LE = "1.2.840.10008.1.2"
_TS_EXPLICIT_LE = "1.2.840.10008.1.2.1"

_VR_32BIT = {
    "OB",
    "OW",
    "OF",
    "OD",
    "OL",
    "OV",
    "SQ",
    "UT",
    "UN",
    "UC",
    "UR",
    "SV",
    "UV",
}

# 仅读取判定所需的标签，其余按长度跳过。
_TAG_MODALITY = (0x0008, 0x0060)
_TAG_SOP_CLASS = (0x0008, 0x0016)
_TAG_STUDY_UID = (0x0020, 0x000D)
_TAG_SERIES_UID = (0x0020, 0x000E)
_TAG_INSTANCE_NUMBER = (0x0020, 0x0013)
_TAG_IMAGE_POSITION = (0x0020, 0x0032)
_TAG_IMAGE_ORIENTATION = (0x0020, 0x0037)
_TAG_SLICE_THICKNESS = (0x0018, 0x0050)
_TAG_ROWS = (0x0028, 0x0010)
_TAG_COLUMNS = (0x0028, 0x0011)
_TAG_PIXEL_SPACING = (0x0028, 0x0030)
_TAG_BITS_ALLOCATED = (0x0028, 0x0100)
_TAG_PIXEL_REPRESENTATION = (0x0028, 0x0103)
_TAG_SAMPLES_PER_PIXEL = (0x0028, 0x0002)
_TAG_RESCALE_SLOPE = (0x0028, 0x1053)
_TAG_RESCALE_INTERCEPT = (0x0028, 0x1052)
_TAG_PIXEL_DATA = (0x7FE0, 0x0010)


@dataclass
class DicomSlice:
    """单张切片的头部元数据与像素数组。"""

    path: Path
    transfer_syntax: str = ""
    modality: str = ""
    study_uid: str = ""
    series_uid: str = ""
    instance_number: int | None = None
    rows: int = 0
    columns: int = 0
    pixel_spacing: list[float] = field(default_factory=list)
    image_position: list[float] = field(default_factory=list)
    image_orientation: list[float] = field(default_factory=list)
    slice_thickness: float | None = None
    bits_allocated: int = 0
    pixel_representation: int = 0
    samples_per_pixel: int = 1
    rescale_slope: float = 1.0
    rescale_intercept: float = 0.0
    pixels: np.ndarray | None = None


def _dicom_failure(code: str, message: str, *, row_id: str = "", asset_id: str = "", action: str = "") -> ReaderError:
    return ReaderError(code, message, row_id=row_id, asset_id=asset_id, suggested_action=action)


def _read_element_header(handle: BinaryIO, explicit: bool) -> tuple[int, int, str, int, int] | None:
    """读取一个元素头；返回 (group, elem, vr, length, value_offset)。"""
    head = handle.read(8)
    if len(head) < 8:
        return None
    group, elem = struct.unpack("<HH", head[:4])
    if explicit:
        vr = head[4:6].decode("ascii", "replace")
        if vr in _VR_32BIT:
            extra = handle.read(4)
            if len(extra) < 4:
                return None
            length = struct.unpack("<I", extra[0:4])[0]
        else:
            length = struct.unpack("<H", head[6:8])[0]
    else:
        vr = ""
        length = struct.unpack("<I", head[4:8])[0]
    return group, elem, vr, length, handle.tell()


def _read_slice_header(path: Path, row_id: str, asset_id: str) -> DicomSlice:
    """解析 DICOM 文件头并读取像素数据。"""
    header = DicomSlice(path=path)
    with path.open("rb") as handle:
        preamble = handle.read(132)
        if len(preamble) < 132 or preamble[128:132] != b"DICM":
            raise _dicom_failure(
                "DICOM_MAGIC_MISMATCH",
                f"文件 {path.name} 缺少 DICM 前导标识",
                row_id=row_id,
                asset_id=asset_id,
                action="确认文件未损坏且确实是 DICOM",
            )
        # 文件元信息组始终 Explicit VR Little Endian。
        while True:
            start = handle.tell()
            parsed = _read_element_header(handle, explicit=True)
            if parsed is None:
                raise _dicom_failure(
                    "DICOM_HEADER_INVALID",
                    f"文件 {path.name} 的文件元信息不完整",
                    row_id=row_id,
                    asset_id=asset_id,
                )
            group, elem, _vr, length, value_offset = parsed
            if group != 0x0002:
                handle.seek(start)
                break
            if length <= 0 or length > 1 << 20:
                raise _dicom_failure(
                    "DICOM_HEADER_INVALID",
                    f"文件 {path.name} 的文件元信息长度非法（{length}）",
                    row_id=row_id,
                    asset_id=asset_id,
                )
            value = handle.read(length)
            if group == 0x0002 and elem == 0x0010:
                header.transfer_syntax = value.rstrip(b"\x00 ").decode("ascii", "replace")

        if header.transfer_syntax == _TS_IMPLICIT_LE:
            explicit = False
        elif header.transfer_syntax in ("", _TS_EXPLICIT_LE):
            explicit = True
        else:
            raise _dicom_failure(
                "DICOM_TRANSFER_SYNTAX_UNSUPPORTED",
                f"文件 {path.name} 的传输语法 {header.transfer_syntax!r} 不受支持（仅未压缩 Little Endian）",
                row_id=row_id,
                asset_id=asset_id,
                action="转换为未压缩 Explicit/Implicit VR Little Endian",
            )

        pixel_offset = pixel_length = 0
        while True:
            parsed = _read_element_header(handle, explicit=explicit)
            if parsed is None:
                break
            group, elem, _vr, length, value_offset = parsed
            if length == 0xFFFFFFFF:
                break  # 未定义长度：序列或封装像素
            tag = (group, elem)
            if tag == _TAG_PIXEL_DATA:
                pixel_offset, pixel_length = value_offset, length
                break
            if tag in (
                _TAG_MODALITY,
                _TAG_SOP_CLASS,
                _TAG_STUDY_UID,
                _TAG_SERIES_UID,
                _TAG_INSTANCE_NUMBER,
                _TAG_IMAGE_POSITION,
                _TAG_IMAGE_ORIENTATION,
                _TAG_SLICE_THICKNESS,
                _TAG_PIXEL_SPACING,
                _TAG_RESCALE_SLOPE,
                _TAG_RESCALE_INTERCEPT,
            ):
                if length > 1 << 20:
                    raise _dicom_failure(
                        "DICOM_HEADER_INVALID",
                        f"文件 {path.name} 的标签 {tag} 长度异常（{length}）",
                        row_id=row_id,
                        asset_id=asset_id,
                    )
                value = handle.read(length)
                _apply_text_tag(header, tag, value)
            elif tag in (_TAG_ROWS, _TAG_COLUMNS, _TAG_BITS_ALLOCATED, _TAG_PIXEL_REPRESENTATION, _TAG_SAMPLES_PER_PIXEL):
                value = handle.read(length)
                _apply_numeric_tag(header, tag, value)
            else:
                handle.seek(length, 1)

        if not pixel_offset or not pixel_length:
            raise _dicom_failure(
                "DICOM_HEADER_INVALID",
                f"文件 {path.name} 未找到像素数据",
                row_id=row_id,
                asset_id=asset_id,
                action="确认文件包含未压缩 PixelData",
            )

    if not header.series_uid and not header.study_uid:
        raise _dicom_failure(
            "DICOM_HEADER_INVALID",
            f"文件 {path.name} 缺少 Series/Study Instance UID",
            row_id=row_id,
            asset_id=asset_id,
        )
    header.pixels = _read_dicom_pixels(path, header, pixel_offset, pixel_length, row_id, asset_id)
    return header


def _apply_text_tag(header: DicomSlice, tag: tuple[int, int], value: bytes) -> None:
    text = value.rstrip(b"\x00 ").decode("ascii", "replace")
    if tag == _TAG_MODALITY:
        header.modality = text.strip()
    elif tag == _TAG_STUDY_UID:
        header.study_uid = text.strip()
    elif tag == _TAG_SERIES_UID:
        header.series_uid = text.strip()
    elif tag == _TAG_SOP_CLASS:
        pass
    elif tag == _TAG_INSTANCE_NUMBER:
        try:
            header.instance_number = int(text.strip())
        except ValueError:
            header.instance_number = None
    elif tag == _TAG_IMAGE_POSITION:
        header.image_position = _parse_floats(text)
    elif tag == _TAG_IMAGE_ORIENTATION:
        header.image_orientation = _parse_floats(text)
    elif tag == _TAG_SLICE_THICKNESS:
        values = _parse_floats(text)
        header.slice_thickness = values[0] if values else None
    elif tag == _TAG_PIXEL_SPACING:
        header.pixel_spacing = _parse_floats(text)
    elif tag == _TAG_RESCALE_SLOPE:
        values = _parse_floats(text)
        header.rescale_slope = values[0] if values and values[0] != 0 else 1.0
    elif tag == _TAG_RESCALE_INTERCEPT:
        values = _parse_floats(text)
        header.rescale_intercept = values[0] if values else 0.0


def _apply_numeric_tag(header: DicomSlice, tag: tuple[int, int], value: bytes) -> None:
    if len(value) < 2:
        return
    number = struct.unpack("<H", value[:2])[0]
    if tag == _TAG_ROWS:
        header.rows = number
    elif tag == _TAG_COLUMNS:
        header.columns = number
    elif tag == _TAG_BITS_ALLOCATED:
        header.bits_allocated = number
    elif tag == _TAG_PIXEL_REPRESENTATION:
        header.pixel_representation = number
    elif tag == _TAG_SAMPLES_PER_PIXEL:
        header.samples_per_pixel = number


def _parse_floats(text: str) -> list[float]:
    values: list[float] = []
    for part in text.split("\\"):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(float(part))
        except ValueError:
            return []
    return values


def _read_dicom_pixels(
    path: Path,
    header: DicomSlice,
    offset: int,
    length: int,
    row_id: str,
    asset_id: str,
) -> np.ndarray:
    if header.rows <= 0 or header.columns <= 0:
        raise _dicom_failure(
            "DICOM_HEADER_INVALID",
            f"文件 {path.name} 缺少 Rows/Columns",
            row_id=row_id,
            asset_id=asset_id,
        )
    if header.samples_per_pixel != 1:
        raise _dicom_failure(
            "DICOM_PHOTOMETRIC_UNSUPPORTED",
            f"文件 {path.name} 为多通道像素（SamplesPerPixel={header.samples_per_pixel}），体数据读取器只支持单通道",
            row_id=row_id,
            asset_id=asset_id,
            action="转换为单通道灰度序列",
        )
    dtype: np.dtype
    if header.bits_allocated == 8:
        dtype = np.dtype("i1" if header.pixel_representation else "u1")
    elif header.bits_allocated == 16:
        dtype = np.dtype("<i2" if header.pixel_representation else "<u2")
    elif header.bits_allocated == 32:
        dtype = np.dtype("<i4" if header.pixel_representation else "<u4")
    else:
        raise _dicom_failure(
            "DICOM_HEADER_INVALID",
            f"文件 {path.name} 的 BitsAllocated={header.bits_allocated} 不受支持",
            row_id=row_id,
            asset_id=asset_id,
        )
    expected = header.rows * header.columns * dtype.itemsize
    if length < expected:
        raise _dicom_failure(
            "DICOM_HEADER_INVALID",
            f"文件 {path.name} 像素数据长度 {length} 小于期望 {expected}",
            row_id=row_id,
            asset_id=asset_id,
        )
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(expected)
    pixels = np.frombuffer(raw, dtype=dtype).reshape(header.rows, header.columns)
    values = pixels.astype(np.float32) * np.float32(header.rescale_slope) + np.float32(header.rescale_intercept)
    return values


def _cross(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


@dataclass
class DicomCheckResult:
    slices: list[DicomSlice]
    warnings: list[dict[str, Any]]
    missing_slices: list[str]


def load_dicom_series(
    paths: list[Path],
    *,
    row_id: str,
    sample_id: str,
    require_single_series: bool,
    sort_by: str,
    on_missing_slices: str,
    asset_ids: list[str] | None = None,
    names: list[str] | None = None,
) -> DicomCheckResult:
    """读取并校验一行 DICOM 切片序列；错误定位到行与具体切片。

    ``names`` 是上传时的原始文件名：快照对象键是内容寻址的，扩展名只能从
    原始文件名判断；缺省时退回本地路径。
    """
    if not paths:
        raise ReaderError(
            "ROW_MISSING_ROLE",
            f"行 {sample_id} 没有 DICOM 切片",
            row_id=row_id,
            suggested_action="为该行绑定同一序列的 DICOM 切片",
        )
    slices: list[DicomSlice] = []
    for index, path in enumerate(paths):
        asset_id = asset_ids[index] if asset_ids else ""
        display = (
            names[index]
            if names and index < len(names) and names[index]
            else path.name
        )
        if Path(display).suffix.lower() not in (".dcm", ".dicom"):
            raise _dicom_failure(
                "FILE_EXTENSION_UNSUPPORTED",
                f"文件 {display} 不是受支持的 DICOM 扩展名（.dcm/.dicom）",
                row_id=row_id,
                asset_id=asset_id,
            )
        slices.append(_read_slice_header(path, row_id, asset_id))

    series = {item.series_uid for item in slices if item.series_uid}
    if require_single_series and len(series) > 1:
        raise ReaderError(
            "DICOM_MIXED_SERIES",
            f"样本 {sample_id} 包含 {len(series)} 个 SeriesInstanceUID（混序列）",
            row_id=row_id,
            field="dicom",
            suggested_action="按序列拆分样本行，或关闭 requireSingleSeries",
        )

    first = slices[0]
    for item in slices[1:]:
        if (
            item.rows != first.rows
            or item.columns != first.columns
            or item.pixel_spacing != first.pixel_spacing
            or item.image_orientation != first.image_orientation
        ):
            raise ReaderError(
                "DICOM_GEOMETRY_INCONSISTENT",
                f"样本 {sample_id} 的切片行列数、PixelSpacing 或方向不一致（{item.path.name}）",
                row_id=row_id,
                field="dicom",
                suggested_action="检查是否混入不同重建/序列的切片",
            )

    warnings: list[dict[str, Any]] = []
    missing = _dicom_gaps(slices, sort_by, sample_id, row_id)
    if missing:
        if on_missing_slices == "error":
            raise ReaderError(
                "DICOM_MISSING_SLICES",
                f"样本 {sample_id} 检测到缺片：{'；'.join(missing)}",
                row_id=row_id,
                field="dicom",
                suggested_action="补齐缺失切片；若确需继续，显式把 onMissingSlices 改为 report",
            )
        warnings.append(
            {
                "code": "DICOM_MISSING_SLICES",
                "severity": "warning",
                "rowId": row_id,
                "message": f"样本 {sample_id} 检测到缺片：{'；'.join(missing)}",
                "suggestedAction": "确认缺片是否影响分析；补齐后重新读取",
            }
        )

    if sort_by == "instance_number":
        slices.sort(key=lambda item: item.instance_number if item.instance_number is not None else 0)
    else:
        orientation = first.image_orientation
        normal = _cross(orientation[:3], orientation[3:]) if len(orientation) == 6 else [0.0, 0.0, 1.0]
        slices.sort(key=lambda item: sum(p * n for p, n in zip(item.image_position, normal, strict=False)))
    return DicomCheckResult(slices=slices, warnings=warnings, missing_slices=missing)


def _dicom_gaps(slices: list[DicomSlice], sort_by: str, sample_id: str, row_id: str) -> list[str]:
    if sort_by == "instance_number":
        if any(item.instance_number is None for item in slices):
            raise ReaderError(
                "DICOM_ORDER_UNRESOLVED",
                f"样本 {sample_id} 有切片缺少 InstanceNumber，无法按实例号排序",
                row_id=row_id,
                field="dicom",
            )
        numbers = sorted(int(item.instance_number) for item in slices)
        gaps: list[str] = []
        for previous, current in pairwise(numbers):
            if current - previous > 1:
                missing = ",".join(str(n) for n in range(previous + 1, current)[:10])
                gaps.append(f"实例号缺少 {missing}")
        return gaps

    if len(slices[0].image_orientation) != 6:
        raise ReaderError(
            "DICOM_ORDER_UNRESOLVED",
            f"样本 {sample_id} 缺少 ImageOrientationPatient，无法按位置排序",
            row_id=row_id,
            field="dicom",
        )
    orientation = slices[0].image_orientation
    normal = _cross(orientation[:3], orientation[3:])
    positions: list[float] = []
    for item in slices:
        if len(item.image_position) != 3:
            raise ReaderError(
                "DICOM_ORDER_UNRESOLVED",
                f"样本 {sample_id} 有切片缺少 ImagePositionPatient，无法按位置排序",
                row_id=row_id,
                field="dicom",
            )
        positions.append(sum(p * n for p, n in zip(item.image_position, normal, strict=False)))
    positions.sort()
    diffs = [b - a for a, b in pairwise(positions)]
    if any(diff <= 1e-6 for diff in diffs):
        raise ReaderError(
            "DICOM_GEOMETRY_INCONSISTENT",
            f"样本 {sample_id} 存在重复切片位置，无法确定顺序",
            row_id=row_id,
            field="dicom",
        )
    if not diffs:
        return []
    spacing = _median(diffs)
    missing_count = sum(round(diff / spacing) - 1 for diff in diffs if diff > spacing * 1.5)
    if missing_count > 0:
        return [f"层间距异常，估计缺少 {missing_count} 层（中位层间距 {spacing:.3f} mm）"]
    return []


def build_dicom_volume(
    result: DicomCheckResult,
    *,
    sample_id: str,
    patient_id: str,
    max_slices: int,
) -> dict[str, Any]:
    """把已排序切片堆叠为 volume_collection 的一个体数据条目。"""
    slices = result.slices
    if len(slices) > max_slices:
        raise ReaderError(
            "DICOM_TOO_MANY_SLICES",
            f"样本 {sample_id} 切片数 {len(slices)} 超过 maxSlicesPerSeries={max_slices}",
            field="dicom",
            suggested_action="拆分序列或提高 maxSlicesPerSeries（上限 2000）",
        )
    first = slices[0]
    stacked = np.stack([item.pixels for item in slices], axis=0) if slices else np.zeros((0, 0, 0), dtype=np.float32)
    orientation = first.image_orientation
    if len(orientation) == 6:
        row_dir = orientation[:3]
        col_dir = orientation[3:]
        normal = _cross(row_dir, col_dir)
        direction = [*row_dir, *col_dir, *normal]
    else:
        direction = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    origin = list(first.image_position) if len(first.image_position) == 3 else [0.0, 0.0, 0.0]
    if len(slices) > 1:
        projections = [
            sum(p * n for p, n in zip(item.image_position, direction[6:], strict=False))
            for item in slices
            if len(item.image_position) == 3
        ]
        diffs = [b - a for a, b in pairwise(projections)]
        slice_spacing = _median(diffs) if diffs else float(first.slice_thickness or 1.0)
    else:
        slice_spacing = float(first.slice_thickness or 1.0)
    pixel_spacing = first.pixel_spacing if len(first.pixel_spacing) == 2 else [1.0, 1.0]
    return {
        "sampleId": sample_id,
        "patientId": patient_id,
        "format": "dicom",
        "modality": first.modality,
        "seriesInstanceUid": first.series_uid,
        "geometry": {
            "shape": [int(stacked.shape[0]), int(stacked.shape[1]), int(stacked.shape[2])],
            "spacing": [float(slice_spacing), float(pixel_spacing[0]), float(pixel_spacing[1])],
            "origin": [float(v) for v in origin],
            "direction": [float(v) for v in direction],
            "rows": int(first.rows),
            "columns": int(first.columns),
            "sliceCount": int(stacked.shape[0]),
        },
        "warnings": result.warnings,
        "array": stacked,
    }


# ---------------------------------------------------------------------------
# NIfTI-1
# ---------------------------------------------------------------------------

_NIFTI_DTYPES: dict[int, np.dtype] = {
    2: np.dtype("u1"),
    4: np.dtype("<i2"),
    8: np.dtype("<i4"),
    16: np.dtype("<f4"),
    64: np.dtype("<f8"),
    256: np.dtype("i1"),
    512: np.dtype("<u2"),
    768: np.dtype("<u4"),
    1024: np.dtype("<i8"),
    1280: np.dtype("<u8"),
}


def _read_nifti_header(
    path: Path,
    *,
    row_id: str,
    asset_id: str,
    allow_4d: bool,
    volume_index: int,
) -> tuple[dict[str, Any], np.ndarray]:
    # 快照对象键是内容寻址的（无扩展名），gzip 只能按魔数判断。
    with path.open("rb") as probe:
        gzipped = probe.read(2) == b"\x1f\x8b"
    opener = gzip.open if gzipped else open
    with opener(path, "rb") as handle:
        head = handle.read(348)
        if len(head) < 348:
            raise _nifti_invalid(path, row_id, asset_id)
        endian = "<"
        if struct.unpack("<i", head[:4])[0] != 348:
            if struct.unpack(">i", head[:4])[0] != 348:
                raise _nifti_failure(
                    "NIFTI_MAGIC_MISMATCH",
                    f"文件 {path.name} 缺少 NIfTI-1 sizeof_hdr 标识",
                    row_id=row_id,
                    asset_id=asset_id,
                )
            endian = ">"
        magic = head[344:348].rstrip(b"\x00").decode("ascii", "replace")
        if magic not in ("n+1", "ni1"):
            raise _nifti_failure(
                "NIFTI_MAGIC_MISMATCH",
                f"文件 {path.name} 的 NIfTI magic {magic!r} 不受支持",
                row_id=row_id,
                asset_id=asset_id,
            )
        dims = list(struct.unpack(f"{endian}8h", head[40:56]))
        pixdim = list(struct.unpack(f"{endian}8f", head[76:108]))
        datatype = struct.unpack(f"{endian}h", head[70:72])[0]
        vox_offset = struct.unpack(f"{endian}f", head[108:112])[0]
        slope = struct.unpack(f"{endian}f", head[112:116])[0]
        intercept = struct.unpack(f"{endian}f", head[116:120])[0]
        sform_code = struct.unpack(f"{endian}h", head[254:256])[0]
        srow = struct.unpack(f"{endian}12f", head[280:328])
        qform_code = struct.unpack(f"{endian}h", head[252:254])[0]
        quatern = struct.unpack(f"{endian}3f", head[256:268])
        qoffset = struct.unpack(f"{endian}3f", head[268:280])
        qfac = pixdim[0] if pixdim[0] != 0 else 1.0

        if dims[0] < 3 or dims[0] > 4:
            raise _nifti_failure(
                "NIFTI_DIMENSION_UNSUPPORTED",
                f"文件 {path.name} 维度数 {dims[0]} 不在支持范围（仅三维/四维）",
                row_id=row_id,
                asset_id=asset_id,
            )
        if any(value <= 0 for value in dims[1:4]):
            raise _nifti_invalid(path, row_id, asset_id)
        if dims[0] == 4:
            if not allow_4d:
                raise _nifti_failure(
                    "NIFTI_4D_NOT_ALLOWED",
                    f"文件 {path.name} 为四维（{dims[4]} 个体积），allow4D=false",
                    row_id=row_id,
                    asset_id=asset_id,
                    action="允许四维并用 volumeIndex 选择体积，或上传三维文件",
                )
            if volume_index >= dims[4]:
                raise _nifti_failure(
                    "NIFTI_VOLUME_INDEX_OUT_OF_RANGE",
                    f"文件 {path.name} 的 volumeIndex={volume_index} 超出体积数 {dims[4]}",
                    row_id=row_id,
                    asset_id=asset_id,
                )
        dtype = _NIFTI_DTYPES.get(datatype)
        if dtype is None:
            raise _nifti_failure(
                "NIFTI_DIMENSION_UNSUPPORTED",
                f"文件 {path.name} 的 datatype={datatype} 不受支持",
                row_id=row_id,
                asset_id=asset_id,
            )
        if endian == ">":
            dtype = dtype.newbyteorder(">")
        shape_xyz = tuple(int(v) for v in dims[1 : dims[0] + 1])
        handle.seek(int(vox_offset or 352))
        count = int(np.prod(shape_xyz))
        raw = handle.read(count * dtype.itemsize)
        if len(raw) < count * dtype.itemsize:
            raise _nifti_invalid(path, row_id, asset_id)
        # NIfTI 数据按最快变化维度在前存储；转置为 numpy 的 (t,)z,y,x。
        data = np.frombuffer(raw, dtype=dtype).reshape(shape_xyz, order="F")
        if dims[0] == 4:
            values = data.transpose(3, 2, 1, 0).astype(np.float32)
            values = values[volume_index, ...]
        else:
            values = data.transpose(2, 1, 0).astype(np.float32)
        if slope not in (0.0, 1.0):
            values = values * np.float32(slope)
        if intercept:
            values = values + np.float32(intercept)

    spacing_xyz = [float(v) if v > 0 else 1.0 for v in pixdim[1:4]]
    affine = _nifti_affine(sform_code, srow, qform_code, quatern, qoffset, spacing_xyz, qfac)
    header = {
        "dims": dims,
        "spacing": [spacing_xyz[2], spacing_xyz[1], spacing_xyz[0]],
        "origin": [float(affine[0][3]), float(affine[1][3]), float(affine[2][3])],
        "direction": _affine_direction(affine),
        "datatype": datatype,
        "volumeIndex": volume_index if dims[0] == 4 else 0,
    }
    return header, values


def _nifti_affine(
    sform_code: int,
    srow: tuple[float, ...],
    qform_code: int,
    quatern: tuple[float, ...],
    qoffset: tuple[float, ...],
    spacing: list[float],
    qfac: float,
) -> list[list[float]]:
    if sform_code > 0:
        return [
            [srow[0], srow[1], srow[2], srow[3]],
            [srow[4], srow[5], srow[6], srow[7]],
            [srow[8], srow[9], srow[10], srow[11]],
        ]
    if qform_code > 0:
        b, c, d = quatern[0], quatern[1], quatern[2]
        a = max(0.0, 1.0 - (b * b + c * c + d * d)) ** 0.5
        rotation = [
            [a * a + b * b - c * c - d * d, 2 * (b * c - a * d), 2 * (b * d + a * c)],
            [2 * (b * c + a * d), a * a + c * c - b * b - d * d, 2 * (c * d - a * b)],
            [2 * (b * d - a * c), 2 * (c * d + a * b), a * a + d * d - b * b - c * c],
        ]
        qfac = qfac if qfac != 0 else 1.0
        scales = [spacing[0], spacing[1], spacing[2] * qfac]
        return [
            [rotation[r][0] * scales[0], rotation[r][1] * scales[1], rotation[r][2] * scales[2], qoffset[r]]
            for r in range(3)
        ]
    return [
        [spacing[0], 0.0, 0.0, 0.0],
        [0.0, spacing[1], 0.0, 0.0],
        [0.0, 0.0, spacing[2], 0.0],
    ]


def _affine_direction(affine: list[list[float]]) -> list[float]:
    columns = [[affine[row][col] for row in range(3)] for col in range(3)]
    normalized: list[float] = []
    for column in columns:
        norm = sum(value * value for value in column) ** 0.5
        normalized.extend([value / norm if norm else 0.0 for value in column])
    return normalized


def _nifti_failure(code: str, message: str, *, row_id: str, asset_id: str, action: str = "") -> ReaderError:
    return ReaderError(code, message, row_id=row_id, asset_id=asset_id, suggested_action=action)


def _nifti_invalid(path: Path, row_id: str, asset_id: str) -> ReaderError:
    return _nifti_failure(
        "NIFTI_HEADER_INVALID",
        f"文件 {path.name} 的 NIfTI 头不完整或数据长度不足",
        row_id=row_id,
        asset_id=asset_id,
        action="确认文件完整；必要时重新导出 NIfTI-1",
    )


def read_nifti_volume(
    path: Path,
    *,
    row_id: str,
    asset_id: str,
    sample_id: str,
    patient_id: str,
    allow_4d: bool,
    volume_index: int,
) -> dict[str, Any]:
    """读取单个 NIfTI 文件为一个 volume_collection 条目。"""
    header, values = _read_nifti_header(
        path,
        row_id=row_id,
        asset_id=asset_id,
        allow_4d=allow_4d,
        volume_index=volume_index,
    )
    return {
        "sampleId": sample_id,
        "patientId": patient_id,
        "format": "nifti",
        "modality": "",
        "seriesInstanceUid": "",
        "geometry": {
            "shape": [int(v) for v in values.shape],
            "spacing": header["spacing"],
            "origin": header["origin"],
            "direction": header["direction"],
            "rows": int(values.shape[1]),
            "columns": int(values.shape[2]),
            "sliceCount": int(values.shape[0]),
        },
        "warnings": [],
        "array": values,
    }
