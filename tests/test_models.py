import pytest
from core.models import DeploymentRecord, DeploymentStatus, ApprovalStatus, VerificationStatus


def test_deployment_record_defaults():
    """Verify default values of DeploymentRecord model."""
    record = DeploymentRecord(
        deployment_id="test-dep-001",
        user_request="Deploy t3.micro in ap-south-1"
    )
    assert record.service == "ec2"
    assert record.status == DeploymentStatus.DRAFT
    assert record.approval_status == ApprovalStatus.PENDING
    assert record.verification_status == VerificationStatus.NOT_CHECKED
    assert record.aws_resource_ids == []
    assert record.plan_hash is None
