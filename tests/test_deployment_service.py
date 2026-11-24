import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from config import AppConfig
from core.exceptions import ApprovalError, ConfigurationError, TerraformExecutionError
from core.models import (
    ApprovalStatus,
    AWSAuthMethod,
    AWSIdentity,
    AWSService,
    DeploymentRecord,
    DeploymentStatus,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
)
from database.db import init_db
from database.repository import DeploymentRepository
from services.aws_auth_service import AWSAuthService, AWSCredentialsContainer
from services.deployment_service import DeploymentService
from services.terraform_service import TerraformService


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_cloud_chat.db"
    init_db(db_file)
    return db_file


@pytest.fixture
def repository(temp_db):
    return DeploymentRepository(temp_db)


@pytest.fixture
def sample_spec():
    return NormalizedInfrastructureSpec(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[NormalizedEC2Spec(name="test-server", instance_type="t3.micro", region="ap-south-1")],
        user_request="Deploy ec2",
    )


def test_execute_deployment_missing_record(repository, sample_spec):
    """Fails if deployment record does not exist in SQLite."""
    service = DeploymentService(repository=repository)
    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment("non-existent-id", sample_spec, "hash123")
    assert "not found in database" in str(exc.value)


def test_execute_deployment_unapproved(repository, sample_spec):
    """Fails if deployment record is pending approval."""
    record = DeploymentRecord(
        deployment_id="dep-pending",
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.PENDING,
        plan_hash="hash123",
    )
    repository.create(record)

    service = DeploymentService(repository=repository)
    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment("dep-pending", sample_spec, "hash123")
    assert "not received explicit operator approval" in str(exc.value)


def test_execute_deployment_tampered_plan_hash(repository, sample_spec):
    """Fails if plan hash passed does not match approved hash stored in database."""
    record = DeploymentRecord(
        deployment_id="dep-tampered",
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="original-hash-111",
    )
    repository.create(record)

    service = DeploymentService(repository=repository)
    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment("dep-tampered", sample_spec, "tampered-hash-222")
    assert "Plan integrity verification failed" in str(exc.value)


def test_execute_deployment_concurrency_lock(repository, sample_spec):
    """Fails if deployment is already in progress."""
    record = DeploymentRecord(
        deployment_id="dep-in-prog",
        user_request="Deploy ec2",
        status=DeploymentStatus.IN_PROGRESS,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash123",
    )
    repository.create(record)

    service = DeploymentService(repository=repository)
    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment("dep-in-prog", sample_spec, "hash123")
    assert "already in progress" in str(exc.value)


def test_execute_deployment_rejects_re_execution_of_completed(repository, sample_spec):
    """Fails if deployment has already reached terminal status COMPLETED."""
    record = DeploymentRecord(
        deployment_id="dep-already-done",
        user_request="Deploy ec2",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-done",
    )
    repository.create(record)

    service = DeploymentService(repository=repository)
    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment("dep-already-done", sample_spec, "hash-done")
    assert "cannot be re-executed" in str(exc.value)


def test_execute_deployment_rejects_re_execution_of_partially_deployed(repository, sample_spec):
    """Fails if deployment has already reached terminal status PARTIALLY_DEPLOYED."""
    record = DeploymentRecord(
        deployment_id="dep-already-partial",
        user_request="Deploy ec2",
        status=DeploymentStatus.PARTIALLY_DEPLOYED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-partial",
    )
    repository.create(record)

    service = DeploymentService(repository=repository)
    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment("dep-already-partial", sample_spec, "hash-partial")
    assert "cannot be re-executed" in str(exc.value)


