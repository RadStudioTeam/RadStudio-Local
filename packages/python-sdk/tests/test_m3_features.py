"""M3 IBSI 特征提取测试"""

import unittest

import numpy as np

from radstudio.features import (
    FeatureConfig,
    FeatureExtractor,
    FeatureFamily,
)
from radstudio.preprocessing import (
    BinMethod,
    ImagePreprocessor,
    PreprocessingConfig,
    ROIVerifier,
)
from radstudio.validation import (
    IBSIValidator,
    ReferenceDataManager,
)


class TestImagePreprocessor(unittest.TestCase):
    """图像预处理器测试"""

    def setUp(self):
        """测试前准备"""
        self.preprocessor = ImagePreprocessor()

        # 创建测试图像和 ROI
        self.image = np.random.rand(64, 64) * 100
        self.roi_mask = np.zeros((64, 64), dtype=np.int32)
        self.roi_mask[16:48, 16:48] = 1  # 中心 32x32 区域

    def test_preprocess_valid_input(self):
        """测试有效输入预处理"""
        result = self.preprocessor.preprocess(self.image, self.roi_mask)

        self.assertEqual(result.image.shape, (64, 64))
        self.assertEqual(result.roi_mask.shape, (64, 64))
        self.assertGreater(result.intensity_stats["voxel_count"], 0)

    def test_preprocess_with_pixel_spacing(self):
        """测试带像素间距的预处理"""
        result = self.preprocessor.preprocess(self.image, self.roi_mask, pixel_spacing=(0.5, 0.5))

        self.assertEqual(result.metadata["pixel_spacing_x"], 0.5)
        self.assertEqual(result.metadata["pixel_spacing_y"], 0.5)

    def test_preprocess_invalid_shape(self):
        """测试无效形状输入"""
        bad_roi = np.zeros((32, 32), dtype=np.int32)

        with self.assertRaises(ValueError):
            self.preprocessor.preprocess(self.image, bad_roi)

    def test_preprocess_empty_roi(self):
        """测试空 ROI 输入"""
        empty_roi = np.zeros((64, 64), dtype=np.int32)

        with self.assertRaises(ValueError):
            self.preprocessor.preprocess(self.image, empty_roi)

    def test_discretize_fixed_width(self):
        """测试固定 bin 宽度离散化"""
        config = PreprocessingConfig(bin_method=BinMethod.FIXED_BIN_WIDTH, bin_width=10)
        preprocessor = ImagePreprocessor(config)

        result = preprocessor.preprocess(self.image, self.roi_mask)

        # 离散化后的值应该是正整数
        roi_discretized = result.discretized_image[self.roi_mask > 0]
        self.assertTrue(np.all(roi_discretized > 0))


class TestROIVerifier(unittest.TestCase):
    """ROI 校验器测试"""

    def test_verify_valid_roi(self):
        """测试有效 ROI 验证"""
        image = np.random.rand(64, 64)
        roi = np.zeros((64, 64), dtype=np.int32)
        roi[16:48, 16:48] = 1

        result = ROIVerifier.verify_roi_alignment(image, roi)

        self.assertTrue(result["aligned"])
        self.assertEqual(len(result["issues"]), 0)

    def test_verify_shape_mismatch(self):
        """测试形状不匹配"""
        image = np.random.rand(64, 64)
        roi = np.zeros((32, 32), dtype=np.int32)

        result = ROIVerifier.verify_roi_alignment(image, roi)

        self.assertFalse(result["aligned"])
        self.assertGreater(len(result["issues"]), 0)

    def test_verify_empty_roi(self):
        """测试空 ROI"""
        image = np.random.rand(64, 64)
        roi = np.zeros((64, 64), dtype=np.int32)

        result = ROIVerifier.verify_roi_alignment(image, roi)

        self.assertFalse(result["aligned"])

    def test_compute_overlap(self):
        """测试 ROI 重叠计算"""
        roi1 = np.zeros((64, 64), dtype=np.int32)
        roi1[16:48, 16:48] = 1

        roi2 = np.zeros((64, 64), dtype=np.int32)
        roi2[32:64, 32:64] = 1

        overlap = ROIVerifier.compute_roi_overlap(roi1, roi2)

        self.assertGreater(overlap, 0)
        self.assertLess(overlap, 1)


