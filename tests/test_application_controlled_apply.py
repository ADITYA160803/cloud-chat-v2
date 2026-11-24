from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from config import AppConfig
from core.exceptions import ApprovalError, ConfigurationError
from core.models import (
    ApprovalStatus,
    AWSService,
    DeploymentRecord,
    DeploymentStatus,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    VerificationStatus,
)
from database.db import init_db
from database.repository import DeploymentRepository
from services.aws_auth_service import AWSAuthService, AWSCredentialsContainer
from services.deployment_service import DeploymentService


@pytest.fixture
def temp_repo(tmp_path):
    """Provides an isolated SQLite deployment repository."""
    db_file = tmp_path / "test_app_apply.db"
    init_db(db_file)
    return DeploymentRepository(db_path=db_file)


@pytest.fixture
def sample_spec():
    return NormalizedInfrastructureSpec(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="app-server",
                instance_type="t3.micro",
                region="ap-south-1",
            )
        ],
        user_request="Deploy ec2 instance",
    )


def test_concurrency_lock_rejects_duplicate_execution(temp_repo, sample_spec):
    """A deployment currently IN_PROGRESS must be rejected to prevent duplicate execution."""
    rec = DeploymentRecord(
        deployment_id="dep-concurrent-01",
        user_request="Deploy ec2",
        status=DeploymentStatus.IN_PROGRESS,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-12345",
    )
    temp_repo.create(rec)

    service = DeploymentService(repository=temp_repo)
    with pytest.raises(ApprovalError, match="is already in progress"):
        service.execute_deployment("dep-concurrent-01", sample_spec, "hash-12345")


def test_terminal_state_lock_rejects_reexecution_of_completed(temp_repo, sample_spec):
    """A COMPLETED deployment must never be re-executed by the application."""
    rec = DeploymentRecord(
        deployment_id="dep-completed-01",
        user_request="Deploy ec2",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-12345",
    )
    temp_repo.create(rec)

    service = DeploymentService(repository=temp_repo)
    with pytest.raises(ApprovalError, match="cannot be re-executed because it has already reached terminal status 'completed'"):
        service.execute_deployment("dep-completed-01", sample_spec, "hash-12345")


def test_terminal_state_lock_rejects_reexecution_of_partially_deployed(temp_repo, sample_spec):
    """A PARTIALLY_DEPLOYED deployment must never be re-executed by the application without new plan."""
    rec = DeploymentRecord(
        deployment_id="dep-partially-deployed-01",
        user_request="Deploy ec2",
        status=DeploymentStatus.PARTIALLY_DEPLOYED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-12345",
    )
    temp_repo.create(rec)

    service = DeploymentService(repository=temp_repo)
    with pytest.raises(ApprovalError, match="cannot be re-executed because it has already reached terminal status 'partially_deployed'"):
        service.execute_deployment("dep-partially-deployed-01", sample_spec, "hash-12345")


def test_plan_review_approval_resets_execution_flags(temp_repo, sample_spec):
    """Clicking Approve & Deploy clears stale execution state and navigates to Deployment in Progress."""
    from core.models import PlannerReadinessStatus, PlannerResult, TerraformPlanResult
    from ui.plan_review_view import render_plan_review_view

    dep_id = "dep-reset-flags-01"
    rec = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
    )
    temp_repo.create(rec)

    plan_result = PlannerResult(readiness=PlannerReadinessStatus.READY, normalized_spec=sample_spec)
    pipeline_result = TerraformPlanResult(
        deployment_id=dep_id,
        workspace_dir=str(Path("workspaces") / dep_id),
        stage="PLAN",
        format_passed=True,
        init_passed=True,
        validation_passed=True,
        plan_passed=True,
        to_add=1,
        plan_hash="valid-hash-abc",
        actions=[{"resource": "aws_instance.app_server", "type": "aws_instance", "action": "create"}],
    )

    session = {
        "active_plan_result": plan_result,
        "active_tf_files": {"main.tf": "# tf"},
        "active_tf_plan_result": pipeline_result,
        "active_deployment_id": dep_id,
        "deployment_executed": True,  # Stale flag from previous run
        "active_deployment_result": {"status": "OLD"},
        "deployment_error": "Old error",
    }

    def fake_button(label, *args, **kwargs):
        if "Approve" in label:
            return True
        return False

    with patch("streamlit.session_state", session), \
         patch("streamlit.button", side_effect=fake_button), \
         patch("streamlit.rerun"), \
         patch("ui.plan_review_view.DeploymentRepository", return_value=temp_repo):
        render_plan_review_view()

    # Verify execution flags were safely cleared and page set to progress
    assert session.get("plan_approved") is True
    assert "deployment_executed" not in session
    assert "active_deployment_result" not in session
    assert "deployment_error" not in session
    assert session.get("current_page") == "Deployment in Progress"


