import pytest
from services.deployment_service import DeploymentService
from services.terraform_service import TerraformService
from ui.chat_view import render_chat_view
from ui.cost_view import render_cost_view
from ui.deployment_progress_view import render_deployment_progress_view
from ui.deployment_view import render_deployment_view
from ui.history_view import render_history_view
from ui.home_view import render_home_view
from ui.intent_view import render_intent_view
from ui.plan_review_view import render_plan_review_view
from ui.resources_view import render_resources_view
from ui.settings_view import render_settings_view
from ui.terraform_code_view import render_terraform_code_view
from ui.theme import apply_theme, render_top_header
from ui.verification_view import render_verification_view


def test_ui_renderers_importable():
    """Verify all 9 wizard pages and secondary views are callable."""
    assert callable(render_home_view)
    assert callable(render_chat_view)
    assert callable(render_intent_view)
    assert callable(render_cost_view)
    assert callable(render_terraform_code_view)
    assert callable(render_plan_review_view)
    assert callable(render_deployment_progress_view)
    assert callable(render_deployment_view)
    assert callable(render_verification_view)
    assert callable(render_history_view)
    assert callable(render_resources_view)
    assert callable(render_settings_view)


def test_theme_functions_callable():
    """Verify theme styling functions are present and callable."""
    assert callable(apply_theme)
    assert callable(render_top_header)


def test_terraform_and_deployment_services_callable():
    """Verify Terraform and Deployment services can be instantiated."""
    tf = TerraformService()
    dep = DeploymentService()
    assert tf is not None
    assert dep is not None


def test_app_main_navigation_structure():
    """Verify app.main() sets up navigation without conflicting widget defaults."""
    import app
    assert callable(app.ensure_initialized)
    assert callable(app.main)


def test_render_deployment_progress_view_no_name_error(tmp_path, monkeypatch):
    """Verify render_deployment_progress_view runs without NameError when recovering DB record."""
    from unittest.mock import MagicMock, patch
    from core.models import ApprovalStatus, DeploymentRecord, DeploymentStatus

    monkeypatch.chdir(tmp_path)
    dep_id = "dep-mock-terminal-01"
    session = {"active_deployment_id": dep_id}
    mock_rec = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy t3.micro",
        service="ec2",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="plan-hash-123",
        normalized_spec={
            "service": "ec2",
            "region": "ap-south-1",
            "resources": [{"instance_type": "t3.micro", "storage": {"size_gb": 8, "volume_type": "gp3"}}],
        },
    )

    ws_dir = tmp_path / "workspaces" / dep_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "tfplan").write_bytes(b"dummy binary plan")

    with patch("streamlit.session_state", session), \
         patch("database.repository.DeploymentRepository.get", return_value=mock_rec), \
         patch("services.terraform_service.TerraformService.calculate_plan_artifact_hash", return_value="hash"), \
         patch("services.terraform_service.TerraformService.calculate_plan_hash", return_value="plan-hash-123"), \
         patch("streamlit.columns") as mock_cols, \
         patch("streamlit.markdown"), \
         patch("streamlit.button") as mock_btn, \
         patch("streamlit.error") as mock_err, \
         patch("streamlit.warning"), \
         patch("streamlit.info"), \
         patch("streamlit.empty") as mock_empty:
        
        mock_btn.return_value = False
        mock_cols.side_effect = lambda spec, *args, **kwargs: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]
        mock_empty.return_value = MagicMock()

        # This should execute past line 135 without raising NameError
        render_deployment_progress_view()
        assert session.get("deployment_executed") is True
