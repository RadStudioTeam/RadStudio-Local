"""RadStudio Python SDK - 医学影像组学低代码科研平台"""

__version__ = "0.2.0"

# M3 特征提取模块
from .features import (
    FeatureConfig,
    FeatureExtractionResult,
    FeatureExtractor,
    FeatureFamily,
    FeatureValue,
)
from .models import (
    # M2 模型
    Dataset,
    DatasetSample,
    # 枚举
    DatasetStatus,
    DatasetVersion,
    DatasetVersionStatus,
    ExperimentRun,
    FileUpload,
    # 项目
    Project,
    ProjectStatus,
    QCConfig,
    QCIssue,
    QCReport,
    QCResult,
    QCSeverity,
    QCStatus,
    UploadStatus,
    # M1 模型
    Workflow,
    WorkflowVersion,
)

# M3 预处理模块
from .preprocessing import (
    ImagePreprocessor,
    PreprocessingConfig,
    PreprocessingResult,
    ROIVerifier,
)

# M3 验证模块
from .validation import (
    FeatureValidationReport,
    IBSIValidator,
    ReferenceDataManager,
    ReferenceValue,
    ValidationStatus,
)


def __getattr__(name: str):
    """延迟导入可选依赖的模块"""
    if name == "RadStudioClient":
        from .client import RadStudioClient

        return RadStudioClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # M1/M2 模型
    "Dataset",
    "DatasetSample",
    "DatasetStatus",
    "DatasetVersion",
    "DatasetVersionStatus",
    "ExperimentRun",
    "FeatureConfig",
    "FeatureExtractionResult",
    # M3 特征提取
    "FeatureExtractor",
    "FeatureFamily",
    "FeatureValidationReport",
    "FeatureValue",
    "FileUpload",
    # M3 验证
    "IBSIValidator",
    # M3 预处理
    "ImagePreprocessor",
    "PreprocessingConfig",
    "PreprocessingResult",
    "Project",
    "ProjectStatus",
    "QCConfig",
    "QCIssue",
    "QCReport",
    "QCResult",
    "QCSeverity",
    "QCStatus",
    "ROIVerifier",
    "RadStudioClient",
    "ReferenceDataManager",
    "ReferenceValue",
    "UploadStatus",
    "ValidationStatus",
    "Workflow",
    "WorkflowVersion",
]
