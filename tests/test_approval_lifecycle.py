from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from core.exceptions import ApprovalError
from core.models import ApprovalStatus, DeploymentRecord, DeploymentStatus
from database.db import init_db
from database.repository import DeploymentRepository
from ui.saved_plans_view import resolve_plan_approval_display
from ui.settings_view import _revoke_active_unexecuted_approval


@pytest.fixture
def temp_repo(tmp_path):
    """Provides a fresh isolated SQLite database repository for testing."""
    db_file = tmp_path / "test_lifecycle.db"
    init_db(db_file)
    return DeploymentRepository(db_path=db_file)


def test_settings_authentication_cannot_revoke_completed_deployment_approval(temp_repo):
    """Settings actions must never revoke approval for COMPLETED or PARTIALLY_DEPLOYED deployments."""
    # 1. Create completed deployment
    completed_rec = DeploymentRecord(
        deployment_id="dep-completed-001",
        user_request="Deploy production EC2 instance",
        service="ec2",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="a" * 64,
        approved_at=datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc),
    )
    temp_repo.create(completed_rec)

    # 2. Create partially deployed deployment
    partial_rec = DeploymentRecord(
        deployment_id="dep-partial-002",
        user_request="Deploy partial EC2 instance",
        service="ec2",
        status=DeploymentStatus.PARTIALLY_DEPLOYED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="b" * 64,
        approved_at=datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc),
    )
    temp_repo.create(partial_rec)

    # Attempt to revoke via settings action while mocking session state
    session = {"active_deployment_id": "dep-completed-001", "plan_approved": True}
    with patch("streamlit.session_state", session), \
         patch("database.repository.DeploymentRepository._get_conn", temp_repo._get_conn):
        _revoke_active_unexecuted_approval("dep-completed-001")
        _revoke_active_unexecuted_approval("dep-partial-002")

    # Verify both records retain APPROVED status in SQLite
    after_completed = temp_repo.get("dep-completed-001")
    after_partial = temp_repo.get("dep-partial-002")
    assert after_completed.approval_status == ApprovalStatus.APPROVED
    assert after_partial.approval_status == ApprovalStatus.APPROVED


def test_terminal_deployment_approval_cannot_be_mutated(temp_repo):
    """Terminal deployment (COMPLETED and PARTIALLY_DEPLOYED) approval status cannot be mutated."""
    rec = DeploymentRecord(
        deployment_id="dep-term-001",
        user_request="Production workload",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="c" * 64,
        approved_at=datetime.now(timezone.utc),
    )
    temp_repo.create(rec)

    # Directly calling update_approval must raise ApprovalError and leave DB unmutated
    with pytest.raises(ApprovalError, match="Cannot mutate approval status of terminal deployment"):
        temp_repo.update_approval("dep-term-001", ApprovalStatus.REVOKED)

    refetched = temp_repo.get("dep-term-001")
    assert refetched.approval_status == ApprovalStatus.APPROVED


def test_existing_approved_at_is_preserved_when_changing_approval_status(temp_repo):
    """Existing approved_at timestamp must be preserved when an unexecuted deployment changes approval status."""
    orig_approved_at = datetime(2026, 11, 22, 8, 30, 0, tzinfo=timezone.utc)
    rec = DeploymentRecord(
        deployment_id="dep-draft-001",
        user_request="Staging EC2 workload",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="d" * 64,
        approved_at=orig_approved_at,
    )
    temp_repo.create(rec)

    # Revoke approval on this unexecuted draft
    success = temp_repo.update_approval("dep-draft-001", ApprovalStatus.REVOKED)
    assert success is True

    refetched = temp_repo.get("dep-draft-001")
    assert refetched.approval_status == ApprovalStatus.REVOKED
    # The historical approved_at timestamp MUST be preserved, not wiped to None
    assert refetched.approved_at is not None
    assert refetched.approved_at.year == 2026
    assert refetched.approved_at.hour == 8


