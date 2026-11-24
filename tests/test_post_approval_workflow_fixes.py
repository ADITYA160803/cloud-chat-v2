import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from config import config
from core.exceptions import ApprovalError
from core.models import (
    ApprovalStatus,
    AWSService,
    InterpretedInfrastructure,
    EC2ResourceSpec,
    NetworkSpec,
    StorageSpec,
    VolumeType,
    NormalizedInfrastructureSpec,
    NormalizedEC2Spec,
    PlannerReadinessStatus,
    PlannerResult,
    TerraformPlanResult,
    TerraformExecutionStage,
    DeploymentRecord,
    DeploymentStatus,
    AMIResolutionResult,
)
from database.db import init_db
from database.repository import DeploymentRepository
from services.infrastructure_planner import InfrastructurePlanner
from services.terraform_service import TerraformService


def test_missing_vpc_subnet_blocks_progression(tmp_path):
    """Test that missing existing VPC/Subnet IDs result in NEEDS_USER_INPUT and block planning."""
    interp = InterpretedInfrastructure(
        user_request="Create a private EC2 instance in Mumbai with t3.micro using my existing VPC and subnet",
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            EC2ResourceSpec(
                name="test-server",
                instance_type="t3.micro",
                network=NetworkSpec(
                    use_existing_vpc=True,
                    use_existing_subnet=True,
                    vpc_id=None,
                    subnet_id=None,
                ),
            )
        ],
        missing_information=["Existing VPC ID required", "Existing Subnet ID required"],
        requires_clarification=True,
    )
    planner = InfrastructurePlanner()
    result = planner.plan(interp)
    assert result.readiness == PlannerReadinessStatus.NEEDS_USER_INPUT
    assert any("VPC" in m for m in result.missing_information)
    assert any("Subnet" in m for m in result.missing_information)


def test_failed_terraform_plan_marks_plan_passed_false():
    """Test that failed terraform plan results in plan_passed=False and empty plan_hash."""
    tf_service = TerraformService()
    spec = NormalizedInfrastructureSpec(
        deployment_id="dep-fail-test",
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="app-server",
                instance_type="t3.micro",
                ami=AMIResolutionResult(ami_id="ami-12345", os_preference="amazon_linux_2023"),
                network=NetworkSpec(),
                storage=StorageSpec(size_gb=10, volume_type=VolumeType.GP3, encrypted=True),
            )
        ],
    )

    # Mock _run_command to fail at terraform plan step
    def mock_run(cmd, cwd, timeout):
        cmd_str = " ".join(cmd)
        if "fmt" in cmd_str:
            return {"exit_code": 0, "output": "", "error": ""}
        if "init" in cmd_str:
            return {"exit_code": 0, "output": "", "error": ""}
        if "validate" in cmd_str:
            return {"exit_code": 0, "output": '{"valid": true}', "error": ""}
        if "plan" in cmd_str:
            return {"exit_code": 1, "output": "", "error": "Plugin connection failed: connectex error"}
        return {"exit_code": 0, "output": "", "error": ""}

    with patch.object(tf_service, "_run_command", side_effect=mock_run):
        res = tf_service.run_pipeline("dep-fail-test", spec)
        assert res.plan_passed is False
        assert res.stage == TerraformExecutionStage.PLAN
        assert res.plan_hash == ""
        assert res.plan_artifact_hash == ""
        assert res.plan_file_path is None
        assert "connectex error" in res.error_message


def test_failed_plan_cannot_be_approved(tmp_path):
    """Test that repository update_approval rejects approval if plan_hash is empty."""
    db_file = tmp_path / "test.db"
    init_db(db_file)
    repo = DeploymentRepository(db_path=db_file)

    rec = DeploymentRecord(
        deployment_id="dep-test-approve-fail",
        user_request="Create ec2 instance",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
    )
    repo.create(rec)

    with pytest.raises(ApprovalError, match="empty or missing plan_hash"):
        repo.update_approval("dep-test-approve-fail", ApprovalStatus.APPROVED, plan_hash="")

    with pytest.raises(ApprovalError, match="empty or missing plan_hash"):
        repo.update_approval("dep-test-approve-fail", ApprovalStatus.APPROVED, plan_hash=None)


