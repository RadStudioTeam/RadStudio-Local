"""M3 特征验证与报告生成模块"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class ValidationStatus(str, Enum):
    """验证状态"""

    VALIDATED = "validated"  # 已验证通过
    SUPPORTED_NOT_VALIDATED = "supported_not_validated"  # 已实现但未验证
    NOT_SUPPORTED = "not_supported"  # 未实现
    FAILED = "failed"  # 验证失败


@dataclass
class ReferenceValue:
    """参考值"""

    feature_id: str
    reference_source: str  # 参考来源
    reference_value: float
    tolerance_type: str  # "absolute" 或 "relative"
    tolerance_value: float
    description: str = ""


@dataclass
class ValidationResult:
    """验证结果"""

    feature_id: str
    feature_name: str
    computed_value: float
    reference_value: float | None
    tolerance: float | None
    status: ValidationStatus
    difference: float | None = None
    within_tolerance: bool | None = None
    message: str = ""


@dataclass
class FeatureValidationReport:
    """特征验证报告"""

    # 元数据
    report_id: str
    generated_at: datetime
    engine_version: str
    configuration: dict[str, Any]

    # 验证结果
    results: list[ValidationResult]

    # 统计
    total_features: int
    validated_count: int
    supported_not_validated_count: int
    not_supported_count: int
    failed_count: int

    # 总结
    overall_status: str
    recommendations: list[str]

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at.isoformat(),
            "engine_version": self.engine_version,
            "configuration": self.configuration,
            "results": [
                {
                    "feature_id": r.feature_id,
                    "feature_name": r.feature_name,
                    "computed_value": r.computed_value,
                    "reference_value": r.reference_value,
                    "tolerance": r.tolerance,
                    "status": r.status.value,
                    "difference": r.difference,
                    "within_tolerance": r.within_tolerance,
                    "message": r.message,
                }
                for r in self.results
            ],
            "summary": {
                "total_features": self.total_features,
                "validated_count": self.validated_count,
                "supported_not_validated_count": self.supported_not_validated_count,
                "not_supported_count": self.not_supported_count,
                "failed_count": self.failed_count,
            },
            "overall_status": self.overall_status,
            "recommendations": self.recommendations,
        }

    def to_json(self) -> str:
        """转换为 JSON"""
        return json.dumps(self.to_dict(), indent=2)


class IBSIValidator:
    """IBSI 特征验证器"""

    def __init__(self, reference_data: dict[str, ReferenceValue] | None = None):
        self.reference_data = reference_data or {}
        self.engine_version = "0.1.0"

    def validate_features(
        self, computed_features: dict[str, float], configuration: dict[str, Any]
    ) -> FeatureValidationReport:
        """
        验证特征值

        Args:
            computed_features: 计算得到的特征值 {feature_id: value}
            configuration: 特征提取配置

        Returns:
            FeatureValidationReport: 验证报告
        """
        results = []

        for feature_id, computed_value in computed_features.items():
            result = self._validate_single_feature(feature_id, computed_value)
            results.append(result)

        # 统计
        validated_count = sum(1 for r in results if r.status == ValidationStatus.VALIDATED)
        supported_not_validated_count = sum(
            1 for r in results if r.status == ValidationStatus.SUPPORTED_NOT_VALIDATED
        )
        not_supported_count = sum(1 for r in results if r.status == ValidationStatus.NOT_SUPPORTED)
        failed_count = sum(1 for r in results if r.status == ValidationStatus.FAILED)

        # 确定整体状态
        if failed_count > 0:
            overall_status = "failed"
        elif validated_count == len(results):
            overall_status = "all_validated"
        elif validated_count > 0:
            overall_status = "partially_validated"
        else:
            overall_status = "no_validation_data"

        # 生成建议
        recommendations = self._generate_recommendations(results, overall_status)

        return FeatureValidationReport(
            report_id=f"report_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}",
            generated_at=datetime.now(UTC),
            engine_version=self.engine_version,
            configuration=configuration,
            results=results,
            total_features=len(results),
            validated_count=validated_count,
            supported_not_validated_count=supported_not_validated_count,
            not_supported_count=not_supported_count,
            failed_count=failed_count,
            overall_status=overall_status,
            recommendations=recommendations,
        )

    def _validate_single_feature(self, feature_id: str, computed_value: float) -> ValidationResult:
        """验证单个特征"""
        # 检查是否有参考数据
        if feature_id not in self.reference_data:
            return ValidationResult(
                feature_id=feature_id,
                feature_name=self._get_feature_name(feature_id),
                computed_value=computed_value,
                reference_value=None,
                tolerance=None,
                status=ValidationStatus.SUPPORTED_NOT_VALIDATED,
                message="No reference data available",
            )

        ref = self.reference_data[feature_id]

        # 计算差异
        difference = abs(computed_value - ref.reference_value)

        # 检查是否在容差范围内
        if ref.tolerance_type == "absolute":
            within_tolerance = difference <= ref.tolerance_value
            tolerance = ref.tolerance_value
        elif ref.tolerance_type == "relative":
            if ref.reference_value != 0:
                relative_diff = difference / abs(ref.reference_value)
                within_tolerance = relative_diff <= ref.tolerance_value
                tolerance = ref.tolerance_value
            else:
                within_tolerance = difference == 0
                tolerance = 0
        else:
            within_tolerance = False
            tolerance = None

        # 确定状态
        if within_tolerance:
            status = ValidationStatus.VALIDATED
            message = "Feature validated successfully"
        else:
            status = ValidationStatus.FAILED
            message = f"Feature validation failed: difference {difference:.6f} exceeds tolerance"

        return ValidationResult(
            feature_id=feature_id,
            feature_name=self._get_feature_name(feature_id),
            computed_value=computed_value,
            reference_value=ref.reference_value,
            tolerance=tolerance,
            status=status,
            difference=difference,
            within_tolerance=within_tolerance,
            message=message,
        )

    def _get_feature_name(self, feature_id: str) -> str:
        """获取特征名称"""
        # 特征 ID 到名称的映射
        name_map = {
            "fo_energy": "Energy",
            "fo_entropy": "Entropy",
            "fo_mean": "Mean",
            "fo_median": "Median",
            "fo_variance": "Variance",
            "fo_std": "Standard Deviation",
            "fo_skewness": "Skewness",
            "fo_kurtosis": "Kurtosis",
            "fo_min": "Minimum",
            "fo_max": "Maximum",
            "fo_range": "Range",
            "shape_area_pixels": "Area (pixels)",
            "shape_area_physical": "Area (physical)",
            "shape_perimeter": "Perimeter",
            "shape_circularity": "Circularity",
            "shape_compactness": "Compactness",
            "shape_aspect_ratio": "Aspect Ratio",
            "glcm_contrast": "GLCM Contrast",
            "glcm_correlation": "GLCM Correlation",
            "glcm_energy": "GLCM Energy",
            "glcm_homogeneity": "GLCM Homogeneity",
            "glcm_entropy": "GLCM Entropy",
        }
        return name_map.get(feature_id, feature_id)

    def _generate_recommendations(
        self, results: list[ValidationResult], overall_status: str
    ) -> list[str]:
        """生成建议"""
        recommendations = []

        if overall_status == "failed":
            recommendations.append("存在验证失败的特征，请检查实现")

        failed_features = [r for r in results if r.status == ValidationStatus.FAILED]
        if failed_features:
            feature_ids = [r.feature_id for r in failed_features]
            recommendations.append(f"以下特征验证失败: {', '.join(feature_ids)}")

        not_validated = [r for r in results if r.status == ValidationStatus.SUPPORTED_NOT_VALIDATED]
        if not_validated:
            recommendations.append(f"有 {len(not_validated)} 个特征缺少参考数据，无法验证")

        if overall_status == "all_validated":
            recommendations.append("所有特征验证通过")

        return recommendations


class ReferenceDataManager:
    """参考数据管理器"""

    @staticmethod
    def load_from_json(json_path: str) -> dict[str, ReferenceValue]:
        """从 JSON 文件加载参考数据"""
        with open(json_path, "r") as f:
            data = json.load(f)

        references = {}
        for feature_id, ref_data in data.items():
            references[feature_id] = ReferenceValue(
                feature_id=feature_id,
                reference_source=ref_data.get("source", ""),
                reference_value=ref_data["value"],
                tolerance_type=ref_data.get("tolerance_type", "absolute"),
                tolerance_value=ref_data.get("tolerance_value", 0.001),
                description=ref_data.get("description", ""),
            )

        return references

    @staticmethod
    def get_default_references() -> dict[str, ReferenceValue]:
        """获取默认参考数据（用于测试）"""
        return {
            "fo_energy": ReferenceValue(
                feature_id="fo_energy",
                reference_source="IBSI 1 Digital Phantom",
                reference_value=1000000.0,
                tolerance_type="relative",
                tolerance_value=0.01,
                description="Energy feature reference value",
            ),
            "fo_entropy": ReferenceValue(
                feature_id="fo_entropy",
                reference_source="IBSI 1 Digital Phantom",
                reference_value=5.0,
                tolerance_type="relative",
                tolerance_value=0.01,
                description="Entropy feature reference value",
            ),
        }
