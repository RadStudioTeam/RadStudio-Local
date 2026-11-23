"""M2 质控引擎测试"""

import unittest
from uuid import uuid4

from radstudio.models import (
    DatasetSample,
    QCConfig,
    QCStatus,
)
from radstudio.qc_engine import QCEngine, compute_version_hash


class TestQCEngine(unittest.TestCase):
    """质控引擎测试"""

    def setUp(self):
        """测试前准备"""
        self.engine = QCEngine()
        self.sample_base = {
            "id": uuid4(),
            "dataset_id": uuid4(),
            "sample_index": 1,
            "patient_id": "P001",
            "image_id": "IMG001",
            "image_path": "/data/images/IMG001.dcm",
            "image_hash": "abc123",
            "image_size": 102400,
            "roi_id": "ROI001",
            "roi_path": "/data/rois/ROI001.dcm",
            "roi_hash": "def456",
            "roi_type": "polygon",
            "label_value": 1,
            "label_source": "clinical",
            "metadata": {"PatientID": "P001", "Modality": "US", "BodyPartExamined": "Breast"},
            "created_by": uuid4(),
        }

    def test_missing_label_warning(self):
        """测试缺失标签仅告警，不阻断"""
        config = QCConfig(check_readability=False, check_empty_image=False, check_empty_roi=False)
        engine = QCEngine(config)
        data = {**self.sample_base, "label_value": None, "label_source": None, "metadata": {}}
        sample = DatasetSample(**data)
        status, issues = engine.check_sample(sample)
        self.assertEqual(status, QCStatus.WARNING)
        self.assertTrue(any(i.check_type == "missing_label" for i in issues))

    def test_invalid_label_value_raises(self):
        """测试无效标签值抛出异常"""
        data = {**self.sample_base, "label_value": 2}
        with self.assertRaises(ValueError):
            DatasetSample(**data)

    def test_missing_roi_blocked(self):
        """测试缺失 ROI 被阻断"""
        data = {**self.sample_base, "roi_path": None, "roi_id": None}
        sample = DatasetSample(**data)
        status, issues = self.engine.check_sample(sample)
        self.assertEqual(status, QCStatus.BLOCKED)
        self.assertTrue(any(i.check_type == "empty_roi" for i in issues))

    def test_duplicate_patient_detection(self):
        """测试重复患者检测"""
        sample1 = DatasetSample(**self.sample_base)
        sample2_data = {**self.sample_base, "id": uuid4(), "image_id": "IMG002"}
        sample2 = DatasetSample(**sample2_data)

        issues = self.engine.check_duplicate_patients([sample1, sample2])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].check_type, "duplicate_patient")

    def test_no_duplicate_patients(self):
        """测试无重复患者"""
        sample1 = DatasetSample(**self.sample_base)
        sample2_data = {
            **self.sample_base,
            "id": uuid4(),
            "patient_id": "P002",
            "image_id": "IMG002",
        }
        sample2 = DatasetSample(**sample2_data)

        issues = self.engine.check_duplicate_patients([sample1, sample2])
        self.assertEqual(len(issues), 0)

    def test_custom_config_disable_checks(self):
        """测试自定义配置禁用某些检查"""
        config = QCConfig(check_readability=False, check_empty_roi=False, check_empty_image=False)
        engine = QCEngine(config)

        data = {**self.sample_base, "label_value": None, "label_source": None, "metadata": {}}
        sample = DatasetSample(**data)
        status, issues = engine.check_sample(sample)
        # 缺失标签仅告警；元数据检查默认关闭
        self.assertEqual(status, QCStatus.WARNING)
        self.assertTrue(any(i.check_type == "missing_label" for i in issues))
        self.assertFalse(any(i.check_type == "missing_metadata" for i in issues))
        self.assertFalse(any(i.check_type == "readability" for i in issues))
        self.assertFalse(any(i.check_type == "empty_roi" for i in issues))


class TestHashFunctions(unittest.TestCase):
    """哈希函数测试"""

    def test_compute_version_hash_deterministic(self):
        """测试版本哈希确定性"""
        hashes = ["hash1", "hash2", "hash3"]
        result1 = compute_version_hash(hashes)
        result2 = compute_version_hash(hashes)
        self.assertEqual(result1, result2)

    def test_compute_version_hash_order_independent(self):
        """测试版本哈希与顺序无关"""
        hashes1 = ["hash1", "hash2", "hash3"]
        hashes2 = ["hash3", "hash1", "hash2"]
        result1 = compute_version_hash(hashes1)
        result2 = compute_version_hash(hashes2)
        self.assertEqual(result1, result2)

    def test_compute_version_hash_different_inputs(self):
        """测试不同输入产生不同哈希"""
        hashes1 = ["hash1", "hash2"]
        hashes2 = ["hash3", "hash4"]
        result1 = compute_version_hash(hashes1)
        result2 = compute_version_hash(hashes2)
        self.assertNotEqual(result1, result2)