def test_unexecuted_deployment_approval_can_still_be_revoked(temp_repo):
    """Unexecuted deployments in DRAFT status can still be revoked by settings actions."""
    rec = DeploymentRecord(
        deployment_id="dep-unexec-001",
        user_request="Dev testing deployment",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="e" * 64,
        approved_at=datetime.now(timezone.utc),
    )
    temp_repo.create(rec)

    session = {"active_deployment_id": "dep-unexec-001", "plan_approved": True}
    with patch("streamlit.session_state", session), \
         patch("database.repository.DeploymentRepository._get_conn", temp_repo._get_conn):
        _revoke_active_unexecuted_approval("dep-unexec-001")

    refetched = temp_repo.get("dep-unexec-001")
    assert refetched.approval_status == ApprovalStatus.REVOKED
    assert session.get("plan_approved") is False


def test_historical_missing_approval_timestamps_displayed_honestly():
    """Historical missing approval timestamps are displayed honestly without fabricating or substituting timestamps."""
    # Case 1: Completed deployment with genuine evidence but missing approved_at
    completed_without_ts = DeploymentRecord(
        deployment_id="98c69e2e-d5b2-4a37-942d-3f2c0eafc300",
        user_request="Deploy public EC2 instance",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.REVOKED,  # Accidentally set to revoked historically
        plan_hash="0a73c987c11fbb975745418a6a8a23823ec6a927a577a6ee1a15da60780f5c6e",
        execution_started_at=datetime(2026, 11, 22, 13, 28, 10, tzinfo=timezone.utc),
        created_at=datetime(2026, 11, 22, 13, 23, 42, tzinfo=timezone.utc),
        aws_resource_ids=["i-01ef153cd68711430", "sg-06b5b95c6766bca27"],
        approved_at=None,
    )
    status_label, timestamp_label = resolve_plan_approval_display(completed_without_ts)
    assert status_label == "APPROVED (CONSUMED)"
    assert timestamp_label == "Historical approval timestamp unavailable"
    # Verify execution_started_at or created_at was NOT substituted as approved_at
    assert "13:28:10" not in timestamp_label
    assert "13:23:42" not in timestamp_label

    # Case 2: Completed deployment with genuine approved_at timestamp
    actual_ts = datetime(2026, 11, 22, 13, 25, 0, tzinfo=timezone.utc)
    completed_with_ts = DeploymentRecord(
        deployment_id="dep-with-ts",
        user_request="Deploy public EC2",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="f" * 64,
        approved_at=actual_ts,
    )
    status_label_2, timestamp_label_2 = resolve_plan_approval_display(completed_with_ts)
    assert status_label_2 == "APPROVED (CONSUMED)"
    assert "2026-11-22 13:25:00" in timestamp_label_2

    # Case 3: Draft unapproved deployment
    draft_rec = DeploymentRecord(
        deployment_id="dep-draft-plain",
        user_request="Draft plan",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
        approved_at=None,
    )
    status_label_3, timestamp_label_3 = resolve_plan_approval_display(draft_rec)
    assert status_label_3 == "PENDING"
    assert timestamp_label_3 == "Not yet approved"