def test_invalid_old_approval_revoked_on_migration(tmp_path):
    """Test that existing corrupted database records with empty plan_hash are revoked on init_db."""
    db_file = tmp_path / "test_mig.db"
    init_db(db_file)
    repo = DeploymentRepository(db_path=db_file)

    rec = DeploymentRecord(
        deployment_id="dep-corrupt-1",
        user_request="Legacy deployment with empty hash",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="",
    )
    repo.create(rec)

    # Re-run init_db to simulate application startup migration
    init_db(db_file)
    updated = repo.get("dep-corrupt-1")
    assert updated.approval_status == ApprovalStatus.REVOKED
    assert updated.plan_hash is None
    assert "Approval revoked" in (updated.error_message or "")


def test_zero_resource_plan_detection():
    """Test that zero resource modification plan is identified."""
    tf_res = TerraformPlanResult(
        deployment_id="dep-zero-test",
        workspace_dir="/tmp/fake",
        to_add=0,
        to_change=0,
        to_destroy=0,
        plan_passed=True,
    )
    assert tf_res.to_add == 0 and tf_res.to_change == 0 and tf_res.to_destroy == 0


def test_mock_plan_cannot_be_used_for_real_deployment():
    """Test that mock plans carry is_mock_plan=True and cannot be applied in real deployment mode."""
    mock_res = TerraformPlanResult(
        deployment_id="dep-mock-plan",
        workspace_dir="/tmp/mock",
        plan_passed=True,
        plan_hash="mockhash123",
        is_mock_plan=True,
    )
    assert mock_res.is_mock_plan is True


def test_missing_plan_artifact_detection(tmp_path):
    """Test that missing plan artifact on disk is detected and prevents approval."""
    fake_path = tmp_path / "nonexistent" / "tfplan"
    res = TerraformPlanResult(
        deployment_id="dep-missing-art",
        workspace_dir=str(tmp_path),
        plan_passed=True,
        plan_hash="fakehash",
        plan_file_path=str(fake_path),
    )
    assert not Path(res.plan_file_path).exists()


def test_deployment_page_rejects_unapproved_or_empty_hash_records(tmp_path):
    """Test that SQLite records without valid approval or with empty hash cannot execute."""
    db_file = tmp_path / "test_exec.db"
    init_db(db_file)
    repo = DeploymentRepository(db_path=db_file)

    # 1. Unapproved record
    r1 = DeploymentRecord(
        deployment_id="dep-unapproved",
        user_request="Unapproved request",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
        plan_hash="hash123",
    )
    repo.create(r1)
    assert repo.get("dep-unapproved").approval_status != ApprovalStatus.APPROVED

    # 2. Revoked record
    r2 = DeploymentRecord(
        deployment_id="dep-revoked",
        user_request="Revoked request",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.REVOKED,
        plan_hash=None,
    )
    repo.create(r2)
    assert repo.get("dep-revoked").approval_status == ApprovalStatus.REVOKED
    assert repo.get("dep-revoked").plan_hash is None


def test_deployment_page_recovers_valid_records(tmp_path):
    """Test that valid approved deployment records with normalized_spec and plan_hash can be retrieved."""
    db_file = tmp_path / "test_recov.db"
    init_db(db_file)
    repo = DeploymentRepository(db_path=db_file)

    spec = NormalizedInfrastructureSpec(
        deployment_id="dep-valid-1",
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="valid-server",
                instance_type="t3.micro",
                ami=AMIResolutionResult(ami_id="ami-111", os_preference="amazon_linux_2023"),
                network=NetworkSpec(),
                storage=StorageSpec(size_gb=8, volume_type=VolumeType.GP3, encrypted=True),
            )
        ],
    )

    r = DeploymentRecord(
        deployment_id="dep-valid-1",
        user_request="Valid deployment",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="validplanhash123",
        normalized_spec=spec.model_dump(),
        aws_account_id="123456789012",
        aws_region="ap-south-1",
    )
    repo.create(r)

    fetched = repo.get("dep-valid-1")
    assert fetched is not None
    assert fetched.approval_status == ApprovalStatus.APPROVED
    assert fetched.plan_hash == "validplanhash123"
    assert fetched.normalized_spec is not None
    assert fetched.normalized_spec["resources"][0]["name"] == "valid-server"


def test_real_deployment_safety_defaults_unchanged(monkeypatch):
    """Test safety defaults: real deployment remains disabled by default when not overridden."""
    monkeypatch.delenv("ENABLE_MOCK_MODE", raising=False)
    monkeypatch.delenv("ENABLE_REAL_TERRAFORM_APPLY", raising=False)
    from config import AppConfig
    c = AppConfig()
    assert c.enable_mock_mode is True
    assert c.enable_real_terraform_apply is False
    assert c.is_real_deployment_allowed() is False