def test_mock_deployment_execution_and_audit(repository, sample_spec, monkeypatch):
    """Verifies mock deployment runs safely and writes complete SQLite audit trail."""
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: False)
    record = DeploymentRecord(
        deployment_id="dep-mock-01",
        user_request="Create t3.micro in Mumbai",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-mock-123",
        aws_account_id="123456789012",
        aws_region="ap-south-1",
    )
    repository.create(record)

    service = DeploymentService(repository=repository)
    result = service.execute_deployment("dep-mock-01", sample_spec, "hash-mock-123")

    assert result["status"] == "COMPLETED (MOCK)"
    assert result["execution_mode"] == "mock"
    assert "instance_id" in result
    assert result["verification"]["overall_status"] == "VERIFIED"

    # Verify SQLite Audit Persistence
    updated = repository.get("dep-mock-01")
    assert updated.status == DeploymentStatus.COMPLETED
    assert updated.execution_mode == "mock"
    assert updated.aws_account_id == "123456789012"
    assert updated.aws_region == "ap-south-1"
    assert updated.terraform_exit_code == 0
    assert updated.execution_started_at is not None
    assert updated.execution_completed_at is not None
    assert len(updated.aws_resource_ids) >= 1
    assert "MOCK DEPLOYMENT LOG" in updated.execution_logs


def test_real_deployment_requires_both_safety_flags(repository, sample_spec, monkeypatch):
    """Real deployment must route to mock mode if ENABLE_REAL_TERRAFORM_APPLY is False or mock mode is True."""
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: False)
    record = DeploymentRecord(
        deployment_id="dep-safe-01",
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-safe",
    )
    repository.create(record)

    service = DeploymentService(repository=repository)
    result = service.execute_deployment("dep-safe-01", sample_spec, "hash-safe")
    assert result["execution_mode"] == "mock"


def test_real_deployment_unauthenticated_session(repository, sample_spec, monkeypatch):
    """If real deployment is deliberately enabled but session is unauthenticated, raise error."""
    record = DeploymentRecord(
        deployment_id="dep-real-01",
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-real",
        aws_account_id="999888777666",
        aws_region="us-east-1",
    )
    repository.create(record)

    auth_service = AWSAuthService()
    service = DeploymentService(repository=repository, auth_service=auth_service)

    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment("dep-real-01", sample_spec, "hash-real")
    assert "No active authenticated AWS session found" in str(exc.value)


def test_real_deployment_account_mismatch_revokes_approval(repository, sample_spec, monkeypatch):
    """If approved account differs from active session account, revoke approval and reject."""
    dep_id = "dep-mismatch-acct"
    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-mismatch",
        aws_account_id="111111111111",
        aws_region="ap-south-1",
    )
    repository.create(record)

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123")
    identity = AWSIdentity(
        account_id="222222222222",
        arn="arn:aws:iam::222222222222:user/dev",
        user_id="AID222",
        region="ap-south-1",
    )
    auth_service.set_active_credentials(creds, identity)

    service = DeploymentService(repository=repository, auth_service=auth_service)
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment(dep_id, sample_spec, "hash-mismatch")
    assert "Security mismatch: Plan was approved for AWS Account" in str(exc.value)

    # Verify approval status is explicitly REVOKED in SQLite
    updated = repository.get(dep_id)
    assert updated.approval_status == ApprovalStatus.REVOKED


def test_real_deployment_region_mismatch_revokes_approval(repository, sample_spec, monkeypatch):
    """If approved region differs from active session region, revoke approval and reject."""
    dep_id = "dep-mismatch-reg"
    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-reg",
        aws_account_id="111111111111",
        aws_region="ap-south-1",
    )
    repository.create(record)

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="us-west-2")
    identity = AWSIdentity(
        account_id="111111111111",
        arn="arn:aws:iam::111111111111:user/dev",
        user_id="AID111",
        region="us-west-2",
    )
    auth_service.set_active_credentials(creds, identity)

    service = DeploymentService(repository=repository, auth_service=auth_service)
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment(dep_id, sample_spec, "hash-reg")
    assert "Security mismatch: Plan was approved for Region" in str(exc.value)

    updated = repository.get(dep_id)
    assert updated.approval_status == ApprovalStatus.REVOKED


