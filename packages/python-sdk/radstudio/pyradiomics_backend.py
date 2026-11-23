"""PyRadiomics 特征提取后端。

用于把系统内的特征提取口径与外部 PyRadiomics 对齐（参考训练集
``训练集_ZScore标准化.xlsx`` 即由 PyRadiomics 提取）：

- 输入图像按 ``(1, H, W)`` 单层体积交给 PyRadiomics，以 3D 模式提取，
  与参考集一致：original = 107（93 个一阶/纹理 + 14 个 3D 形状），
  wavelet 共 8 个子带各 93 个，合计 851 个特征。
- 离散化由 PyRadiomics 内部按 ``binWidth`` 完成，不使用
  Preprocessing2D 的预离散化结果。

依赖安装注意：PyPI 上的 pyradiomics sdist 缺少 ``cmatrices.h`` 无法编译
（上游 issue #949），本项目从 GitHub 源码钉 commit 安装，见 pyproject.toml。
"""

from __future__ import annotations

import logging

import numpy as np

# 源码安装地址（与 pyproject.toml 保持一致），reproduction 归档的
# requirements.lock 用它替代无法从 PyPI 安装的 pyradiomics==version。
PYRADIOMICS_REQUIREMENT = (
    "pyradiomics @ https://github.com/AIM-Harvard/pyradiomics/archive/"
    "8ed579383b44806651c463d5e691f3b2b57522ab.tar.gz"
)

# 工作流参数 featureFamilies -> PyRadiomics 特征类名。
# shape_2d 映射为 3D "shape" 类：参考训练集的 original_shape_* 正是 3D 形状特征
# （在单层体积上计算，退化维度与参考集一致）。
FEATURE_CLASS_BY_FAMILY = {
    "first_order": "firstorder",
    "shape_2d": "shape",
    "glcm": "glcm",
    "glrlm": "glrlm",
    "glszm": "glszm",
    "gldm": "gldm",
    "ngtdm": "ngtdm",
}

# 工作流参数 imageTypes -> PyRadiomics 图像类型名（大小写敏感）。
IMAGE_TYPE_BY_NAME = {
    "original": "Original",
    "wavelet": "Wavelet",
    "square": "Square",
    "squareroot": "SquareRoot",
    "logarithm": "Logarithm",
    "exponential": "Exponential",
    "gradient": "Gradient",
}

_FEATURE_KEY_PREFIXES = ("diagnostics", "general")


def extract(
    image: np.ndarray,
    roi_mask: np.ndarray,
    families: list[str],
    image_types: list[str],
    bin_width: float = 25.0,
) -> dict[str, float]:
    """用 PyRadiomics 提取特征，返回 ``特征名 -> 浮点值``。

    Args:
        image: 2D 原始灰度图像。
        roi_mask: 2D ROI 掩码（非零为 ROI）。
        families: 工作流特征族参数（``FEATURE_CLASS_BY_FAMILY`` 的键）。
        image_types: 工作流图像类型参数（``IMAGE_TYPE_BY_NAME`` 的键）。
        bin_width: PyRadiomics 灰度离散化 bin 宽度。

    Returns:
        PyRadiomics 命名的特征字典（如 ``original_firstorder_Mean``），
        不含 diagnostics/general 元数据键；非有限值原样保留，由调用方处理。
    """
    import SimpleITK as sitk
    from radiomics import featureextractor, setVerbosity

    # PyRadiomics 逐样本向 stderr 输出 warning（如 GLCM 对称性提示），数百
    # 样本 × 多图像类型即产生数千行、数 MB 输出，会淹没容器错误信息
    # （实验 6a543adc 的 2MB error_message 即此问题）。错误仍以异常抛出，
    # 不受静默影响。
    setVerbosity(logging.ERROR)

    img = np.asarray(image, dtype=np.float64)
    mask = np.asarray(roi_mask)
    if img.ndim != 2:
        raise ValueError(f"Image must be 2D, got {img.ndim}D")
    if mask.shape != img.shape:
        raise ValueError(f"Mask shape {mask.shape} does not match image shape {img.shape}")
    if not np.any(mask > 0):
        raise ValueError("ROI mask is empty")

    unknown_families = [f for f in families if f not in FEATURE_CLASS_BY_FAMILY]
    if unknown_families:
        raise ValueError(f"Unknown feature families: {unknown_families}")
    unknown_types = [t for t in image_types if t not in IMAGE_TYPE_BY_NAME]
    if unknown_types:
        raise ValueError(f"Unknown image types: {unknown_types}")

    # (1, H, W) 单层体积：与参考训练集的 3D 模式口径一致（8 个小波子带）。
    input_image = sitk.GetImageFromArray(img[None, :, :])
    input_mask = sitk.GetImageFromArray((mask > 0).astype(np.uint8)[None, :, :])

    extractor = featureextractor.RadiomicsFeatureExtractor(binWidth=float(bin_width))
    extractor.addProvenance(False)  # 不输出 diagnostics_* 元数据

    extractor.disableAllImageTypes()
    for name in image_types:
        extractor.enableImageTypeByName(IMAGE_TYPE_BY_NAME[name])

    extractor.disableAllFeatures()
    for family in families:
        extractor.enableFeatureClassByName(FEATURE_CLASS_BY_FAMILY[family])

    result = extractor.execute(input_image, input_mask)
    values: dict[str, float] = {}
    for key, value in result.items():
        if key.startswith(_FEATURE_KEY_PREFIXES):
            continue
        if isinstance(value, complex):
            # shape 特征来自特征分解，可能返回虚部为 0 的复数（如 Elongation）。
            # 虚部非零说明上游数值异常，显式失败而不是静默丢弃。
            if abs(value.imag) > 1e-9 * max(1.0, abs(value.real)):
                raise ValueError(f"Feature {key} returned a complex value: {value}")
            value = value.real
        values[key] = float(value)
    return values