def test_deployments_view_execute_button_resets_execution_flags(temp_repo):
    """Clicking Execute Deployment in Deployments view clears stale execution state."""
    from ui.deployment_view import render_deployment_view

    dep_id = "dep-exec-button-01"
    rec = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        status=DeploymentStatus.PENDING_APPROVAL,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-valid",
        aws_region="ap-south-1",
    )
    temp_repo.create(rec)

    session = {
        "active_deployment_id": dep_id,
        "deployment_executed": True,
        "active_deployment_result": {"status": "OLD"},
        "deployment_error": "Old error",
    }

    def fake_button(label, *args, **kwargs):
        if "Execute Deployment" in label:
            return True
        return False

    with patch("streamlit.session_state", session), \
         patch("streamlit.button", side_effect=fake_button), \
         patch("streamlit.rerun"), \
         patch("ui.deployment_view.DeploymentRepository", return_value=temp_repo):
        render_deployment_view()

    assert "deployment_executed" not in session
    assert "active_deployment_result" not in session
    assert "deployment_error" not in session
    assert session.get("current_page") == "Deployment in Progress"


def test_deployment_service_auto_authenticates_profile(temp_repo, sample_spec, monkeypatch):
    """When credentials are not in memory, execute_deployment attempts profile auto-authentication."""
    from core.models import AWSIdentity, AWSAuthMethod

    dep_id = "dep-auto-profile-01"
    rec = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-auto-prof",
        aws_account_id="082229155432",
        aws_region="ap-south-1",
    )
    temp_repo.create(rec)

    auth_service = AWSAuthService()
    # Ensure memory is initially empty
    assert auth_service.get_active_credentials() is None

    mock_identity = AWSIdentity(
        account_id="082229155432",
        arn="arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/session",
        user_id="AROARGJJ5KZULLBNUIHDL",
        region="ap-south-1",
        auth_method=AWSAuthMethod.PROFILE,
        profile_name="cloud-chat-deploy",
        is_valid=True,
    )

    def fake_auth_profile(profile_name, region=None):
        if profile_name == "cloud-chat-deploy":
            creds = AWSCredentialsContainer(profile_name="cloud-chat-deploy", region=region or "ap-south-1")
            auth_service.set_active_credentials(creds, mock_identity)
            return mock_identity
        raise ConfigurationError("Profile not found")

    auth_service.authenticate_profile = MagicMock(side_effect=fake_auth_profile)

    service = DeploymentService(repository=temp_repo, auth_service=auth_service)
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    # Mock tf_service to avoid real terraform files on disk in this test
    tf_service_mock = MagicMock()
    ws_mock = MagicMock()
    ws_plan = MagicMock()
    ws_plan.exists.return_value = True
    ws_plan.read_bytes.return_value = b"binary-plan"
    ws_mock.__truediv__.return_value = ws_plan
    ws_mock.glob.return_value = []
    tf_service_mock.get_workspace_dir.return_value = ws_mock
    tf_service_mock.calculate_plan_hash.return_value = "hash-auto-prof"
    service.tf_service = tf_service_mock

    # Mock subprocess.run to simulate successful apply
    proc_mock = MagicMock()
    proc_mock.returncode = 0
    proc_mock.stdout = "Apply complete! Resources: 1 added, 0 changed, 0 destroyed."
    proc_mock.stderr = ""

    with patch("subprocess.run", return_value=proc_mock), \
         patch.object(service, "verify_deployment", return_value={"overall_status": "VERIFIED"}):
        result = service.execute_deployment(dep_id, sample_spec, "hash-auto-prof")

    assert result["status"] == "SUCCEEDED"
    assert auth_service.authenticate_profile.called


