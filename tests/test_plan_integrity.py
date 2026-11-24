import pytest
from config import AppConfig
from core.exceptions import ApprovalError
from core.models import (
    ApprovalStatus,
    DeploymentRecord,
    DeploymentStatus,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    StorageSpec,
    VolumeType,
)
from database.db import init_db
from database.repository import DeploymentRepository
from services.deployment_service import DeploymentService
from services.terraform_service import TerraformService


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test_integrity.db"
    init_db(db_path)
    return db_path


def test_plan_hash_deterministic_and_sensitive_to_change():
    """Verify that plan hash is deterministic and changes upon any HCL modification."""
    tf_service = TerraformService()
    spec = NormalizedInfrastructureSpec(
        region="us-east-1",
        resources=[
            NormalizedEC2Spec(
                name="srv-1",
                instance_type="t3.micro",
                storage=StorageSpec(size_gb=10, volume_type=VolumeType.GP3),
            )
        ],
    )
    files1 = tf_service.generate_configuration(spec)
    hash1 = tf_service.calculate_plan_hash(files1)
    hash1_repeat = tf_service.calculate_plan_hash(files1)
    assert hash1 == hash1_repeat

    # Modify configuration
    spec.resources[0].instance_type = "t3.small"
    files2 = tf_service.generate_configuration(spec)
    hash2 = tf_service.calculate_plan_hash(files2)

    assert hash1 != hash2


def test_deployment_fails_without_explicit_approval(temp_db):
    """Verify deployment execution rejects unapproved plans."""
    repo = DeploymentRepository(db_path=temp_db)
    dep_id = "dep-test-unapproved"
    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Create EC2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
        plan_hash="hash123",
    )
    repo.create(record)

    service = DeploymentService(repository=repo)
    spec = NormalizedInfrastructureSpec(region="us-east-1", resources=[])

    with pytest.raises(ApprovalError) as exc_info:
        service.execute_deployment(dep_id, spec, approved_plan_hash="hash123")
    assert "explicit operator approval" in str(exc_info.value).lower()


def test_deployment_fails_if_plan_hash_mismatches(temp_db):
    """Verify deployment execution rejects execution if plan hash changed after approval."""
    repo = DeploymentRepository(db_path=temp_db)
    dep_id = "dep-test-mismatch"
    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Create EC2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="original-hash-111",
    )
    repo.create(record)

    service = DeploymentService(repository=repo)
    spec = NormalizedInfrastructureSpec(region="us-east-1", resources=[])

    # Attempt to deploy with modified/tampered plan hash
    with pytest.raises(ApprovalError) as exc_info:
        service.execute_deployment(dep_id, spec, approved_plan_hash="modified-hash-222")
    assert "integrity verification failed" in str(exc_info.value).lower()


def test_mock_deployment_cannot_execute_real_aws(temp_db, monkeypatch):
    """Verify mock deployment produces mock identifiers and does not invoke real AWS apply."""
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: False)
    repo = DeploymentRepository(db_path=temp_db)
    dep_id = "dep-test-mock"
    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Create EC2 in us-east-1",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-mock-verified",
    )
    repo.create(record)

    service = DeploymentService(repository=repo)
    spec = NormalizedInfrastructureSpec(
        region="us-east-1",
        resources=[NormalizedEC2Spec(instance_type="t3.micro")],
    )

    result = service.execute_deployment(dep_id, spec, approved_plan_hash="hash-mock-verified")

    assert result["status"] == "COMPLETED (MOCK)"
    assert "i-" in result["instance_id"]
    # Verify in DB
    updated = repo.get(dep_id)
    assert updated.status == DeploymentStatus.COMPLETED
    assert updated.aws_resource_ids is not None


def test_independent_tfplan_binary_hashing():
    """Verify that the saved binary tfplan artifact is independently hashed."""
    tf_service = TerraformService()
    raw_plan_bytes = b"\x00\x01\x02\x03TERRAFORMPLANBINARYMOCK"
    artifact_hash1 = tf_service.calculate_plan_artifact_hash(raw_plan_bytes)
    artifact_hash2 = tf_service.calculate_plan_artifact_hash(raw_plan_bytes)

    assert len(artifact_hash1) == 64
    assert artifact_hash1 == artifact_hash2

    # Modified plan bytes produces a different hash
    tampered_bytes = b"\x00\x01\x02\x03TERRAFORMPLANBINARYTAMPERED"
    tampered_hash = tf_service.calculate_plan_artifact_hash(tampered_bytes)
    assert artifact_hash1 != tampered_hash
    assert tf_service.calculate_plan_artifact_hash(b"") == ""
