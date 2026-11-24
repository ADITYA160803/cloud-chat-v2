from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest
import streamlit as st

from core.models import ApprovalStatus, DeploymentRecord, DeploymentStatus, VerificationStatus
from database.db import init_db
from database.repository import DeploymentRepository
from ui.chat_view import PRESET_EXAMPLES, render_chat_view
from ui.deployment_view import _get_relevant_config_details, render_deployment_view
from ui.history_view import render_history_view
from ui.settings_view import render_settings_view


@pytest.fixture
def test_repo(tmp_path):
    """Provides an isolated test repository."""
    db_file = tmp_path / "test_ui.db"
    init_db(db_file)
    return DeploymentRepository(db_path=db_file)


# ============================================================================
# Page 1: New Deployment Tests
# ============================================================================

def test_page1_preset_examples():
    """Verify Page 1 contains exactly three quick examples."""
    assert len(PRESET_EXAMPLES) == 3
    assert "private" in PRESET_EXAMPLES[0].lower()
    assert "public" in PRESET_EXAMPLES[1].lower()
    assert "production" in PRESET_EXAMPLES[2].lower()


def test_page1_markdown_formatting_not_literal():
    """Verify Markdown symbols like **EC2** are formatted to HTML and not displayed literally."""
    from ui.chat_view import _format_chat_content
    raw_text = "Target service is **EC2** in **ap-south-1** with `t3.micro`."
    formatted = _format_chat_content(raw_text)
    assert "**EC2**" not in formatted
    assert "<strong>EC2</strong>" in formatted or "<b>EC2</b>" in formatted
    assert "<code>t3.micro</code>" in formatted


def test_page1_render_chat_view():
    """Verify Page 1 renders header, examples, input box, and recent messages."""
    session = {
        "chat_messages": [
            {"message_id": "m1", "role": "user", "content": "Deploy an EC2 instance"},
            {"message_id": "m2", "role": "assistant", "content": "Understood, planning now."},
        ],
        "user_chat_input": "",
    }
    with patch("streamlit.session_state", session), \
         patch("streamlit.markdown") as mock_md, \
         patch("streamlit.button", return_value=False), \
         patch("streamlit.columns") as mock_cols, \
         patch("streamlit.form") as mock_form, \
         patch("streamlit.text_area") as mock_ta, \
         patch("streamlit.form_submit_button", return_value=False), \
         patch("streamlit.caption"):

        mock_cols.side_effect = lambda spec, *args, **kwargs: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]
        render_chat_view()
        assert mock_ta.called
        assert mock_md.called


# ============================================================================
# Page 2: Deployments View Tests
# ============================================================================

def test_page2_empty_state_when_no_deployments(tmp_path):
    """Verify Page 2 shows empty state with Start New Deployment button when no deployment exists."""
    session = {}
    with patch("streamlit.session_state", session), \
         patch("database.repository.DeploymentRepository.get", return_value=None), \
         patch("database.repository.DeploymentRepository.list_recent", return_value=[]), \
         patch("streamlit.markdown") as mock_md, \
         patch("streamlit.button", return_value=False) as mock_btn, \
         patch("streamlit.columns") as mock_cols:

        mock_cols.side_effect = lambda spec, *args, **kwargs: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]
        render_deployment_view()
        assert any("No Current Deployment" in str(call) for call in mock_md.call_args_list)


def test_page2_displays_current_deployment_and_progress():
    """Verify Page 2 displays Card 1 (Current Deployment) and Card 2 (5 stages)."""
    rec = DeploymentRecord(
        deployment_id="dep-current-test-123",
        user_request="Deploy t3.micro web server in Mumbai",
        service="ec2",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="hash-123",
        aws_region="ap-south-1",
        aws_resource_ids=["i-01ef153cd68711430", "sg-06b5b95c6766bca27"],
        verification_status=VerificationStatus.VERIFIED,
    )
    session = {"active_deployment_id": rec.deployment_id, "deployments_show_plan": True}

    with patch("streamlit.session_state", session), \
         patch("database.repository.DeploymentRepository.get", return_value=rec), \
         patch("streamlit.markdown") as mock_md, \
         patch("streamlit.button", return_value=False), \
         patch("streamlit.columns") as mock_cols:

        mock_cols.side_effect = lambda spec, *args, **kwargs: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]
        render_deployment_view()

        md_texts = " ".join([str(call) for call in mock_md.call_args_list])
        assert "dep-current-test-123" in md_texts
        assert "Plan Created" in md_texts
        assert "Plan Reviewed" in md_texts
        assert "Deploying" in md_texts
        assert "Completed / Failed" in md_texts
        assert "Approval Status" in md_texts
        assert "Saved Terraform Plan Details" in md_texts


def test_page2_relevant_config_details_extractor():
    """Verify configuration details extractor parses spec without raw metadata."""
    rec = DeploymentRecord(
        deployment_id="dep-cfg-01",
        user_request="Deploy EC2",
        service="ec2",
        normalized_spec={
            "region": "ap-south-1",
            "resources": [{
                "instance_type": "t3.small",
                "storage": {"size_gb": 20, "volume_type": "gp3", "encrypted": True},
                "networking": {"vpc_id": "vpc-test-1", "subnet_id": "subnet-test-1"},
            }],
        },
    )
    details = _get_relevant_config_details(rec)
    assert details["Instance Type"] == "t3.small"
    assert "20 GB gp3" in details["Storage"]
    assert details["VPC"] == "vpc-test-1"
    assert details["Subnet"] == "subnet-test-1"