def test_pre_apply_recalculation_detects_disk_file_tampering(repository, sample_spec, tmp_path, monkeypatch):
    """If configuration files on disk are modified after approval, pre-apply recalculation detects it and revokes approval."""
    dep_id = "dep-tamper-disk"
    tf_service = TerraformService(repository=repository, workspace_root=tmp_path)
    tf_files = tf_service.generate_configuration(sample_spec)
    ws_dir = tf_service.write_workspace_files(dep_id, tf_files)

    plan_bytes = b"mock_tfplan_binary_artifact"
    (ws_dir / "tfplan").write_bytes(plan_bytes)
    genuine_plan_hash = tf_service.calculate_plan_hash(tf_files, plan_bytes)

    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash=genuine_plan_hash,
        aws_account_id="111111111111",
        aws_region="ap-south-1",
    )
    repository.create(record)

    # TAMPER with main.tf on disk after approval
    (ws_dir / "main.tf").write_text(tf_files["main.tf"] + "\n# MALICIOUS TAMPERING", encoding="utf-8")

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA11112222EXAMPLE", secret_access_key="secret", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dep", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    service = DeploymentService(repository=repository, auth_service=auth_service, terraform_service=tf_service, terraform_binary="terraform")
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment(dep_id, sample_spec, genuine_plan_hash)
    assert "Plan integrity verification failed" in str(exc.value)
    assert "altered since approval" in str(exc.value)

    # Verify approval status is explicitly revoked in database
    updated = repository.get(dep_id)
    assert updated.approval_status == ApprovalStatus.REVOKED


def test_pre_apply_recalculation_detects_binary_plan_tampering(repository, sample_spec, tmp_path, monkeypatch):
    """If the binary tfplan artifact is swapped or modified on disk after approval, recalculation revokes approval."""
    dep_id = "dep-tamper-plan"
    tf_service = TerraformService(repository=repository, workspace_root=tmp_path)
    tf_files = tf_service.generate_configuration(sample_spec)
    ws_dir = tf_service.write_workspace_files(dep_id, tf_files)

    plan_bytes = b"mock_tfplan_binary_artifact"
    (ws_dir / "tfplan").write_bytes(plan_bytes)
    genuine_plan_hash = tf_service.calculate_plan_hash(tf_files, plan_bytes)

    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash=genuine_plan_hash,
        aws_account_id="111111111111",
        aws_region="ap-south-1",
    )
    repository.create(record)

    # TAMPER with binary tfplan on disk
    (ws_dir / "tfplan").write_bytes(b"REPLACED_DIFFERENT_BINARY_PLAN_ARTIFACT")

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA11112222EXAMPLE", secret_access_key="secret", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dep", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    service = DeploymentService(repository=repository, auth_service=auth_service, terraform_service=tf_service, terraform_binary="terraform")
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    with pytest.raises(ApprovalError) as exc:
        service.execute_deployment(dep_id, sample_spec, genuine_plan_hash)
    assert "Plan integrity verification failed" in str(exc.value)

    updated = repository.get(dep_id)
    assert updated.approval_status == ApprovalStatus.REVOKED


