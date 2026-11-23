"""M3 2D 预处理模块 - IBSI 标准预处理"""

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np


class InterpolationMethod(str, Enum):
    """插值方法"""

    LINEAR = "linear"
    NEAREST = "nearest"
    CUBIC = "cubic"


class BinMethod(str, Enum):
    """灰度离散化方法"""

    FIXED_BIN_WIDTH = "fixed_bin_width"
    FIXED_BIN_COUNT = "fixed_bin_count"


@dataclass
class PreprocessingConfig:
    """预处理配置"""

    # 插值方法
    interpolation: InterpolationMethod = InterpolationMethod.LINEAR

    # 像素间距 (mm)
    pixel_spacing_x: float | None = None
    pixel_spacing_y: float | None = None

    # 灰度离散化
    bin_method: BinMethod = BinMethod.FIXED_BIN_WIDTH
    bin_width: int = 25
    bin_count: int | None = None

    # 灰度范围限制
    min_intensity: float | None = None
    max_intensity: float | None = None

    # ROI 设置
    roi_mask_value: int = 1

    def validate(self) -> None:
        """验证配置"""
        if self.bin_method == BinMethod.FIXED_BIN_WIDTH and self.bin_width <= 0:
            raise ValueError("bin_width must be positive for fixed_bin_width method")
        if self.bin_method == BinMethod.FIXED_BIN_COUNT and (
            self.bin_count is None or self.bin_count <= 0
        ):
            raise ValueError("bin_count must be positive for fixed_bin_count method")


@dataclass
class PreprocessingResult:
    """预处理结果"""

    # 预处理后的图像
    image: np.ndarray

    # ROI 掩码
    roi_mask: np.ndarray

    # 灰度离散化后的图像
    discretized_image: np.ndarray

    # 元数据
    metadata: dict[str, Any]

    # 灰度统计
    intensity_stats: dict[str, float]


class ImagePreprocessor:
    """2D 图像预处理器"""

    def __init__(self, config: PreprocessingConfig | None = None):
        self.config = config or PreprocessingConfig()
        self.config.validate()

    def preprocess(
        self,
        image: np.ndarray,
        roi_mask: np.ndarray,
        pixel_spacing: tuple[float, float] | None = None,
    ) -> PreprocessingResult:
        """
        执行完整的预处理流程

        Args:
            image: 输入图像 (2D)
            roi_mask: ROI 掩码 (2D, 与图像同尺寸)
            pixel_spacing: 像素间距 (x, y) mm

        Returns:
            PreprocessingResult: 预处理结果
        """
        # 验证输入
        self._validate_inputs(image, roi_mask)

        # 设置像素间距
        if pixel_spacing:
            self.config.pixel_spacing_x = pixel_spacing[0]
            self.config.pixel_spacing_y = pixel_spacing[1]

        # 1. 提取 ROI 区域
        roi_image = self._extract_roi(image, roi_mask)

        # 2. 灰度值限制
        if self.config.min_intensity is not None or self.config.max_intensity is not None:
            roi_image = self._clamp_intensity(roi_image)

        # 3. 灰度离散化
        discretized = self._discretize_intensity(roi_image, roi_mask)

        # 4. 计算统计信息
        stats = self._compute_stats(roi_image, roi_mask)

        return PreprocessingResult(
            image=roi_image,
            roi_mask=roi_mask,
            discretized_image=discretized,
            metadata={
                "pixel_spacing_x": self.config.pixel_spacing_x,
                "pixel_spacing_y": self.config.pixel_spacing_y,
                "bin_method": self.config.bin_method.value,
                "bin_width": self.config.bin_width,
                "bin_count": self.config.bin_count,
                "interpolation": self.config.interpolation.value,
            },
            intensity_stats=stats,
        )

    def _validate_inputs(self, image: np.ndarray, roi_mask: np.ndarray) -> None:
        """验证输入数据"""
        if image.ndim != 2:
            raise ValueError(f"Image must be 2D, got {image.ndim}D")
        if roi_mask.ndim != 2:
            raise ValueError(f"ROI mask must be 2D, got {roi_mask.ndim}D")
        if image.shape != roi_mask.shape:
            raise ValueError(
                f"Image shape {image.shape} does not match ROI mask shape {roi_mask.shape}"
            )
        if not np.any(roi_mask > 0):
            raise ValueError("ROI mask is empty (all zeros)")

    def _extract_roi(self, image: np.ndarray, roi_mask: np.ndarray) -> np.ndarray:
        """提取 ROI 区域"""
        roi_value = self.config.roi_mask_value
        mask = roi_mask >= roi_value
        roi_image = np.where(mask, image, 0)
        return roi_image

    def _clamp_intensity(self, image: np.ndarray) -> np.ndarray:
        """限制灰度值范围"""
        result = image.copy()
        if self.config.min_intensity is not None:
            result = np.maximum(result, self.config.min_intensity)
        if self.config.max_intensity is not None:
            result = np.minimum(result, self.config.max_intensity)
        return result

    def _discretize_intensity(self, image: np.ndarray, roi_mask: np.ndarray) -> np.ndarray:
        """灰度离散化"""
        roi_value = self.config.roi_mask_value
        mask = roi_mask >= roi_value

        # 获取 ROI 内的灰度值
        roi_intensities = image[mask]

        if len(roi_intensities) == 0:
            return np.zeros_like(image, dtype=np.int32)

        min_val = np.min(roi_intensities)
        max_val = np.max(roi_intensities)

        if self.config.bin_method == BinMethod.FIXED_BIN_WIDTH:
            # 固定 bin 宽度
            bin_width = self.config.bin_width
            # 计算 bin 边界
            edges = np.arange(min_val, max_val + bin_width, bin_width)
            # 离散化
            discretized = np.digitize(image, edges)
            # ROI 外区域设为 0
            discretized = np.where(mask, discretized, 0)
        elif self.config.bin_method == BinMethod.FIXED_BIN_COUNT:
            # 固定 bin 数量
            bin_count = self.config.bin_count or 64
            # 计算 bin 边界
            edges = np.linspace(min_val, max_val, bin_count + 1)
            # 离散化
            discretized = np.digitize(image, edges)
            # ROI 外区域设为 0
            discretized = np.where(mask, discretized, 0)
        else:
            raise ValueError(f"Unknown bin method: {self.config.bin_method}")

        return discretized.astype(np.int32)

    def _compute_stats(self, image: np.ndarray, roi_mask: np.ndarray) -> dict[str, float]:
        """计算灰度统计信息"""
        roi_value = self.config.roi_mask_value
        mask = roi_mask >= roi_value
        roi_intensities = image[mask]

        if len(roi_intensities) == 0:
            return {}

        return {
            "mean": float(np.mean(roi_intensities)),
            "std": float(np.std(roi_intensities)),
            "min": float(np.min(roi_intensities)),
            "max": float(np.max(roi_intensities)),
            "median": float(np.median(roi_intensities)),
            "voxel_count": int(np.sum(mask)),
        }


