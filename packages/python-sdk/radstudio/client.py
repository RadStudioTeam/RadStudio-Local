"""RadStudio API 客户端"""

import httpx

from .models import Project, Workflow, WorkflowVersion


class RadStudioClient:
    """RadStudio API 客户端"""

    def __init__(self, base_url: str = "http://localhost:8080", api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = {}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def health_check(self) -> dict:
        """健康检查"""
        with httpx.Client() as client:
            resp = client.get(f"{self.base_url}/healthz")
            resp.raise_for_status()
            return resp.json()

    def get_version(self) -> dict:
        """获取 API 版本"""
        with httpx.Client() as client:
            resp = client.get(f"{self.base_url}/api/v1/version")
            resp.raise_for_status()
            return resp.json()

    def create_project(self, name: str, description: str | None = None) -> Project:
        """创建项目（最小管理单元）"""
        with httpx.Client() as client:
            resp = client.post(
                f"{self.base_url}/api/v1/projects",
                json={"name": name, "description": description},
                headers=self.headers,
            )
            resp.raise_for_status()
            return Project(**resp.json())

    def list_projects(self) -> list[Project]:
        """列出项目"""
        with httpx.Client() as client:
            resp = client.get(f"{self.base_url}/api/v1/projects", headers=self.headers)
            resp.raise_for_status()
            return [Project(**item) for item in resp.json()]

    def get_project(self, project_id: str) -> Project:
        """获取项目"""
        with httpx.Client() as client:
            resp = client.get(f"{self.base_url}/api/v1/projects/{project_id}", headers=self.headers)
            resp.raise_for_status()
            return Project(**resp.json())

    def validate_workflow(self, workflow: Workflow) -> dict:
        """校验工作流"""
        with httpx.Client() as client:
            resp = client.post(
                f"{self.base_url}/api/v1/workflows/validate",
                json=workflow.model_dump(),
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    def create_workflow_version(self, project_id: str, workflow: Workflow) -> WorkflowVersion:
        """创建工作流版本"""
        with httpx.Client() as client:
            resp = client.put(
                f"{self.base_url}/api/v1/versions/workflow/{project_id}",
                json=workflow.model_dump(),
                headers=self.headers,
            )
            resp.raise_for_status()
            return WorkflowVersion(**resp.json())