@patch("subprocess.run")
def test_real_deployment_subprocess_execution_success(mock_subproc, repository, sample_spec, tmp_path, monkeypatch):
    """Verifies real apply executes exact command with credential env and captures exit code 0 (SUCCEEDED)."""
    dep_id = "dep-real-apply-ok"
    tf_service = TerraformService(repository=repository, workspace_root=tmp_path)
    tf_files = tf_service.generate_configuration(sample_spec)
    ws_dir = tf_service.write_workspace_files(dep_id, tf_files)
    plan_bytes = b"mock_tfplan_binary_artifact"
    (ws_dir / "tfplan").write_bytes(plan_bytes)
    genuine_hash = tf_service.calculate_plan_hash(tf_files, plan_bytes)

    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash=genuine_hash,
        aws_account_id="111111111111",
        aws_region="ap-south-1",
    )
    repository.create(record)

    auth_service = AWSAuthService()
    secret_val = "SECRET_SUPER_KEY_NEVER_LOG"
    creds = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.ACCESS_KEY,
        access_key_id="AKIA11112222EXAMPLE",
        secret_access_key=secret_val,
        region="ap-south-1",
    )
    identity = AWSIdentity(
        account_id="111111111111",
        arn="arn:aws:iam::111111111111:user/deployer",
        user_id="AID111",
        region="ap-south-1",
        auth_method=AWSAuthMethod.ACCESS_KEY,
    )
    auth_service.set_active_credentials(creds, identity)

    # Mock subprocess.run for apply and show -json
    apply_proc = MagicMock()
    apply_proc.returncode = 0
    apply_proc.stdout = f"Apply complete! Resources: 1 added. Secret: {secret_val}"
    apply_proc.stderr = ""

    show_proc = MagicMock()
    show_proc.returncode = 0
    show_proc.stdout = json.dumps({
        "values": {
            "root_module": {
                "resources": [
                    {"type": "aws_instance", "address": "aws_instance.app_server", "values": {"id": "i-real001122"}}
                ]
            }
        }
    })

    mock_subproc.side_effect = [apply_proc, show_proc]

    service = DeploymentService(
        repository=repository,
        auth_service=auth_service,
        terraform_service=tf_service,
        terraform_binary="terraform",
    )
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    result = service.execute_deployment(dep_id, sample_spec, genuine_hash)

    # Assertions
    assert result["status"] == "SUCCEEDED"
    assert result["execution_mode"] == "real"
    assert result["instance_id"] == "i-real001122"
    assert secret_val not in result["logs"]
    assert "[REDACTED_SECRET_KEY]" in result["logs"]

    # Verify first subprocess call was exact apply command
    first_call_args, first_call_kwargs = mock_subproc.call_args_list[0]
    expected_cmd = ["terraform", "apply", "-input=false", "-auto-approve", "tfplan"]
    assert first_call_args[0] == expected_cmd
    assert first_call_kwargs["env"]["AWS_ACCESS_KEY_ID"] == "AKIA11112222EXAMPLE"
    assert first_call_kwargs["env"]["AWS_SECRET_ACCESS_KEY"] == secret_val

    # Verify database record
    updated_rec = repository.get(dep_id)
    assert updated_rec.status == DeploymentStatus.COMPLETED
    assert updated_rec.terraform_exit_code == 0
    assert "i-real001122" in updated_rec.aws_resource_ids
    assert secret_val not in updated_rec.execution_logs


@patch("subprocess.run")
def test_real_deployment_partial_failure_distinguished_and_no_destroy(mock_subproc, repository, sample_spec, tmp_path, monkeypatch):
    """When apply returns non-zero and state has resources, status is PARTIALLY_DEPLOYED, no destroy is executed."""
    dep_id = "dep-real-apply-partial"
    tf_service = TerraformService(repository=repository, workspace_root=tmp_path)
    tf_files = tf_service.generate_configuration(sample_spec)
    ws_dir = tf_service.write_workspace_files(dep_id, tf_files)
    plan_bytes = b"mock_tfplan_binary_artifact"
    (ws_dir / "tfplan").write_bytes(plan_bytes)
    genuine_hash = tf_service.calculate_plan_hash(tf_files, plan_bytes)

    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash=genuine_hash,
        aws_account_id="111111111111",
        aws_region="ap-south-1",
    )
    repository.create(record)

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.ACCESS_KEY,
        access_key_id="AKIA11112222EXAMPLE",
        secret_access_key="secret",
        region="ap-south-1",
    )
    identity = AWSIdentity(
        account_id="111111111111",
        arn="arn:aws:iam::111111111111:user/deployer",
        user_id="AID111",
        region="ap-south-1",
    )
    auth_service.set_active_credentials(creds, identity)

    # Mock non-zero exit code on apply
    apply_proc = MagicMock()
    apply_proc.returncode = 1
    apply_proc.stdout = "aws_security_group.app_sg created: sg-partial01\naws_instance.app_server: Error creating"
    apply_proc.stderr = "Error: InvalidSubnetID.NotFound: The subnet ID does not exist"

    # State show recovers partially created security group
    show_proc = MagicMock()
    show_proc.returncode = 0
    show_proc.stdout = json.dumps({
        "values": {
            "root_module": {
                "resources": [
                    {"type": "aws_security_group", "address": "aws_security_group.app_sg", "values": {"id": "sg-partial01"}}
                ]
            }
        }
    })

    mock_subproc.side_effect = [apply_proc, show_proc]

    service = DeploymentService(
        repository=repository,
        auth_service=auth_service,
        terraform_service=tf_service,
        terraform_binary="terraform",
    )
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    result = service.execute_deployment(dep_id, sample_spec, genuine_hash)

    assert result["status"] == "PARTIALLY_DEPLOYED"
    assert result["exit_code"] == 1
    assert "sg-partial01" in result["partial_resource_ids"]

    # Verify that NO destroy command was ever executed
    for call in mock_subproc.call_args_list:
        cmd_called = call[0][0]
        assert "destroy" not in cmd_called

    # Verify SQLite record marked as PARTIALLY_DEPLOYED with exit code and partial resource IDs
    updated_rec = repository.get(dep_id)
    assert updated_rec.status == DeploymentStatus.PARTIALLY_DEPLOYED
    assert updated_rec.terraform_exit_code == 1
    assert "sg-partial01" in updated_rec.aws_resource_ids


