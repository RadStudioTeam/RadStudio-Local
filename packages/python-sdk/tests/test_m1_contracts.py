"""M1 契约测试"""

from radstudio.models import Edge, Node, Workflow


class TestWorkflowValidation:
    """工作流校验测试"""

    def test_valid_workflow(self):
        """测试有效工作流"""
        workflow = Workflow(
            name="Test Workflow",
            nodes=[
                Node(id="input1", type="DataInput", name="Input"),
                Node(id="process1", type="Processing", name="Process"),
            ],
            edges=[
                Edge(source="input1", target="process1"),
            ],
        )
        errors = workflow.validate_edges()
        assert len(errors) == 0

    def test_unknown_source_node(self):
        """测试未知源节点引用"""
        workflow = Workflow(
            name="Test Workflow",
            nodes=[
                Node(id="process1", type="Processing", name="Process"),
            ],
            edges=[
                Edge(source="unknown", target="process1"),
            ],
        )
        errors = workflow.validate_edges()
        assert len(errors) == 1
        assert "unknown source node" in errors[0]

    def test_unknown_target_node(self):
        """测试未知目标节点引用"""
        workflow = Workflow(
            name="Test Workflow",
            nodes=[
                Node(id="input1", type="DataInput", name="Input"),
            ],
            edges=[
                Edge(source="input1", target="unknown"),
            ],
        )
        errors = workflow.validate_edges()
        assert len(errors) == 1
        assert "unknown target node" in errors[0]

    def test_multiple_errors(self):
        """测试多个错误"""
        workflow = Workflow(
            name="Test Workflow",
            nodes=[Node(id="node1", type="Test", name="Test")],
            edges=[
                Edge(source="unknown1", target="node1"),
                Edge(source="node1", target="unknown2"),
            ],
        )
        errors = workflow.validate_edges()
        assert len(errors) == 2


class TestWorkflowImmutability:
    """工作流不可变性测试"""

    def test_version_hash_uniqueness(self):
        """测试版本哈希唯一性"""
        from uuid import uuid4

        from radstudio.models import WorkflowVersion

        workflow = Workflow(
            name="Test",
            nodes=[Node(id="n1", type="T", name="T")],
            edges=[],
        )

        v1 = WorkflowVersion(
            project_id=uuid4(),
            version_hash="abc123",
            ir=workflow,
            created_by=uuid4(),
        )

        v2 = WorkflowVersion(
            project_id=v1.project_id,
            version_hash="def456",
            ir=workflow,
            created_by=v1.created_by,
        )

        assert v1.version_hash != v2.version_hash
        assert v1.id != v2.id
