"""M3 IBSI 2D 特征提取模块"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np


class FeatureFamily(str, Enum):
    """特征族"""

    FIRST_ORDER = "first_order"
    SHAPE_2D = "shape_2d"
    GLCM = "glcm"
    GLRLM = "glrlm"
    GLSZM = "glszm"
    GLDM = "gldm"
    NGTDM = "ngtdm"


@dataclass
class FeatureConfig:
    """特征提取配置"""

    # 启用的特征族
    enabled_families: list[FeatureFamily] = field(
        default_factory=lambda: [
            FeatureFamily.FIRST_ORDER,
            FeatureFamily.SHAPE_2D,
            FeatureFamily.GLCM,
        ]
    )

    # GLCM 参数
    glcm_distances: list[int] = field(default_factory=lambda: [1])
    glcm_angles: list[float] = field(
        default_factory=lambda: [0, np.pi / 4, np.pi / 2, 3 * np.pi / 4]
    )
    glcm_symmetric: bool = True
    glcm_normalized: bool = True

    # GLRLM 参数
    glrlm_angles: list[float] = field(
        default_factory=lambda: [0, np.pi / 4, np.pi / 2, 3 * np.pi / 4]
    )

    # GLSZM 参数
    gldm_dependence_threshold: float = 0.0

    # 特征选择
    feature_ids: list[str] | None = None  # None 表示提取所有特征


@dataclass
class FeatureValue:
    """特征值"""

    feature_id: str
    feature_name: str
    family: FeatureFamily
    value: float
    unit: str = ""
    description: str = ""


@dataclass
class FeatureExtractionResult:
    """特征提取结果"""

    features: list[FeatureValue]
    metadata: dict[str, Any]
    computation_time: float

    def to_dict(self) -> dict[str, float]:
        """转换为字典格式"""
        return {f.feature_id: f.value for f in self.features}

    def to_table(self) -> list[dict[str, Any]]:
        """转换为表格格式"""
        return [
            {
                "feature_id": f.feature_id,
                "feature_name": f.feature_name,
                "family": f.family.value,
                "value": f.value,
                "unit": f.unit,
            }
            for f in self.features
        ]


class FeatureExtractor:
    """IBSI 2D 特征提取器"""

    def __init__(self, config: FeatureConfig | None = None):
        self.config = config or FeatureConfig()

    def extract(
        self,
        image: np.ndarray,
        roi_mask: np.ndarray,
        discretized_image: np.ndarray | None = None,
        pixel_spacing: tuple[float, float] | None = None,
    ) -> FeatureExtractionResult:
        """
        提取特征

        Args:
            image: 原始图像
            roi_mask: ROI 掩码
            discretized_image: 灰度离散化后的图像（用于纹理特征）
            pixel_spacing: 像素间距 (x, y) mm

        Returns:
            FeatureExtractionResult: 特征提取结果
        """
        import time

        start_time = time.time()

        features = []

        # 准备 ROI 数据
        roi_coords = np.where(roi_mask > 0)
        if len(roi_coords[0]) == 0:
            return FeatureExtractionResult(
                features=[], metadata={"error": "Empty ROI"}, computation_time=0.0
            )

        # 使用离散化图像或原始图像
        if discretized_image is None:
            discretized_image = image

        # 提取各族特征
        for family in self.config.enabled_families:
            if family == FeatureFamily.FIRST_ORDER:
                family_features = self._extract_first_order(image, roi_mask)
            elif family == FeatureFamily.SHAPE_2D:
                family_features = self._extract_shape_2d(roi_mask, pixel_spacing)
            elif family == FeatureFamily.GLCM:
                family_features = self._extract_glcm(discretized_image, roi_mask)
            elif family == FeatureFamily.GLRLM:
                family_features = self._extract_glrlm(discretized_image, roi_mask)
            elif family == FeatureFamily.GLSZM:
                family_features = self._extract_glszm(discretized_image, roi_mask)
            elif family == FeatureFamily.GLDM:
                family_features = self._extract_gldm(discretized_image, roi_mask)
            elif family == FeatureFamily.NGTDM:
                family_features = self._extract_ngtdm(discretized_image, roi_mask)
            else:
                continue

            features.extend(family_features)

        # 过滤特征
        if self.config.feature_ids:
            features = [f for f in features if f.feature_id in self.config.feature_ids]

        computation_time = time.time() - start_time

        return FeatureExtractionResult(
            features=features,
            metadata={
                "pixel_spacing": pixel_spacing,
                "roi_voxel_count": len(roi_coords[0]),
                "enabled_families": [f.value for f in self.config.enabled_families],
            },
            computation_time=computation_time,
        )

    def _extract_first_order(self, image: np.ndarray, roi_mask: np.ndarray) -> list[FeatureValue]:
        """提取一阶统计特征"""
        roi_values = image[roi_mask > 0].astype(float)

        if len(roi_values) == 0:
            return []

        features = []

        # Energy
        energy = np.sum(roi_values**2)
        features.append(
            FeatureValue(
                feature_id="fo_energy",
                feature_name="Energy",
                family=FeatureFamily.FIRST_ORDER,
                value=float(energy),
                description="一阶统计特征 - 能量",
            )
        )

        # Entropy
        # 归一化灰度值
        _unique_values, counts = np.unique(roi_values, return_counts=True)
        probabilities = counts / counts.sum()
        entropy = -np.sum(probabilities * np.log2(probabilities + 1e-10))
        features.append(
            FeatureValue(
                feature_id="fo_entropy",
                feature_name="Entropy",
                family=FeatureFamily.FIRST_ORDER,
                value=float(entropy),
                description="一阶统计特征 - 熵",
            )
        )

        # Mean
        mean_val = np.mean(roi_values)
        features.append(
            FeatureValue(
                feature_id="fo_mean",
                feature_name="Mean",
                family=FeatureFamily.FIRST_ORDER,
                value=float(mean_val),
                description="一阶统计特征 - 均值",
            )
        )

        # Median
        median_val = np.median(roi_values)
        features.append(
            FeatureValue(
                feature_id="fo_median",
                feature_name="Median",
                family=FeatureFamily.FIRST_ORDER,
                value=float(median_val),
                description="一阶统计特征 - 中位数",
            )
        )

        # Variance
        variance = np.var(roi_values)
        features.append(
            FeatureValue(
                feature_id="fo_variance",
                feature_name="Variance",
                family=FeatureFamily.FIRST_ORDER,
                value=float(variance),
                description="一阶统计特征 - 方差",
            )
        )

        # Standard Deviation
        std_val = np.std(roi_values)
        features.append(
            FeatureValue(
                feature_id="fo_std",
                feature_name="Standard Deviation",
                family=FeatureFamily.FIRST_ORDER,
                value=float(std_val),
                description="一阶统计特征 - 标准差",
            )
        )

        # Skewness
        skewness = self._compute_skewness(roi_values)
        features.append(
            FeatureValue(
                feature_id="fo_skewness",
                feature_name="Skewness",
                family=FeatureFamily.FIRST_ORDER,
                value=float(skewness),
                description="一阶统计特征 - 偏度",
            )
        )

        # Kurtosis
        kurtosis = self._compute_kurtosis(roi_values)
        features.append(
            FeatureValue(
                feature_id="fo_kurtosis",
                feature_name="Kurtosis",
                family=FeatureFamily.FIRST_ORDER,
                value=float(kurtosis),
                description="一阶统计特征 - 峰度",
            )
        )

        # Min, Max, Range
        features.append(
            FeatureValue(
                feature_id="fo_min",
                feature_name="Minimum",
                family=FeatureFamily.FIRST_ORDER,
                value=float(np.min(roi_values)),
                description="一阶统计特征 - 最小值",
            )
        )

        features.append(
            FeatureValue(
                feature_id="fo_max",
                feature_name="Maximum",
                family=FeatureFamily.FIRST_ORDER,
                value=float(np.max(roi_values)),
                description="一阶统计特征 - 最大值",
            )
        )

        features.append(
            FeatureValue(
                feature_id="fo_range",
                feature_name="Range",
                family=FeatureFamily.FIRST_ORDER,
                value=float(np.max(roi_values) - np.min(roi_values)),
                description="一阶统计特征 - 范围",
            )
        )

        return features

    def _extract_shape_2d(
        self, roi_mask: np.ndarray, pixel_spacing: tuple[float, float] | None = None
    ) -> list[FeatureValue]:
        """提取 2D 形状特征"""
        features = []

        # 计算 ROI 面积（像素数）
        area_pixels = np.sum(roi_mask > 0)
        features.append(
            FeatureValue(
                feature_id="shape_area_pixels",
                feature_name="Area (pixels)",
                family=FeatureFamily.SHAPE_2D,
                value=float(area_pixels),
                description="2D 形状特征 - 面积（像素）",
            )
        )

        # 计算物理面积
        if pixel_spacing:
            physical_area = area_pixels * pixel_spacing[0] * pixel_spacing[1]
            features.append(
                FeatureValue(
                    feature_id="shape_area_physical",
                    feature_name="Area (physical)",
                    family=FeatureFamily.SHAPE_2D,
                    value=float(physical_area),
                    unit="mm²",
                    description="2D 形状特征 - 面积（物理）",
                )
            )

        # 计算周长
        perimeter = self._compute_perimeter(roi_mask)
        features.append(
            FeatureValue(
                feature_id="shape_perimeter",
                feature_name="Perimeter",
                family=FeatureFamily.SHAPE_2D,
                value=float(perimeter),
                description="2D 形状特征 - 周长",
            )
        )

        # 计算圆度
        if perimeter > 0:
            circularity = 4 * np.pi * area_pixels / (perimeter**2)
            features.append(
                FeatureValue(
                    feature_id="shape_circularity",
                    feature_name="Circularity",
                    family=FeatureFamily.SHAPE_2D,
                    value=float(circularity),
                    description="2D 形状特征 - 圆度",
                )
            )

        # 计算紧凑度
        if perimeter > 0:
            compactness = np.sqrt(4 * area_pixels / np.pi) / perimeter
            features.append(
                FeatureValue(
                    feature_id="shape_compactness",
                    feature_name="Compactness",
                    family=FeatureFamily.SHAPE_2D,
                    value=float(compactness),
                    description="2D 形状特征 - 紧凑度",
                )
            )

        # 计算纵横比
        bbox = self._compute_bounding_box(roi_mask)
        if bbox:
            width = bbox[1] - bbox[0] + 1
            height = bbox[3] - bbox[2] + 1
            if min(width, height) > 0:
                aspect_ratio = max(width, height) / min(width, height)
                features.append(
                    FeatureValue(
                        feature_id="shape_aspect_ratio",
                        feature_name="Aspect Ratio",
                        family=FeatureFamily.SHAPE_2D,
                        value=float(aspect_ratio),
                        description="2D 形状特征 - 纵横比",
                    )
                )

        return features

    def _extract_glcm(self, image: np.ndarray, roi_mask: np.ndarray) -> list[FeatureValue]:
        """提取 GLCM 纹理特征"""
        features = []

        # 计算 GLCM 矩阵
        glcm = self._compute_glcm(image, roi_mask)

        if glcm is None or np.sum(glcm) == 0:
            return features

        # 归一化 GLCM
        glcm_norm = glcm / np.sum(glcm)

        # 计算 GLCM 特征
        # Contrast
        contrast = self._glcm_contrast(glcm_norm)
        features.append(
            FeatureValue(
                feature_id="glcm_contrast",
                feature_name="Contrast",
                family=FeatureFamily.GLCM,
                value=float(contrast),
                description="GLCM 纹理特征 - 对比度",
            )
        )

        # Correlation
        correlation = self._glcm_correlation(glcm_norm)
        features.append(
            FeatureValue(
                feature_id="glcm_correlation",
                feature_name="Correlation",
                family=FeatureFamily.GLCM,
                value=float(correlation),
                description="GLCM 纹理特征 - 相关性",
            )
        )

        # Energy (Angular Second Moment)
        energy = np.sum(glcm_norm**2)
        features.append(
            FeatureValue(
                feature_id="glcm_energy",
                feature_name="Energy",
                family=FeatureFamily.GLCM,
                value=float(energy),
                description="GLCM 纹理特征 - 能量",
            )
        )

        # Homogeneity
        homogeneity = self._glcm_homogeneity(glcm_norm)
        features.append(
            FeatureValue(
                feature_id="glcm_homogeneity",
                feature_name="Homogeneity",
                family=FeatureFamily.GLCM,
                value=float(homogeneity),
                description="GLCM 纹理特征 - 同质性",
            )
        )

        # Entropy
        entropy = -np.sum(glcm_norm * np.log2(glcm_norm + 1e-10))
        features.append(
            FeatureValue(
                feature_id="glcm_entropy",
                feature_name="Entropy",
                family=FeatureFamily.GLCM,
                value=float(entropy),
                description="GLCM 纹理特征 - 熵",
            )
        )

        return features

    def _compute_glcm(
        self, image: np.ndarray, roi_mask: np.ndarray, distance: int = 1, angle: float = 0
    ) -> np.ndarray | None:
        """计算 GLCM 矩阵"""
        roi_image = image.copy()
        roi_image[roi_mask == 0] = 0

        # 获取灰度级数
        max_intensity = int(np.max(roi_image[roi_mask > 0]))
        if max_intensity == 0:
            return None

        # 初始化 GLCM
        glcm = np.zeros((max_intensity + 1, max_intensity + 1), dtype=np.float64)

        # 计算偏移量
        dx = int(np.round(np.cos(angle) * distance))
        dy = int(np.round(np.sin(angle) * distance))

        # 获取 ROI 坐标
        y_coords, x_coords = np.where(roi_mask > 0)

        for y, x in zip(y_coords, x_coords):
            # 邻居坐标
            ny, nx = y + dy, x + dx

            # 检查边界
            if 0 <= ny < image.shape[0] and 0 <= nx < image.shape[1] and roi_mask[ny, nx] > 0:
                i = int(roi_image[y, x])
                j = int(roi_image[ny, nx])
                glcm[i, j] += 1
                glcm[j, i] += 1  # 对称

        return glcm

    def _glcm_contrast(self, glcm: np.ndarray) -> float:
        """计算 GLCM 对比度"""
        n = glcm.shape[0]
        i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        return np.sum(glcm * (i - j) ** 2)

    def _glcm_correlation(self, glcm: np.ndarray) -> float:
        """计算 GLCM 相关性"""
        n = glcm.shape[0]
        i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")

        mu_i = np.sum(i * glcm)
        mu_j = np.sum(j * glcm)
        sigma_i = np.sqrt(np.sum((i - mu_i) ** 2 * glcm))
        sigma_j = np.sqrt(np.sum((j - mu_j) ** 2 * glcm))

        if sigma_i * sigma_j == 0:
            return 0.0

        return np.sum((i - mu_i) * (j - mu_j) * glcm) / (sigma_i * sigma_j)

    def _glcm_homogeneity(self, glcm: np.ndarray) -> float:
        """计算 GLCM 同质性"""
        n = glcm.shape[0]
        i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        return np.sum(glcm / (1 + (i - j) ** 2))

    def _extract_glrlm(self, image: np.ndarray, roi_mask: np.ndarray) -> list[FeatureValue]:
        """提取 GLRLM (Gray Level Run Length Matrix) 纹理特征"""
        features = []

        # 计算 GLRLM 矩阵
        glrlm = self._compute_glrlm(image, roi_mask)

        if glrlm is None or np.sum(glrlm) == 0:
            return features

        # 归一化
        glrlm_norm = glrlm / np.sum(glrlm)

        # 计算特征
        n_g, n_r = glrlm.shape
        _i, j = np.meshgrid(np.arange(n_g), np.arange(n_r), indexing="ij")

        # SRE (Short Run Emphasis)
        sre = np.sum(glrlm_norm / (j + 1) ** 2)
        features.append(
            FeatureValue(
                feature_id="glrlm_sre",
                feature_name="Short Run Emphasis",
                family=FeatureFamily.GLRLM,
                value=float(sre),
                description="GLRLM 纹理特征 - 短游程强调",
            )
        )

        # LRE (Long Run Emphasis)
        lre = np.sum(glrlm_norm * (j + 1) ** 2)
        features.append(
            FeatureValue(
                feature_id="glrlm_lre",
                feature_name="Long Run Emphasis",
                family=FeatureFamily.GLRLM,
                value=float(lre),
                description="GLRLM 纹理特征 - 长游程强调",
            )
        )

        # GLN (Gray Level Non-uniformity)
        gln = np.sum(np.sum(glrlm_norm, axis=1) ** 2)
        features.append(
            FeatureValue(
                feature_id="glrlm_gln",
                feature_name="Gray Level Non-uniformity",
                family=FeatureFamily.GLRLM,
                value=float(gln),
                description="GLRLM 纹理特征 - 灰度非均匀性",
            )
        )

        # RLN (Run Length Non-uniformity)
        rln = np.sum(np.sum(glrlm_norm, axis=0) ** 2)
        features.append(
            FeatureValue(
                feature_id="glrlm_rln",
                feature_name="Run Length Non-uniformity",
                family=FeatureFamily.GLRLM,
                value=float(rln),
                description="GLRLM 纹理特征 - 游程长度非均匀性",
            )
        )

        # RP (Run Percentage)
        total_runs = np.sum(glrlm)
        total_voxels = np.sum(roi_mask > 0)
        rp = total_runs / total_voxels if total_voxels > 0 else 0
        features.append(
            FeatureValue(
                feature_id="glrlm_rp",
                feature_name="Run Percentage",
                family=FeatureFamily.GLRLM,
                value=float(rp),
                description="GLRLM 纹理特征 - 游程百分比",
            )
        )

        return features

    def _compute_glrlm(self, image: np.ndarray, roi_mask: np.ndarray) -> np.ndarray | None:
        """计算 GLRLM 矩阵"""
        roi_image = image.copy()
        roi_image[roi_mask == 0] = 0

        max_intensity = int(np.max(roi_image[roi_mask > 0]))
        if max_intensity == 0:
            return None

        max_run_length = max(image.shape)
        glrlm = np.zeros((max_intensity + 1, max_run_length), dtype=np.float64)

        # 水平方向计算
        for y in range(image.shape[0]):
            run_length = 0
            current_val = 0
            for x in range(image.shape[1]):
                if roi_mask[y, x] > 0:
                    if image[y, x] == current_val and run_length > 0:
                        run_length += 1
                    else:
                        if run_length > 0 and current_val <= max_intensity:
                            glrlm[current_val, run_length - 1] += 1
                        current_val = int(image[y, x])
                        run_length = 1
                else:
                    if run_length > 0 and current_val <= max_intensity:
                        glrlm[current_val, run_length - 1] += 1
                    run_length = 0
                    current_val = 0
            if run_length > 0 and current_val <= max_intensity:
                glrlm[current_val, run_length - 1] += 1

        return glrlm

    def _extract_glszm(self, image: np.ndarray, roi_mask: np.ndarray) -> list[FeatureValue]:
        """提取 GLSZM (Gray Level Size Zone Matrix) 纹理特征"""
        features = []

        # 计算 GLSZM 矩阵
        glszm = self._compute_glszm(image, roi_mask)

        if glszm is None or np.sum(glszm) == 0:
            return features

        # 归一化
        glszm_norm = glszm / np.sum(glszm)

        n_g, n_s = glszm.shape
        _i, j = np.meshgrid(np.arange(n_g), np.arange(n_s), indexing="ij")

        # SAE (Small Area Emphasis)
        sae = np.sum(glszm_norm / (j + 1) ** 2)
        features.append(
            FeatureValue(
                feature_id="glszm_sae",
                feature_name="Small Area Emphasis",
                family=FeatureFamily.GLSZM,
                value=float(sae),
                description="GLSZM 纹理特征 - 小区域强调",
            )
        )

        # LAE (Large Area Emphasis)
        lae = np.sum(glszm_norm * (j + 1) ** 2)
        features.append(
            FeatureValue(
                feature_id="glszm_lae",
                feature_name="Large Area Emphasis",
                family=FeatureFamily.GLSZM,
                value=float(lae),
                description="GLSZM 纹理特征 - 大区域强调",
            )
        )

        # GLN (Gray Level Non-uniformity)
        gln = np.sum(np.sum(glszm_norm, axis=1) ** 2)
        features.append(
            FeatureValue(
                feature_id="glszm_gln",
                feature_name="Gray Level Non-uniformity",
                family=FeatureFamily.GLSZM,
                value=float(gln),
                description="GLSZM 纹理特征 - 灰度非均匀性",
            )
        )

        # ZSN (Zone Size Non-uniformity)
        zsn = np.sum(np.sum(glszm_norm, axis=0) ** 2)
        features.append(
            FeatureValue(
                feature_id="glszm_zsn",
                feature_name="Zone Size Non-uniformity",
                family=FeatureFamily.GLSZM,
                value=float(zsn),
                description="GLSZM 纹理特征 - 区域大小非均匀性",
            )
        )

        return features

    def _compute_glszm(self, image: np.ndarray, roi_mask: np.ndarray) -> np.ndarray | None:
        """计算 GLSZM 矩阵"""
        from scipy import ndimage

        roi_image = image.copy()
        roi_image[roi_mask == 0] = 0

        max_intensity = int(np.max(roi_image[roi_mask > 0]))
        if max_intensity == 0:
            return None

        max_zone_size = np.sum(roi_mask > 0)
        glszm = np.zeros((max_intensity + 1, max_zone_size), dtype=np.float64)

        # 对每个灰度级计算连通区域
        for g in range(max_intensity + 1):
            binary = (image == g) & (roi_mask > 0)
            if np.any(binary):
                labeled, num_features = ndimage.label(binary)
                for region_id in range(1, num_features + 1):
                    zone_size = np.sum(labeled == region_id)
                    if zone_size > 0 and zone_size <= max_zone_size:
                        glszm[g, zone_size - 1] += 1

        return glszm

    def _extract_gldm(self, image: np.ndarray, roi_mask: np.ndarray) -> list[FeatureValue]:
        """提取 GLDM (Gray Level Dependence Matrix) 纹理特征"""
        features = []

        # 计算 GLDM 矩阵
        gldm = self._compute_gldm(image, roi_mask)

        if gldm is None or np.sum(gldm) == 0:
            return features

        # 归一化
        gldm_norm = gldm / np.sum(gldm)

        n_g, n_d = gldm.shape
        _i, j = np.meshgrid(np.arange(n_g), np.arange(n_d), indexing="ij")

        # SDE (Small Dependence Emphasis)
        sde = np.sum(gldm_norm / (j + 1) ** 2)
        features.append(
            FeatureValue(
                feature_id="gldm_sde",
                feature_name="Small Dependence Emphasis",
                family=FeatureFamily.GLDM,
                value=float(sde),
                description="GLDM 纹理特征 - 小依赖强调",
            )
        )

        # LDE (Large Dependence Emphasis)
        lde = np.sum(gldm_norm * (j + 1) ** 2)
        features.append(
            FeatureValue(
                feature_id="gldm_lde",
                feature_name="Large Dependence Emphasis",
                family=FeatureFamily.GLDM,
                value=float(lde),
                description="GLDM 纹理特征 - 大依赖强调",
            )
        )

        # GLN (Gray Level Non-uniformity)
        gln = np.sum(np.sum(gldm_norm, axis=1) ** 2)
        features.append(
            FeatureValue(
                feature_id="gldm_gln",
                feature_name="Gray Level Non-uniformity",
                family=FeatureFamily.GLDM,
                value=float(gln),
                description="GLDM 纹理特征 - 灰度非均匀性",
            )
        )

        # DN (Dependence Non-uniformity)
        dn = np.sum(np.sum(gldm_norm, axis=0) ** 2)
        features.append(
            FeatureValue(
                feature_id="gldm_dn",
                feature_name="Dependence Non-uniformity",
                family=FeatureFamily.GLDM,
                value=float(dn),
                description="GLDM 纹理特征 - 依赖非均匀性",
            )
        )

        return features

    def _compute_gldm(
        self, image: np.ndarray, roi_mask: np.ndarray, alpha: int = 0
    ) -> np.ndarray | None:
        """计算 GLDM 矩阵"""
        roi_image = image.copy()
        roi_image[roi_mask == 0] = 0

        max_intensity = int(np.max(roi_image[roi_mask > 0]))
        if max_intensity == 0:
            return None

        max_dependence = np.sum(roi_mask > 0)
        gldm = np.zeros((max_intensity + 1, max_dependence), dtype=np.float64)

        y_coords, x_coords = np.where(roi_mask > 0)

        for y, x in zip(y_coords, x_coords):
            current_val = int(image[y, x])

            # 计算依赖（相邻像素中灰度值相近的数量）
            dependence = 0
            for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                ny, nx = y + dy, x + dx
                if (
                    0 <= ny < image.shape[0]
                    and 0 <= nx < image.shape[1]
                    and roi_mask[ny, nx] > 0
                    and abs(int(image[ny, nx]) - current_val) <= alpha
                ):
                    dependence += 1

            if dependence > 0 and dependence <= max_dependence:
                gldm[current_val, dependence - 1] += 1

        return gldm

    def _extract_ngtdm(self, image: np.ndarray, roi_mask: np.ndarray) -> list[FeatureValue]:
        """提取 NGTDM (Neighborhood Gray Tone Difference Matrix) 纹理特征"""
        features = []

        # 计算 NGTDM
        ngtdm, _gray_levels = self._compute_ngtdm(image, roi_mask)

        if ngtdm is None or np.sum(ngtdm) == 0:
            return features

        # 计算概率
        total_voxels = np.sum(roi_mask > 0)
        p = ngtdm[:, 0] / total_voxels if total_voxels > 0 else np.zeros_like(ngtdm[:, 0])
        s = ngtdm[:, 1]

        # Coarseness
        valid_mask = p > 0
        if np.any(valid_mask):
            coarseness = 1.0 / (np.sum(p[valid_mask] * s[valid_mask]) + 1e-10)
            features.append(
                FeatureValue(
                    feature_id="ngtdm_coarseness",
                    feature_name="Coarseness",
                    family=FeatureFamily.NGTDM,
                    value=float(coarseness),
                    description="NGTDM 纹理特征 - 粗糙度",
                )
            )

        # Contrast
        if np.any(valid_mask):
            contrast = np.sum(p[valid_mask] ** 2 * s[valid_mask])
            features.append(
                FeatureValue(
                    feature_id="ngtdm_contrast",
                    feature_name="Contrast",
                    family=FeatureFamily.NGTDM,
                    value=float(contrast),
                    description="NGTDM 纹理特征 - 对比度",
                )
            )

        # Busyness
        if np.any(valid_mask):
            numerator = np.sum(p[valid_mask] * s[valid_mask])
            denominator = np.sum(np.abs(np.diff(p[valid_mask] * s[valid_mask])))
            busyness = numerator / (denominator + 1e-10)
            features.append(
                FeatureValue(
                    feature_id="ngtdm_busyness",
                    feature_name="Busyness",
                    family=FeatureFamily.NGTDM,
                    value=float(busyness),
                    description="NGTDM 纹理特征 - 忙碌度",
                )
            )

        return features

    def _compute_ngtdm(self, image: np.ndarray, roi_mask: np.ndarray, distance: int = 1):
        """计算 NGTDM"""
        roi_image = image.copy()
        roi_image[roi_mask == 0] = 0

        max_intensity = int(np.max(roi_image[roi_mask > 0]))
        if max_intensity == 0:
            return None, 0

        # 初始化 NGTDM: [count, sum_of_differences]
        ngtdm = np.zeros((max_intensity + 1, 2), dtype=np.float64)

        y_coords, x_coords = np.where(roi_mask > 0)

        for y, x in zip(y_coords, x_coords):
            current_val = int(image[y, x])

            # 计算邻域平均灰度值
            neighbor_sum = 0
            neighbor_count = 0
            for dy in range(-distance, distance + 1):
                for dx in range(-distance, distance + 1):
                    if dy == 0 and dx == 0:
                        continue
                    ny, nx = y + dy, x + dx
                    if (
                        0 <= ny < image.shape[0]
                        and 0 <= nx < image.shape[1]
                        and roi_mask[ny, nx] > 0
                    ):
                        neighbor_sum += image[ny, nx]
                        neighbor_count += 1

            if neighbor_count > 0:
                neighbor_avg = neighbor_sum / neighbor_count
                ngtdm[current_val, 0] += 1
                ngtdm[current_val, 1] += abs(current_val - neighbor_avg)

        return ngtdm, max_intensity + 1

    def _compute_perimeter(self, roi_mask: np.ndarray) -> float:
        """计算 ROI 周长"""
        # 使用边界检测计算周长
        from scipy import ndimage

        edges = ndimage.binary_erosion(roi_mask, iterations=1)
        boundary = np.logical_xor(roi_mask.astype(bool), edges)
        return float(np.sum(boundary))

    def _compute_bounding_box(self, roi_mask: np.ndarray) -> tuple[int, int, int, int] | None:
        """计算 ROI 边界框"""
        coords = np.where(roi_mask > 0)
        if len(coords[0]) == 0:
            return None
        return (
            int(np.min(coords[1])),  # x_min
            int(np.max(coords[1])),  # x_max
            int(np.min(coords[0])),  # y_min
            int(np.max(coords[0])),  # y_max
        )

    def _compute_skewness(self, values: np.ndarray) -> float:
        """计算偏度"""
        n = len(values)
        if n < 3:
            return 0.0
        mean = np.mean(values)
        std = np.std(values)
        if std == 0:
            return 0.0
        return float(np.mean(((values - mean) / std) ** 3))

    def _compute_kurtosis(self, values: np.ndarray) -> float:
        """计算峰度"""
        n = len(values)
        if n < 4:
            return 0.0
        mean = np.mean(values)
        std = np.std(values)
        if std == 0:
            return 0.0
        return float(np.mean(((values - mean) / std) ** 4) - 3)
