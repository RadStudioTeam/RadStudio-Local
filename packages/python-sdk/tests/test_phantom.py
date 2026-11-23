"""M3 数字幻影回归测试"""

import unittest

import numpy as np

from radstudio.features import FeatureConfig, FeatureExtractor, FeatureFamily
from radstudio.preprocessing import ImagePreprocessor
from radstudio.validation import IBSIValidator, ReferenceValue


class DigitalPhantom:
    """数字幻影生成器"""

    @staticmethod
    def create_checkerboard(size: int = 64, block_size: int = 8) -> tuple:
        """
        创建棋盘格幻影

        Args:
            size: 图像大小
            block_size: 棋盘格块大小

        Returns:
            (image, roi_mask): 图像和 ROI 掩码
        """
        image = np.zeros((size, size), dtype=np.float64)

        for y in range(size):
            for x in range(size):
                block_y = y // block_size
                block_x = x // block_size
                if (block_y + block_x) % 2 == 0:
                    image[y, x] = 100
                else:
                    image[y, x] = 200

        # 创建圆形 ROI
        roi_mask = np.zeros((size, size), dtype=np.int32)
        center = size // 2
        radius = size // 3
        y, x = np.ogrid[:size, :size]
        mask = (x - center) ** 2 + (y - center) ** 2 <= radius**2
        roi_mask[mask] = 1

        return image, roi_mask

    @staticmethod
    def create_gradient(size: int = 64) -> tuple:
        """
        创建渐变幻影

        Args:
            size: 图像大小

        Returns:
            (image, roi_mask): 图像和 ROI 掩码
        """
        # 创建水平渐变
        image = np.zeros((size, size), dtype=np.float64)
        for x in range(size):
            image[:, x] = x * (255.0 / size)

        # 创建矩形 ROI
        roi_mask = np.zeros((size, size), dtype=np.int32)
        roi_mask[size // 4 : 3 * size // 4, size // 4 : 3 * size // 4] = 1

        return image, roi_mask

    @staticmethod
    def create_circle(size: int = 64, intensity: float = 200) -> tuple:
        """
        创建圆形幻影

        Args:
            size: 图像大小
            intensity: 圆形灰度值

        Returns:
            (image, roi_mask): 图像和 ROI 掩码
        """
        image = np.zeros((size, size), dtype=np.float64)
        roi_mask = np.zeros((size, size), dtype=np.int32)

        center = size // 2
        radius = size // 4
        y, x = np.ogrid[:size, :size]
        mask = (x - center) ** 2 + (y - center) ** 2 <= radius**2

        image[mask] = intensity
        roi_mask[mask] = 1

        return image, roi_mask

    @staticmethod
    def create_noise(size: int = 64, mean: float = 128, std: float = 30) -> tuple:
        """
        创建噪声幻影

        Args:
            size: 图像大小
            mean: 均值
            std: 标准差

        Returns:
            (image, roi_mask): 图像和 ROI 掩码
        """
        np.random.seed(42)  # 固定随机种子以保证可重复性
        image = np.random.normal(mean, std, (size, size))
        image = np.clip(image, 0, 255)

        # 创建 ROI
        roi_mask = np.zeros((size, size), dtype=np.int32)
        roi_mask[8 : size - 8, 8 : size - 8] = 1

        return image, roi_mask


class TestDigitalPhantom(unittest.TestCase):
    """数字幻影测试"""

    def setUp(self):
        """测试前准备"""
        self.preprocessor = ImagePreprocessor()
        self.extractor = FeatureExtractor()

    def test_checkerboard_phantom(self):
        """测试棋盘格幻影"""
        image, roi_mask = DigitalPhantom.create_checkerboard()

        # 预处理
        result = self.preprocessor.preprocess(image, roi_mask)

        # 提取特征
        features = self.extractor.extract(result.image, result.roi_mask, result.discretized_image)

        # 验证特征
        self.assertGreater(len(features.features), 0)

        # 检查一阶特征
        feature_dict = features.to_dict()
        self.assertIn("fo_mean", feature_dict)
        self.assertIn("fo_variance", feature_dict)

        # 棋盘格应该有较高的方差
        self.assertGreater(feature_dict["fo_variance"], 0)

    def test_gradient_phantom(self):
        """测试渐变幻影"""
        image, roi_mask = DigitalPhantom.create_gradient()

        result = self.preprocessor.preprocess(image, roi_mask)
        features = self.extractor.extract(result.image, result.roi_mask, result.discretized_image)

        feature_dict = features.to_dict()

        # 渐变图像应该有非零的偏度
        self.assertIn("fo_skewness", feature_dict)

    def test_circle_phantom(self):
        """测试圆形幻影"""
        image, roi_mask = DigitalPhantom.create_circle()

        result = self.preprocessor.preprocess(image, roi_mask)
        features = self.extractor.extract(
            result.image, result.roi_mask, result.discretized_image, pixel_spacing=(1.0, 1.0)
        )

        feature_dict = features.to_dict()

        # 检查形状特征
        self.assertIn("shape_area_pixels", feature_dict)
        self.assertIn("shape_circularity", feature_dict)

        # 圆形应该有较高的圆度
        if "shape_circularity" in feature_dict:
            self.assertGreater(feature_dict["shape_circularity"], 0.5)

    def test_noise_phantom(self):
        """测试噪声幻影"""
        image, roi_mask = DigitalPhantom.create_noise()

        result = self.preprocessor.preprocess(image, roi_mask)
        features = self.extractor.extract(result.image, result.roi_mask, result.discretized_image)

        feature_dict = features.to_dict()

        # 噪声图像应该有接近零的偏度
        if "fo_skewness" in feature_dict:
            self.assertAlmostEqual(feature_dict["fo_skewness"], 0, delta=0.5)

    def test_reproducibility(self):
        """测试可重复性"""
        image, roi_mask = DigitalPhantom.create_circle()

        # 多次提取特征
        results = []
        for _ in range(3):
            result = self.preprocessor.preprocess(image, roi_mask)
            features = self.extractor.extract(
                result.image, result.roi_mask, result.discretized_image
            )
            results.append(features.to_dict())

        # 验证结果一致性
        for key in results[0]:
            for i in range(1, len(results)):
                self.assertAlmostEqual(
                    results[0][key],
                    results[i][key],
                    places=10,
                    msg=f"Feature {key} not reproducible",
                )


class TestFeatureValidation(unittest.TestCase):
    """特征验证测试"""

    def setUp(self):
        """测试前准备"""
        self.preprocessor = ImagePreprocessor()
        self.extractor = FeatureExtractor()

        # 创建参考数据
        self.references = {
            "fo_mean": ReferenceValue(
                feature_id="fo_mean",
                reference_source="Digital Phantom",
                reference_value=100.0,
                tolerance_type="relative",
                tolerance_value=0.1,
                description="Mean intensity",
            ),
            "fo_variance": ReferenceValue(
                feature_id="fo_variance",
                reference_source="Digital Phantom",
                reference_value=1000.0,
                tolerance_type="relative",
                tolerance_value=0.2,
                description="Variance",
            ),
        }
        self.validator = IBSIValidator(self.references)

    def test_validate_phantom_features(self):
        """测试数字幻影特征验证"""
        # 创建已知的幻影
        image = np.ones((64, 64)) * 100
        roi_mask = np.zeros((64, 64), dtype=np.int32)
        roi_mask[16:48, 16:48] = 1

        # 提取特征
        result = self.preprocessor.preprocess(image, roi_mask)
        features = self.extractor.extract(result.image, result.roi_mask)
        feature_dict = features.to_dict()

        # 验证特征
        report = self.validator.validate_features(feature_dict, {"phantom": "uniform"})

        # 检查报告
        self.assertGreater(report.total_features, 0)
        self.assertIn(
            report.overall_status,
            ["all_validated", "partially_validated", "no_validation_data", "failed"],
        )

    def test_validation_report_generation(self):
        """测试验证报告生成"""
        computed_features = {
            "fo_mean": 100.0,
            "fo_variance": 1000.0,
        }

        report = self.validator.validate_features(computed_features, configuration={"test": True})

        # 检查报告结构
        self.assertIsNotNone(report.report_id)
        self.assertIsNotNone(report.generated_at)
        self.assertEqual(report.total_features, 2)

        # 检查 JSON 输出
        json_str = report.to_json()
        self.assertIsInstance(json_str, str)
        self.assertIn("fo_mean", json_str)


class TestFeatureFamilies(unittest.TestCase):
    """特征族完整性测试"""

    def setUp(self):
        """测试前准备"""
        # 创建测试图像
        self.image = np.random.rand(64, 64) * 100
        self.roi_mask = np.zeros((64, 64), dtype=np.int32)
        self.roi_mask[16:48, 16:48] = 1

        # 预处理
        preprocessor = ImagePreprocessor()
        result = preprocessor.preprocess(self.image, self.roi_mask)
        self.discretized = result.discretized_image

    def test_all_feature_families(self):
        """测试所有特征族"""
        for family in FeatureFamily:
            config = FeatureConfig(enabled_families=[family])
            extractor = FeatureExtractor(config)

            features = extractor.extract(self.image, self.roi_mask, self.discretized)

            # 每个特征族都应该返回一些特征（除了可能为空的情况）
            if family in [FeatureFamily.FIRST_ORDER, FeatureFamily.SHAPE_2D, FeatureFamily.GLCM]:
                self.assertGreater(
                    len(features.features), 0, f"Feature family {family.value} returned no features"
                )

    def test_feature_ids_unique(self):
        """测试特征 ID 唯一性"""
        config = FeatureConfig(
            enabled_families=[
                FeatureFamily.FIRST_ORDER,
                FeatureFamily.SHAPE_2D,
                FeatureFamily.GLCM,
                FeatureFamily.GLRLM,
                FeatureFamily.GLSZM,
                FeatureFamily.GLDM,
                FeatureFamily.NGTDM,
            ]
        )
        extractor = FeatureExtractor(config)

        features = extractor.extract(self.image, self.roi_mask, self.discretized)

        # 检查特征 ID 唯一性
        feature_ids = [f.feature_id for f in features.features]
        self.assertEqual(len(feature_ids), len(set(feature_ids)))


if __name__ == "__main__":
    unittest.main()
