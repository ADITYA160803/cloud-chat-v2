import os
import uuid
import pytest
from database.db import init_db
from database.repository import DeploymentRepository
from core.models import DeploymentRecord, DeploymentStatus, ApprovalStatus, VerificationStatus


@pytest.fixture
def temp_db(tmp_path):
    """Fixture providing an isolated temporary SQLite database."""
    test_db_path = tmp_path / "test_cloud_chat.db"
    init_db(test_db_path)
    return test_db_path


def test_database_init_and_crud(temp_db):
    """Test full CRUD operations on SQLite deployment repository."""
    repo = DeploymentRepository(db_path=temp_db)

    deployment_id = str(uuid.uuid4())
    record = DeploymentRecord(
        deployment_id=deployment_id,
        user_request="Create an EC2 instance in us-east-1",
        service="ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
        parsed_intent={"service": "ec2", "region": "us-east-1"},
    )

    # 1. Create
    created = repo.create(record)
    assert created.deployment_id == deployment_id

    # 2. Get
    fetched = repo.get(deployment_id)
    assert fetched is not None
    assert fetched.deployment_id == deployment_id
    assert fetched.user_request == "Create an EC2 instance in us-east-1"
    assert fetched.parsed_intent == {"service": "ec2", "region": "us-east-1"}
    assert fetched.status == DeploymentStatus.DRAFT

    # 3. Update status
    updated = repo.update_status(deployment_id, DeploymentStatus.IN_PROGRESS)
    assert updated is True
    fetched_after = repo.get(deployment_id)
    assert fetched_after.status == DeploymentStatus.IN_PROGRESS

    # 4. Update approval
    updated_approval = repo.update_approval(
        deployment_id,
        ApprovalStatus.APPROVED,
        plan_hash="abc123hash"
    )
    assert updated_approval is True
    fetched_approved = repo.get(deployment_id)
    assert fetched_approved.approval_status == ApprovalStatus.APPROVED
    assert fetched_approved.plan_hash == "abc123hash"

    # 5. List recent
    recent = repo.list_recent(limit=10)
    assert len(recent) == 1
    assert recent[0].deployment_id == deployment_id

    # 6. Summary count
    summary = repo.count_summary()
    assert summary["total"] == 1
    assert summary["in_progress"] == 1
    assert summary["successful"] == 0