class TestFeatureExtractor(unittest.TestCase):
    """特征提取器测试"""

    def setUp(self):
        """测试前准备"""
        self.extractor = FeatureExtractor()

        # 创建测试图像
        self.image = np.random.rand(64, 64) * 100
        self.roi_mask = np.zeros((64, 64), dtype=np.int32)
        self.roi_mask[16:48, 16:48] = 1

    def test_extract_first_order(self):
        """测试一阶特征提取"""
        config = FeatureConfig(enabled_families=[FeatureFamily.FIRST_ORDER])
        extractor = FeatureExtractor(config)

        result = extractor.extract(self.image, self.roi_mask)

        self.assertGreater(len(result.features), 0)

        # 检查特征 ID
        feature_ids = [f.feature_id for f in result.features]
        self.assertIn("fo_energy", feature_ids)
        self.assertIn("fo_entropy", feature_ids)
        self.assertIn("fo_mean", feature_ids)

    def test_extract_shape_2d(self):
        """测试 2D 形状特征提取"""
        config = FeatureConfig(enabled_families=[FeatureFamily.SHAPE_2D])
        extractor = FeatureExtractor(config)

        result = extractor.extract(self.image, self.roi_mask, pixel_spacing=(0.5, 0.5))

        self.assertGreater(len(result.features), 0)

        # 检查特征
        feature_ids = [f.feature_id for f in result.features]
        self.assertIn("shape_area_pixels", feature_ids)

    def test_extract_glcm(self):
        """测试 GLCM 特征提取"""
        # 使用离散化图像
        discretized = (self.image / 10).astype(int)

        config = FeatureConfig(enabled_families=[FeatureFamily.GLCM])
        extractor = FeatureExtractor(config)

        result = extractor.extract(discretized, self.roi_mask)

        self.assertGreater(len(result.features), 0)

    def test_extract_all_families(self):
        """测试提取所有特征族"""
        config = FeatureConfig(
            enabled_families=[
                FeatureFamily.FIRST_ORDER,
                FeatureFamily.SHAPE_2D,
            ]
        )
        extractor = FeatureExtractor(config)

        result = extractor.extract(self.image, self.roi_mask)

        self.assertGreater(len(result.features), 0)

    def test_to_dict(self):
        """测试转换为字典"""
        result = self.extractor.extract(self.image, self.roi_mask)

        result_dict = result.to_dict()

        self.assertIsInstance(result_dict, dict)
        self.assertGreater(len(result_dict), 0)

    def test_to_table(self):
        """测试转换为表格"""
        result = self.extractor.extract(self.image, self.roi_mask)

        result_table = result.to_table()

        self.assertIsInstance(result_table, list)
        self.assertGreater(len(result_table), 0)

        # 检查表格结构
        first_row = result_table[0]
        self.assertIn("feature_id", first_row)
        self.assertIn("feature_name", first_row)
        self.assertIn("value", first_row)


class TestIBSIValidator(unittest.TestCase):
    """IBSI 验证器测试"""

    def setUp(self):
        """测试前准备"""
        self.references = ReferenceDataManager.get_default_references()
        self.validator = IBSIValidator(self.references)

    def test_validate_with_reference(self):
        """测试有参考数据的验证"""
        computed_features = {
            "fo_energy": 1000000.0,
            "fo_entropy": 5.0,
        }

        report = self.validator.validate_features(
            computed_features, configuration={"bin_width": 25}
        )

        self.assertEqual(report.total_features, 2)
        self.assertEqual(report.validated_count, 2)
        self.assertEqual(report.overall_status, "all_validated")

    def test_validate_without_reference(self):
        """测试无参考数据的验证"""
        computed_features = {
            "fo_mean": 50.0,
            "fo_std": 10.0,
        }

        report = self.validator.validate_features(
            computed_features, configuration={"bin_width": 25}
        )

        self.assertEqual(report.total_features, 2)
        self.assertEqual(report.supported_not_validated_count, 2)

    def test_validate_failed(self):
        """测试验证失败"""
        computed_features = {
            "fo_energy": 500000.0,  # 与参考值差异太大
        }

        report = self.validator.validate_features(
            computed_features, configuration={"bin_width": 25}
        )

        self.assertEqual(report.total_features, 1)
        self.assertEqual(report.failed_count, 1)
        self.assertEqual(report.overall_status, "failed")

    def test_report_to_json(self):
        """测试报告转 JSON"""
        computed_features = {"fo_energy": 1000000.0}

        report = self.validator.validate_features(computed_features, configuration={})

        json_str = report.to_json()

        self.assertIsInstance(json_str, str)
        self.assertIn("fo_energy", json_str)


if __name__ == "__main__":
    unittest.main()
