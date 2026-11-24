from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from core.models import (
    ApprovalStatus,
    DeploymentRecord,
    DeploymentStatus,
    VerificationStatus,
)
from ui.saved_plans_view import (
    extract_plan_resource_summary,
    get_plan_actions_details,
    render_saved_plans_view,
)


def test_extract_plan_resource_summary_from_plan_summary():
    rec = DeploymentRecord(
        deployment_id="test-1234",
        user_request="Deploy t3.micro",
        service="ec2",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        plan_summary={
            "to_add": 5,
            "to_change": 0,
            "to_destroy": 0,
            "actions": [
                {"resource": "aws_instance.app_server", "type": "aws_instance", "action": "create"},
                {"resource": "aws_security_group.app_sg", "type": "aws_security_group", "action": "create"},
                {"resource": "aws_iam_role.app_role", "type": "aws_iam_role", "action": "create"},
            ],
        },
    )
    summary = extract_plan_resource_summary(rec)
    assert "+5/~0/-0" in summary
    assert "instance" in summary.lower() or "security_group" in summary.lower()


def test_extract_plan_resource_summary_from_normalized_spec():
    rec = DeploymentRecord(
        deployment_id="test-5678",
        user_request="Deploy t3.micro",
        service="ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
        normalized_spec={
            "service": "ec2",
            "region": "ap-south-1",
            "resources": [
                {
                    "name": "app-server",
                    "instance_type": "t3.small",
                    "storage": {"size_gb": 20, "volume_type": "gp3"},
                }
            ],
        },
    )
    summary = extract_plan_resource_summary(rec)
    assert "t3.small" in summary
    assert "20 GiB" in summary


def test_extract_plan_resource_summary_from_parsed_intent():
    rec = DeploymentRecord(
        deployment_id="test-9012",
        user_request="Deploy t3.micro",
        service="ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
        parsed_intent={
            "service": "ec2",
            "region": "ap-south-1",
            "resources": [
                {
                    "instance_type": "t3.nano",
                }
            ],
        },
    )
    summary = extract_plan_resource_summary(rec)
    assert "t3.nano" in summary


def test_extract_plan_resource_summary_fallback():
    rec = DeploymentRecord(
        deployment_id="test-9999",
        user_request="Deploy something",
        service="ec2",
        status=DeploymentStatus.DRAFT,
        approval_status=ApprovalStatus.PENDING,
    )
    summary = extract_plan_resource_summary(rec)
    assert "EC2" in summary


def test_get_plan_actions_details():
    rec = DeploymentRecord(
        deployment_id="test-act",
        user_request="Deploy",
        plan_summary={
            "actions": [
                {"resource": "aws_instance.app", "type": "aws_instance", "action": "create", "details": "Type: t3.micro"}
            ]
        },
    )
    actions = get_plan_actions_details(rec)
    assert len(actions) == 1
    assert actions[0]["resource"] == "aws_instance.app"


@patch("ui.saved_plans_view.DeploymentRepository")
@patch("streamlit.session_state", {})
def test_render_saved_plans_view_empty_state(mock_repo_cls):
    mock_repo = MagicMock()
    mock_repo.list_recent.return_value = []
    mock_repo_cls.return_value = mock_repo

    with patch("streamlit.markdown") as mock_md, patch("streamlit.button") as mock_btn:
        render_saved_plans_view()
        # Verify empty state rendered
        calls = [str(call) for call in mock_md.call_args_list]
        assert any("No Saved Deployment Plans Found" in c for c in calls)


@patch("ui.saved_plans_view.DeploymentRepository")
@patch("streamlit.session_state", {})
def test_render_saved_plans_view_with_plans(mock_repo_cls):
    mock_repo = MagicMock()
    rec = DeploymentRecord(
        deployment_id="dep-12345678-abcd-1234-abcd-1234567890ab",
        user_request="Create t3.micro EC2 instance",
        service="ec2",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        aws_region="ap-south-1",
        plan_hash="abc123plan_hash",
        created_at=datetime.now(timezone.utc),
        plan_summary={
            "to_add": 5,
            "to_change": 0,
            "to_destroy": 0,
            "actions": [
                {"resource": "aws_instance.app", "type": "aws_instance", "action": "create", "details": "t3.micro"}
            ],
        },
    )
    mock_repo.list_recent.return_value = [rec]
    mock_repo_cls.return_value = mock_repo

    with patch("streamlit.dataframe") as mock_df, \
         patch("streamlit.selectbox") as mock_select, \
         patch("streamlit.text_input") as mock_text, \
         patch("streamlit.columns") as mock_cols:
        mock_text.return_value = ""
        mock_select.side_effect = ["All", "All", 0]
        mock_cols.side_effect = lambda spec, *args, **kwargs: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]

        render_saved_plans_view()
        assert mock_df.called