# ============================================================================
# Page 3: Deployment History Tests
# ============================================================================

def test_page3_deployment_history_columns_and_selection():
    """Verify Page 3 displays table with exactly 4 columns and Selected Record Details."""
    rec = DeploymentRecord(
        deployment_id="dep-hist-001",
        user_request="Deploy private EC2 instance",
        service="ec2",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        verification_status=VerificationStatus.VERIFIED,
        aws_resource_ids=["i-0987654321fedcba0"],
        created_at=datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc),
    )
    session = {}
    with patch("streamlit.session_state", session), \
         patch("database.repository.DeploymentRepository.list_recent", return_value=[rec]), \
         patch("streamlit.text_input", return_value=""), \
         patch("streamlit.dataframe") as mock_df, \
         patch("streamlit.selectbox", return_value=0), \
         patch("streamlit.markdown") as mock_md:

        render_history_view()
        assert mock_df.called
        df_data = mock_df.call_args[0][0]
        assert len(df_data) == 1
        row = df_data[0]
        # Must have exactly these 4 keys
        assert set(row.keys()) == {"Deployment ID", "Request", "Date", "Status"}
        assert row["Deployment ID"] == "dep-hist"
        assert row["Status"] == "COMPLETED"

        # Check Selected Record Details in markdown
        md_texts = " ".join([str(call) for call in mock_md.call_args_list])
        assert "Selected Record Details" in md_texts
        assert "dep-hist-001" in md_texts
        assert "i-0987654321fedcba0" in md_texts


# ============================================================================
# Page 4: Settings View Tests
# ============================================================================

def test_page4_settings_single_aws_card():
    """Verify Page 4 displays exactly one AWS Connection card with fixed profile and region."""
    session = {}
    with patch("streamlit.session_state", session), \
         patch("services.aws_auth_service.aws_auth_service.get_active_identity", return_value=None), \
         patch("streamlit.markdown") as mock_md, \
         patch("streamlit.button", return_value=False) as mock_btn, \
         patch("streamlit.columns") as mock_cols:

        mock_cols.side_effect = lambda spec, *args, **kwargs: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]
        render_settings_view()

        md_texts = " ".join([str(call) for call in mock_md.call_args_list])
        assert "AWS Connection Management" in md_texts
        assert "cloud-chat-deploy" in md_texts
        assert "ap-south-1 (Mumbai)" in md_texts
        assert "Disconnected" in md_texts

        # Ensure removed UI elements are NOT present
        assert "Method A" not in md_texts
        assert "Method B" not in md_texts
        assert "Groq" not in md_texts
        assert "Gemini" not in md_texts


def test_page4_settings_test_connection_button():
    """Verify clicking Test Connection invokes authenticate_profile with cloud-chat-deploy."""
    session = {}
    mock_identity = MagicMock(account_id="082229155432", arn="arn:aws:iam::082229155432:role/deploy", is_valid=True)

    with patch("streamlit.session_state", session), \
         patch("services.aws_auth_service.aws_auth_service.get_active_identity", return_value=None), \
         patch("services.aws_auth_service.aws_auth_service.authenticate_profile", return_value=mock_identity) as mock_auth, \
         patch("streamlit.markdown"), \
         patch("streamlit.button") as mock_btn, \
         patch("streamlit.columns") as mock_cols, \
         patch("streamlit.spinner"), \
         patch("streamlit.success") as mock_succ, \
         patch("streamlit.rerun"):

        mock_cols.side_effect = lambda spec, *args, **kwargs: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]
        # First button is "Test Connection"
        mock_btn.side_effect = lambda label, *args, **kwargs: "Test Connection" in label

        render_settings_view()
        assert mock_auth.called
        mock_auth.assert_called_once_with(profile_name="cloud-chat-deploy", region="ap-south-1")
        assert session.get("aws_identity") == mock_identity
        assert mock_succ.called


def test_page4_settings_no_additional_arn_strip_when_connected():
    """Verify Page 4 does not render the additional Account ID and Role ARN strip when connected."""
    session = {}
    mock_identity = MagicMock(account_id="082229155432", arn="arn:aws:iam::082229155432:role/deploy", is_valid=True)

    with patch("streamlit.session_state", session), \
         patch("services.aws_auth_service.aws_auth_service.get_active_identity", return_value=mock_identity), \
         patch("streamlit.markdown") as mock_md, \
         patch("streamlit.button", return_value=False), \
         patch("streamlit.columns") as mock_cols:

        mock_cols.side_effect = lambda spec, *args, **kwargs: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]
        render_settings_view()

        md_texts = " ".join([str(call) for call in mock_md.call_args_list])
        assert "Active Role / Identity" not in md_texts
        assert "Connected" in md_texts
        assert "AWS Cloud Connectivity" in md_texts