def test_apply_blocked_if_plan_contains_destructions(temp_repo, sample_spec, monkeypatch):
    """If approved plan has to_destroy > 0 or is_destructive, execution is blocked."""
    from services.aws_auth_service import AWSIdentity, AWSAuthMethod

    dep_id = "dep-block-destroy-01"
    rec = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-destroy",
        plan_summary={"to_add": 1, "to_change": 0, "to_destroy": 1, "is_destructive": True},
        aws_account_id="082229155432",
        aws_region="ap-south-1",
    )
    temp_repo.create(rec)

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(profile_name="cloud-chat-deploy", region="ap-south-1")
    identity = AWSIdentity(
        account_id="082229155432",
        arn="arn:aws:sts::082229155432:assumed-role/Role/session",
        user_id="USER1",
        region="ap-south-1",
        auth_method=AWSAuthMethod.PROFILE,
        is_valid=True,
    )
    auth_service.set_active_credentials(creds, identity)

    service = DeploymentService(repository=temp_repo, auth_service=auth_service)
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    # Mock tf_service
    tf_service_mock = MagicMock()
    ws_mock = MagicMock()
    ws_plan = MagicMock()
    ws_plan.exists.return_value = True
    ws_mock.__truediv__.return_value = ws_plan
    tf_service_mock.get_workspace_dir.return_value = ws_mock
    service.tf_service = tf_service_mock

    with pytest.raises(ApprovalError, match="Approved plan contains destructive changes"):
        service.execute_deployment(dep_id, sample_spec, "hash-destroy")


def test_apply_blocked_if_state_contains_tainted_resources(temp_repo, sample_spec, monkeypatch, tmp_path):
    """If workspace state contains tainted resources, apply is blocked to prevent accidental destroy/replace."""
    import json
    from services.aws_auth_service import AWSIdentity, AWSAuthMethod

    dep_id = "dep-block-tainted-01"
    rec = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-tainted",
        aws_account_id="082229155432",
        aws_region="ap-south-1",
    )
    temp_repo.create(rec)

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(profile_name="cloud-chat-deploy", region="ap-south-1")
    identity = AWSIdentity(
        account_id="082229155432",
        arn="arn:aws:sts::082229155432:assumed-role/Role/session",
        user_id="USER1",
        region="ap-south-1",
        auth_method=AWSAuthMethod.PROFILE,
        is_valid=True,
    )
    auth_service.set_active_credentials(creds, identity)

    service = DeploymentService(repository=temp_repo, auth_service=auth_service)
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    # Create workspace with a tainted resource in terraform.tfstate
    ws_dir = tmp_path / "ws_tainted"
    ws_dir.mkdir()
    state_file = ws_dir / "terraform.tfstate"
    state_data = {
        "terraform_version": "1.16.5",
        "resources": [
            {
                "type": "aws_instance",
                "name": "app_server",
                "instances": [{"status": "tainted", "attributes": {"id": "i-0999999999999"}}]
            }
        ]
    }
    state_file.write_text(json.dumps(state_data), encoding="utf-8")
    (ws_dir / "tfplan").write_bytes(b"plan-bytes")

    tf_service_mock = MagicMock()
    tf_service_mock.get_workspace_dir.return_value = ws_dir
    service.tf_service = tf_service_mock

    with pytest.raises(ApprovalError, match="contains tainted resource"):
        service.execute_deployment(dep_id, sample_spec, "hash-tainted")


def test_state_backup_created_before_apply(temp_repo, tmp_path):
    """backup_state creates a timestamped copy and backup_pre_apply of terraform.tfstate."""
    import json
    dep_id = "dep-backup-01"
    ws_dir = tmp_path / "ws_backup"
    ws_dir.mkdir()
    state_file = ws_dir / "terraform.tfstate"
    state_file.write_text(json.dumps({"version": 4, "serial": 1}), encoding="utf-8")

    service = DeploymentService(repository=temp_repo)
    tf_service_mock = MagicMock()
    tf_service_mock.get_workspace_dir.return_value = ws_dir
    service.tf_service = tf_service_mock

    backup = service.backup_state(dep_id)
    assert backup is not None
    assert backup.exists()
    assert (ws_dir / "terraform.tfstate.backup_pre_apply").exists()


