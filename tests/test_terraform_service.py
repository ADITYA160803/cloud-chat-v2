from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from core.exceptions import TerraformExecutionError, ValidationError
from core.models import (
    AMIResolutionResult,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    StorageSpec,
    TerraformExecutionStage,
    VolumeType,
)
from services.terraform_service import TerraformService


def test_terraform_hcl_generation_structure():
    """Verify generated HCL files contain valid provider, resources, variables, and outputs."""
    spec = NormalizedInfrastructureSpec(
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="prod-web",
                instance_type="t3.micro",
                ami=AMIResolutionResult(ami_id="ami-0123456789abcdef0"),
                storage=StorageSpec(size_gb=8, volume_type=VolumeType.GP3, encrypted=True),
            )
        ],
    )
    svc = TerraformService()
    files = svc.generate_configuration(spec)

    assert "main.tf" in files
    assert "variables.tf" in files
    assert "outputs.tf" in files

    main_tf = files["main.tf"]
    assert 'resource "aws_instance" "app_server"' in main_tf
    assert 'resource "aws_security_group" "app_sg"' in main_tf
    assert "encrypted             = true" in main_tf
    assert 'volume_type           = "gp3"' in main_tf


def test_workspace_path_traversal_protection():
    """Verify workspace path resolver strictly rejects path traversal attempts."""
    svc = TerraformService()

    with pytest.raises(ValidationError):
        svc.get_workspace_dir("../../etc")

    with pytest.raises(ValidationError):
        svc.get_workspace_dir("../evil_dir")

    with pytest.raises(ValidationError):
        svc.get_workspace_dir("sub/folder/hack")

    with pytest.raises(ValidationError):
        svc.get_workspace_dir("")


def test_workspace_directory_isolation(tmp_path):
    """Verify that workspaces are created cleanly and independently."""
    svc = TerraformService(workspace_root=tmp_path / "workspaces")
    ws1 = svc.get_workspace_dir("dep-001")
    ws2 = svc.get_workspace_dir("dep-002")
    assert ws1 != ws2
    assert ws1.name == "dep-001"
    assert ws2.name == "dep-002"


def test_prevent_apply_and_destroy_in_m4(tmp_path):
    """Verify that terraform apply and destroy commands are strictly rejected by M4 boundaries."""
    svc = TerraformService(workspace_root=tmp_path)
    workspace = tmp_path / "ws_safety"
    workspace.mkdir()

    with pytest.raises(TerraformExecutionError) as exc_info:
        svc._run_command(["terraform", "apply", "-auto-approve"], workspace, timeout=10)
    assert "forbidden" in str(exc_info.value).lower()

    with pytest.raises(TerraformExecutionError) as exc_info2:
        svc._run_command(["terraform", "destroy", "-auto-approve"], workspace, timeout=10)
    assert "forbidden" in str(exc_info2.value).lower()


def test_missing_terraform_cli_handling(tmp_path):
    """Verify pipeline gracefully reports failure when terraform CLI binary is missing."""
    svc = TerraformService(terraform_binary="", workspace_root=tmp_path)
    spec = NormalizedInfrastructureSpec(region="us-east-1", resources=[NormalizedEC2Spec()])

    res = svc.run_pipeline("dep-no-cli", spec)
    assert res.stage == TerraformExecutionStage.FAILED
    assert res.plan_passed is False
    assert "not found" in res.error_message.lower()




def test_parse_plan_json_destructive_detection():
    """Verify plan parser correctly identifies delete actions as destructive."""
    svc = TerraformService()
    mock_json = """
    {
      "resource_changes": [
        {
          "address": "aws_instance.app_server",
          "type": "aws_instance",
          "change": {
            "actions": ["create"]
          }
        },
        {
          "address": "aws_instance.old_server",
          "type": "aws_instance",
          "change": {
            "actions": ["delete"]
          }
        }
      ]
    }
    """
    spec = NormalizedInfrastructureSpec(region="us-east-1", resources=[])
    to_add, to_change, to_destroy, actions = svc._parse_plan_json(mock_json, spec)

    assert to_add == 1
    assert to_destroy == 1
    assert to_change == 0
    assert len(actions) == 2
    assert any(a["action"] == "delete" for a in actions)


def test_command_timeout_handling(tmp_path):
    """Verify subprocess timeout produces a structured error instead of hanging."""
    svc = TerraformService()
    workspace = tmp_path / "ws_timeout"
    workspace.mkdir()

    # Simulate timeout by patching subprocess.run with TimeoutExpired
    with patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired(cmd="terraform", timeout=1)):
        res = svc._run_command(["terraform", "init"], workspace, timeout=1)
        assert res["exit_code"] == -1
        assert "timed out" in res["error"].lower()