@patch("subprocess.run")
def test_real_deployment_failed_without_resources(mock_subproc, repository, sample_spec, tmp_path, monkeypatch):
    """When apply returns non-zero and NO resources exist in state, status is FAILED."""
    dep_id = "dep-real-apply-zero-res"
    tf_service = TerraformService(repository=repository, workspace_root=tmp_path)
    tf_files = tf_service.generate_configuration(sample_spec)
    ws_dir = tf_service.write_workspace_files(dep_id, tf_files)
    plan_bytes = b"mock_tfplan_binary_artifact"
    (ws_dir / "tfplan").write_bytes(plan_bytes)
    genuine_hash = tf_service.calculate_plan_hash(tf_files, plan_bytes)

    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash=genuine_hash,
        aws_account_id="111111111111",
        aws_region="ap-south-1",
    )
    repository.create(record)

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA11112222EXAMPLE", secret_access_key="secret", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/deployer", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    # Mock non-zero exit code on apply with 0 resources in state
    apply_proc = MagicMock()
    apply_proc.returncode = 1
    apply_proc.stdout = "Error: Provider configuration invalid"
    apply_proc.stderr = "Error: Invalid credentials"

    show_proc = MagicMock()
    show_proc.returncode = 0
    show_proc.stdout = json.dumps({"values": {"root_module": {"resources": []}}})

    mock_subproc.side_effect = [apply_proc, show_proc]

    service = DeploymentService(repository=repository, auth_service=auth_service, terraform_service=tf_service, terraform_binary="terraform")
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    result = service.execute_deployment(dep_id, sample_spec, genuine_hash)

    assert result["status"] == "FAILED"
    assert result["exit_code"] == 1
    assert result["partial_resource_ids"] == []

    updated_rec = repository.get(dep_id)
    assert updated_rec.status == DeploymentStatus.FAILED
    assert updated_rec.aws_resource_ids == []


@patch("subprocess.run")
def test_real_deployment_timeout_execution_status_unknown(mock_subproc, repository, sample_spec, tmp_path, monkeypatch):
    """When apply times out and state cannot be confirmed, status is EXECUTION_STATUS_UNKNOWN."""
    dep_id = "dep-real-apply-timeout"
    tf_service = TerraformService(repository=repository, workspace_root=tmp_path)
    tf_files = tf_service.generate_configuration(sample_spec)
    ws_dir = tf_service.write_workspace_files(dep_id, tf_files)
    plan_bytes = b"mock_tfplan_binary_artifact"
    (ws_dir / "tfplan").write_bytes(plan_bytes)
    genuine_hash = tf_service.calculate_plan_hash(tf_files, plan_bytes)

    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        approval_status=ApprovalStatus.APPROVED,
        plan_hash=genuine_hash,
        aws_account_id="111111111111",
        aws_region="ap-south-1",
    )
    repository.create(record)

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA11112222EXAMPLE", secret_access_key="secret", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/deployer", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    import subprocess
    mock_subproc.side_effect = subprocess.TimeoutExpired(cmd=["terraform", "apply"], timeout=180)

    service = DeploymentService(repository=repository, auth_service=auth_service, terraform_service=tf_service, terraform_binary="terraform")
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    result = service.execute_deployment(dep_id, sample_spec, genuine_hash)

    assert result["status"] == "EXECUTION_STATUS_UNKNOWN"
    assert result["exit_code"] == -1

    updated_rec = repository.get(dep_id)
    assert updated_rec.status == DeploymentStatus.EXECUTION_STATUS_UNKNOWN