def test_extract_iam_permission_error_patterns():
    """extract_iam_permission_error reliably detects various AWS IAM permission denial patterns."""
    # Pattern 1: ec2:MonitorInstances
    err1 = (
        "Error: updating Instance monitoring: operation error EC2: MonitorInstances, "
        "https response error StatusCode: 403, RequestID: abc-123, api error UnauthorizedOperation: "
        "You are not authorized to perform this operation. User: arn:aws:sts::082229155432:assumed-role/Role"
    )
    res1 = DeploymentService.extract_iam_permission_error(err1)
    assert res1 is not None
    assert res1["missing_permission"] == "ec2:MonitorInstances"
    assert "Missing required permission 'ec2:MonitorInstances'" in res1["message"]

    # Pattern 2: iam:CreateRole
    err2 = (
        "Error: creating IAM Role: AccessDenied: User: arn:aws:sts::082229155432:assumed-role/Role "
        "is not authorized to perform: iam:CreateRole on resource: arn:aws:iam::082229155432:role/app-role"
    )
    res2 = DeploymentService.extract_iam_permission_error(err2)
    assert res2 is not None
    assert res2["missing_permission"] == "iam:CreateRole"

    # Pattern 3: General unauthorized operation
    err3 = "Error creating Security Group: api error UnauthorizedOperation: You are not authorized to perform this operation."
    res3 = DeploymentService.extract_iam_permission_error(err3)
    assert res3 is not None
    assert "AccessDenied" in res3["error_type"]

    # Pattern 4: Non-IAM error
    err4 = "Error: InvalidParameterValue: The value of parameter SubnetId is invalid."
    assert DeploymentService.extract_iam_permission_error(err4) is None


def test_apply_marks_partially_deployed_when_verification_fails(temp_repo, sample_spec, monkeypatch):
    """When Terraform apply succeeds (code 0) but AWS verification fails, record status is PARTIALLY_DEPLOYED."""
    from services.aws_auth_service import AWSIdentity, AWSAuthMethod

    dep_id = "dep-verif-mismatch-01"
    rec = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-mismatch",
        aws_account_id="082229155432",
        aws_region="ap-south-1",
    )
    temp_repo.create(rec)

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(profile_name="cloud-chat-deploy", region="ap-south-1")
    identity = AWSIdentity(
        account_id="082229155432",
        arn="arn:aws:sts::082229155432:assumed-role/Role/session",
        user_id="USER1",
        region="ap-south-1",
        auth_method=AWSAuthMethod.PROFILE,
        is_valid=True,
    )
    auth_service.set_active_credentials(creds, identity)

    service = DeploymentService(repository=temp_repo, auth_service=auth_service)
    monkeypatch.setattr(AppConfig, "is_real_deployment_allowed", lambda self: True)

    tf_service_mock = MagicMock()
    ws_mock = MagicMock()
    ws_plan = MagicMock()
    ws_plan.exists.return_value = True
    ws_plan.read_bytes.return_value = b"binary-plan"
    ws_mock.__truediv__.return_value = ws_plan
    ws_mock.glob.return_value = []
    tf_service_mock.get_workspace_dir.return_value = ws_mock
    tf_service_mock.calculate_plan_hash.return_value = "hash-mismatch"
    service.tf_service = tf_service_mock

    proc_mock = MagicMock()
    proc_mock.returncode = 0
    proc_mock.stdout = "Apply complete! Resources: 1 added, 0 changed, 0 destroyed."
    proc_mock.stderr = ""

    # Mock extract_state_resources to return instance
    with patch("subprocess.run", return_value=proc_mock), \
         patch.object(service, "_extract_state_resources", return_value=[{"type": "aws_instance", "id": "i-12345678"}]), \
         patch.object(service, "verify_deployment", return_value={
             "overall_status": "CONFIGURATION_MISMATCH",
             "summary": "AMI mismatch detected"
         }):
        result = service.execute_deployment(dep_id, sample_spec, "hash-mismatch")

    assert result["status"] == "PARTIALLY_DEPLOYED"
    db_rec = temp_repo.get(dep_id)
    assert db_rec.status == DeploymentStatus.PARTIALLY_DEPLOYED
    assert "configuration mismatch" in db_rec.error_message

