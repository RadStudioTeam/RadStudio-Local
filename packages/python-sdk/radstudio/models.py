"""RadStudio 数据模型"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class NodeInput(BaseModel):
    """节点输入定义"""

    name: str
    type: str
    required: bool = True


class NodeOutput(BaseModel):
    """节点输出定义"""

    name: str
    type: str


class Node(BaseModel):
    """工作流节点"""

    id: str
    type: str
    name: str
    parameters: dict = Field(default_factory=dict)
    inputs: list[NodeInput] = Field(default_factory=list)
    outputs: list[NodeOutput] = Field(default_factory=list)


class Edge(BaseModel):
    """工作流边"""

    source: str
    target: str
    source_output: str = "default"
    target_input: str = "default"


class Workflow(BaseModel):
    """工作流定义"""

    name: str
    description: str | None = None
    nodes: list[Node]
    edges: list[Edge]

    def validate_edges(self) -> list[str]:
        """校验边引用的节点是否存在"""
        errors = []
        node_ids = {node.id for node in self.nodes}

        for edge in self.edges:
            if edge.source not in node_ids:
                errors.append(f"Edge references unknown source node: {edge.source}")
            if edge.target not in node_ids:
                errors.append(f"Edge references unknown target node: {edge.target}")

        return errors


class ProjectStatus(str, Enum):
    """项目状态"""

    ACTIVE = "active"
    ARCHIVED = "archived"


class Project(BaseModel):
    """项目（最小管理单元）"""

    id: UUID = Field(default_factory=uuid4)
    name: str
    description: str | None = None
    status: ProjectStatus = ProjectStatus.ACTIVE
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: UUID


class WorkflowVersion(BaseModel):
    """工作流版本（不可变）"""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    version_hash: str
    ir: Workflow
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: UUID


class ExperimentRun(BaseModel):
    """实验运行"""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    workflow_version_id: UUID
    status: str = "pending"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: UUID


class RunEvent(BaseModel):
    """运行事件"""

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    event_id: str
    sequence_no: int
    event_type: str
    payload: dict
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Artifact(BaseModel):
    """Artifact 定义"""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    run_id: UUID
    artifact_type: str
    manifest: dict
    storage_path: str
    file_hash: str
    file_size: int
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: UUID


# ============================================================
# M2 数据集与质控模型
# ============================================================


class DatasetStatus(str, Enum):
    """数据集状态"""

    DRAFT = "draft"
    UPLOADING = "uploading"
    VALIDATING = "validating"
    FROZEN = "frozen"
    ARCHIVED = "archived"


class DatasetVersionStatus(str, Enum):
    """数据集版本状态"""

    PENDING = "pending"
    VALIDATING = "validating"
    VALIDATED = "validated"
    FROZEN = "frozen"


class QCStatus(str, Enum):
    """质控状态"""

    PENDING = "pending"
    ACCEPTED = "accepted"
    WARNING = "warning"
    BLOCKED = "blocked"
    EXCLUDED = "excluded"


class QCResult(str, Enum):
    """质控检查结果"""

    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


class QCSeverity(str, Enum):
    """质控问题严重程度"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class UploadStatus(str, Enum):
    """上传状态"""

    UPLOADING = "uploading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class QCIssue(BaseModel):
    """质控问题"""

    check_type: str
    check_name: str
    status: QCResult
    severity: QCSeverity
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DatasetSample(BaseModel):
    """数据样本"""

    id: UUID = Field(default_factory=uuid4)
    dataset_id: UUID
    sample_index: int
    patient_id: str | None = None
    image_id: str
    image_path: str
    image_hash: str
    image_size: int
    roi_id: str | None = None
    roi_path: str | None = None
    roi_hash: str | None = None
    roi_type: str | None = None
    label_value: int | None = None
    label_source: str | None = None
    qc_status: QCStatus = QCStatus.PENDING
    qc_issues: list[QCIssue] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: UUID

    @field_validator("label_value")
    @classmethod
    def validate_label_value(cls, v: int | None) -> int | None:
        if v is not None and v not in (0, 1):
            raise ValueError("label_value must be 0 or 1")
        return v


class DatasetVersion(BaseModel):
    """数据集版本（不可变）"""

    id: UUID = Field(default_factory=uuid4)
    dataset_id: UUID
    version_number: int
    version_hash: str
    status: DatasetVersionStatus = DatasetVersionStatus.PENDING
    sample_count: int = 0
    accepted_count: int = 0
    warning_count: int = 0
    blocked_count: int = 0
    excluded_count: int = 0
    frozen_at: datetime | None = None
    frozen_by: UUID | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Dataset(BaseModel):
    """数据集"""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    name: str
    description: str | None = None
    status: DatasetStatus = DatasetStatus.DRAFT
    sample_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: UUID


class QCReport(BaseModel):
    """质控报告"""

    id: UUID = Field(default_factory=uuid4)
    report_type: str = "full"
    summary: dict[str, Any]
    issues: list[QCIssue] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    generated_by: UUID | None = None


class FileUpload(BaseModel):
    """文件上传记录"""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    dataset_id: UUID | None = None
    file_path: str
    file_hash: str
    file_size: int
    file_type: str
    status: UploadStatus = UploadStatus.UPLOADING
    retry_count: int = 0
    error_message: str | None = None
    uploaded_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: UUID


class QCConfig(BaseModel):
    """质控配置"""

    check_readability: bool = True
    check_empty_image: bool = True
    check_empty_roi: bool = True
    check_out_of_bounds: bool = True
    check_mismatch: bool = True
    check_missing_label: bool = True
    check_duplicate_patient: bool = True
    check_missing_metadata: bool = False
    image_min_size: int = 64
    image_max_size: int = 4096
    roi_min_area_pixels: int = 10
    required_dicom_tags: list[str] = Field(
        default_factory=lambda: ["PatientID", "Modality", "BodyPartExamined"]
    )
