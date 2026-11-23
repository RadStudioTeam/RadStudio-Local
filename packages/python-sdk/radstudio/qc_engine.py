"""M2 质控引擎 - 数据质量检查"""

import hashlib
from pathlib import Path
from uuid import UUID

from .models import (
    DatasetSample,
    QCConfig,
    QCIssue,
    QCResult,
    QCSeverity,
    QCStatus,
)


class QCEngine:
    """质控引擎"""

    def __init__(self, config: QCConfig | None = None):
        self.config = config or QCConfig()

    def check_sample(self, sample: DatasetSample) -> tuple[QCStatus, list[QCIssue]]:
        """对单个样本执行所有质控检查"""
        issues: list[QCIssue] = []

        # 图像可读性检查
        if self.config.check_readability:
            issue = self._check_image_readability(sample)
            if issue:
                issues.append(issue)

        # 空图像检查
        if self.config.check_empty_image:
            issue = self._check_empty_image(sample)
            if issue:
                issues.append(issue)

        # 空 ROI 检查
        if self.config.check_empty_roi:
            issue = self._check_empty_roi(sample)
            if issue:
                issues.append(issue)

        # ROI 越界检查
        if self.config.check_out_of_bounds and sample.roi_path:
            issue = self._check_roi_out_of_bounds(sample)
            if issue:
                issues.append(issue)

        # 图像/ROI/标签匹配检查
        if self.config.check_mismatch:
            issue = self._check_mismatch(sample)
            if issue:
                issues.append(issue)

        # 缺失标签检查
        if self.config.check_missing_label:
            issue = self._check_missing_label(sample)
            if issue:
                issues.append(issue)

        # 缺失元数据检查
        if self.config.check_missing_metadata:
            issue = self._check_missing_metadata(sample)
            if issue:
                issues.append(issue)

        # 确定整体状态
        status = self._determine_status(issues)
        return status, issues

    def check_duplicate_patients(self, samples: list[DatasetSample]) -> list[QCIssue]:
        """检查重复患者"""
        issues = []
        patient_map: dict[str, list[UUID]] = {}

        for sample in samples:
            if sample.patient_id:
                if sample.patient_id not in patient_map:
                    patient_map[sample.patient_id] = []
                patient_map[sample.patient_id].append(sample.id)

        for patient_id, sample_ids in patient_map.items():
            if len(sample_ids) > 1:
                issues.append(
                    QCIssue(
                        check_type="duplicate_patient",
                        check_name="重复患者检查",
                        status=QCResult.WARNING,
                        severity=QCSeverity.WARNING,
                        message=f"患者 {patient_id} 有 {len(sample_ids)} 个样本，可能导致数据泄漏",
                        details={
                            "patient_id": patient_id,
                            "sample_ids": [str(sid) for sid in sample_ids],
                        },
                    )
                )

        return issues

    def _check_image_readability(self, sample: DatasetSample) -> QCIssue | None:
        """检查图像文件是否可读"""
        image_path = Path(sample.image_path)

        if not image_path.exists():
            return QCIssue(
                check_type="readability",
                check_name="图像可读性检查",
                status=QCResult.FAILED,
                severity=QCSeverity.ERROR,
                message=f"图像文件不存在: {sample.image_path}",
                details={"path": sample.image_path},
            )

        if image_path.stat().st_size == 0:
            return QCIssue(
                check_type="readability",
                check_name="图像可读性检查",
                status=QCResult.FAILED,
                severity=QCSeverity.ERROR,
                message=f"图像文件为空: {sample.image_path}",
                details={"path": sample.image_path},
            )

        return None

    def _check_empty_image(self, sample: DatasetSample) -> QCIssue | None:
        """检查图像是否为空（全黑或全白）"""
        # 实际实现需要读取图像像素数据
        # 这里只检查文件大小是否过小
        if sample.image_size < 1024:  # 小于 1KB
            return QCIssue(
                check_type="empty_image",
                check_name="空图像检查",
                status=QCResult.WARNING,
                severity=QCSeverity.WARNING,
                message=f"图像文件过小，可能是空图像: {sample.image_size} bytes",
                details={"image_size": sample.image_size},
            )
        return None

    def _check_empty_roi(self, sample: DatasetSample) -> QCIssue | None:
        """检查 ROI 是否为空"""
        if not sample.roi_path:
            return QCIssue(
                check_type="empty_roi",
                check_name="空 ROI 检查",
                status=QCResult.FAILED,
                severity=QCSeverity.ERROR,
                message="样本缺少 ROI 文件",
                details={},
            )

        roi_path = Path(sample.roi_path)
        if not roi_path.exists():
            return QCIssue(
                check_type="empty_roi",
                check_name="空 ROI 检查",
                status=QCResult.FAILED,
                severity=QCSeverity.ERROR,
                message=f"ROI 文件不存在: {sample.roi_path}",
                details={"path": sample.roi_path},
            )

        return None

    def _check_roi_out_of_bounds(self, sample: DatasetSample) -> QCIssue | None:
        """检查 ROI 是否超出图像边界"""
        # 实际实现需要解析 ROI 坐标和图像尺寸
        # 这里返回 None，待实际实现
        return None

    def _check_mismatch(self, sample: DatasetSample) -> QCIssue | None:
        """检查图像/ROI/标签匹配"""
        if sample.roi_id and sample.image_id:
            # 检查 ROI 是否对应正确的图像
            # 实际实现需要解析 ROI 文件中的图像引用
            pass

        return None

    def _label_present(self, sample: DatasetSample) -> bool:
        """标签可通过 label_value、label_source 或元数据提供"""
        if sample.label_value is not None:
            return True
        if sample.label_source:
            return True
        metadata = sample.metadata or {}
        for key in (
            "label",
            "label_value",
            "labelValue",
            "target",
            "y",
            "class",
            "diagnosis",
            "classification",
            "标签",
            "分类",
            "诊断",
        ):
            value = metadata.get(key)
            if value is not None and str(value) != "":
                return True
        return False

    def _check_missing_label(self, sample: DatasetSample) -> QCIssue | None:
        """检查标签是否缺失（缺失仅告警，不阻断）"""
        if not self._label_present(sample):
            return QCIssue(
                check_type="missing_label",
                check_name="缺失标签检查",
                status=QCResult.WARNING,
                severity=QCSeverity.WARNING,
                message="样本缺少临床标签，训练前需补充（支持标签表或元数据提供）",
                details={},
            )
        return None

    def _check_missing_metadata(self, sample: DatasetSample) -> QCIssue | None:
        """检查关键元数据是否缺失"""
        missing_tags = []

        metadata = sample.metadata or {}
        for tag in self.config.required_dicom_tags:
            if tag not in metadata or metadata[tag] is None:
                missing_tags.append(tag)

        if missing_tags:
            return QCIssue(
                check_type="missing_metadata",
                check_name="缺失元数据检查",
                status=QCResult.WARNING,
                severity=QCSeverity.WARNING,
                message=f"缺少关键元数据: {', '.join(missing_tags)}",
                details={"missing_tags": missing_tags},
            )

        return None

    def _determine_status(self, issues: list[QCIssue]) -> QCStatus:
        """根据问题列表确定样本状态"""
        if not issues:
            return QCStatus.ACCEPTED

        has_critical = any(i.severity == QCSeverity.CRITICAL for i in issues)
        has_error = any(i.severity == QCSeverity.ERROR for i in issues)
        has_warning = any(i.severity == QCSeverity.WARNING for i in issues)

        if has_critical or has_error:
            return QCStatus.BLOCKED
        elif has_warning:
            return QCStatus.WARNING
        else:
            return QCStatus.ACCEPTED


def compute_file_hash(file_path: str) -> str:
    """计算文件 SHA-256 哈希"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def compute_version_hash(sample_hashes: list[str]) -> str:
    """计算版本哈希（所有样本哈希的有序组合）"""
    sorted_hashes = sorted(sample_hashes)
    combined = "".join(sorted_hashes)
    return hashlib.sha256(combined.encode()).hexdigest()