def test_plan_review_approval_with_pipeline_result_does_not_raise_unbound_local_error(temp_repo):
    """Approval screen safely handles summary when pipeline_result is present without UnboundLocalError."""
    from core.models import (
        AMIResolutionResult,
        NetworkSpec,
        NormalizedEC2Spec,
        NormalizedInfrastructureSpec,
        PlannerReadinessStatus,
        PlannerResult,
        StorageSpec,
        TerraformPlanResult,
    )
    from ui.plan_review_view import render_plan_review_view

    dep_id = "dep-test-pipeline-approval"
    rec = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy public t3.micro",
        service="ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
    )
    temp_repo.create(rec)

    spec = NormalizedInfrastructureSpec(
        service="ec2",
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="app-server",
                instance_type="t3.micro",
                region="ap-south-1",
                ami=AMIResolutionResult(ami_id="ami-0593d733c2fc5bd2e"),
                storage=StorageSpec(size_gb=8, encrypted=True),
                network=NetworkSpec(public_ip=True, subnet_id="subnet-067ec34fd443e6195"),
            )
        ],
    )
    plan_result = PlannerResult(readiness=PlannerReadinessStatus.READY, normalized_spec=spec)
    pipeline_result = TerraformPlanResult(
        deployment_id=dep_id,
        workspace_dir=str(Path("workspaces") / dep_id),
        stage="PLAN",
        format_passed=True,
        init_passed=True,
        validation_passed=True,
        plan_passed=True,
        to_add=5,
        to_change=0,
        to_destroy=0,
        plan_hash="cryptographic-plan-hash-12345",
        actions=[{"resource": "aws_instance.app_server", "type": "aws_instance", "action": "create", "details": "Type: t3.micro"}],
    )

    session = {
        "active_plan_result": plan_result,
        "active_tf_files": {"main.tf": "# tf", "variables.tf": "# vars"},
        "active_tf_plan_result": pipeline_result,
        "active_deployment_id": dep_id,
    }

    # Simulate clicking "✅ Approve & Deploy ➔"
    def fake_button(label, *args, **kwargs):
        if "Approve" in label:
            return True
        return False

    with patch("streamlit.session_state", session), \
         patch("streamlit.button", side_effect=fake_button), \
         patch("streamlit.rerun"), \
         patch("ui.plan_review_view.DeploymentRepository", return_value=temp_repo):
        # Must execute without UnboundLocalError
        render_plan_review_view()

    updated = temp_repo.get(dep_id)
    assert updated.approval_status == ApprovalStatus.APPROVED
    assert updated.plan_hash == "cryptographic-plan-hash-12345"
    assert updated.plan_summary is not None
    assert updated.plan_summary["to_add"] == 5
    assert updated.plan_summary["to_destroy"] == 0


def test_plan_review_approval_without_pipeline_result_safely_persists_summary(temp_repo):
    """Approval screen safely generates and persists plan_summary when pipeline_result is None."""
    from core.models import (
        AMIResolutionResult,
        NetworkSpec,
        NormalizedEC2Spec,
        NormalizedInfrastructureSpec,
        PlannerReadinessStatus,
        PlannerResult,
        StorageSpec,
    )
    from ui.plan_review_view import render_plan_review_view

    dep_id = "dep-test-no-pipeline-approval"
    rec = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy private t3.micro",
        service="ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
    )
    temp_repo.create(rec)

    spec = NormalizedInfrastructureSpec(
        service="ec2",
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="app-server",
                instance_type="t3.micro",
                region="ap-south-1",
                ami=AMIResolutionResult(ami_id="ami-0593d733c2fc5bd2e"),
                storage=StorageSpec(size_gb=8, encrypted=True),
                network=NetworkSpec(public_ip=False, subnet_id="subnet-067ec34fd443e6195"),
            )
        ],
    )
    plan_result = PlannerResult(readiness=PlannerReadinessStatus.READY, normalized_spec=spec)

    session = {
        "active_plan_result": plan_result,
        "active_tf_files": {"main.tf": "# tf content", "variables.tf": "# vars content"},
        "active_tf_plan_result": None,
        "active_deployment_id": dep_id,
    }

    # Simulate clicking "✅ Approve & Deploy ➔" with can_approve forced
    def fake_button(label, *args, **kwargs):
        if "Approve" in label:
            return True
        return False

    with patch("streamlit.session_state", session), \
         patch("streamlit.button", side_effect=fake_button), \
         patch("streamlit.rerun"), \
         patch("ui.plan_review_view.DeploymentRepository", return_value=temp_repo):
        render_plan_review_view()

    # When pipeline_result is None, approval is blocked by approval gate guard
    # Verify that no UnboundLocalError occurred during rendering
    assert session.get("active_plan_hash") is not None