class ROIVerifier:
    """ROI 校验器"""

    @staticmethod
    def verify_roi_alignment(
        image: np.ndarray, roi_mask: np.ndarray, tolerance: float = 0.01
    ) -> dict[str, Any]:
        """
        验证 ROI 与图像对齐

        Args:
            image: 输入图像
            roi_mask: ROI 掩码
            tolerance: 容差

        Returns:
            验证结果
        """
        result = {"aligned": True, "issues": [], "warnings": []}

        # 检查形状匹配
        if image.shape != roi_mask.shape:
            result["aligned"] = False
            result["issues"].append(f"Shape mismatch: image {image.shape} vs ROI {roi_mask.shape}")
            return result

        # 检查 ROI 是否为空
        roi_pixels = np.sum(roi_mask > 0)
        if roi_pixels == 0:
            result["aligned"] = False
            result["issues"].append("ROI is empty (no positive pixels)")
            return result

        # 检查 ROI 面积
        total_pixels = image.shape[0] * image.shape[1]
        roi_ratio = roi_pixels / total_pixels
        result["roi_pixels"] = int(roi_pixels)
        result["total_pixels"] = int(total_pixels)
        result["roi_ratio"] = float(roi_ratio)

        # 检查 ROI 是否超出图像边界
        if np.any(roi_mask > 0):
            roi_coords = np.where(roi_mask > 0)
            min_y, max_y = np.min(roi_coords[0]), np.max(roi_coords[0])
            min_x, max_x = np.min(roi_coords[1]), np.max(roi_coords[1])

            result["roi_bounds"] = {
                "min_y": int(min_y),
                "max_y": int(max_y),
                "min_x": int(min_x),
                "max_x": int(max_x),
            }

        # 检查 ROI 内的灰度值是否有效
        roi_intensities = image[roi_mask > 0]
        if np.any(np.isnan(roi_intensities)):
            result["warnings"].append("ROI contains NaN values")
        if np.any(np.isinf(roi_intensities)):
            result["warnings"].append("ROI contains Inf values")

        return result

    @staticmethod
    def compute_roi_overlap(roi1: np.ndarray, roi2: np.ndarray) -> float:
        """计算两个 ROI 的重叠率"""
        intersection = np.logical_and(roi1 > 0, roi2 > 0)
        union = np.logical_or(roi1 > 0, roi2 > 0)

        if np.sum(union) == 0:
            return 0.0

        return float(np.sum(intersection) / np.sum(union))
