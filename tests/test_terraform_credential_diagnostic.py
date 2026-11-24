"""Automated tests for Terraform AWS Credential Propagation Diagnostic.

Tests verify:
1. Correct AWS identity propagation to Terraform.
2. Correct region propagation.
3. Secret and token credential redaction.
4. Identity mismatch handling (account mismatch, role mismatch).
5. Expired or unavailable credential handling (fail fast without silent fallback).
6. Diagnostic stage failure handling (INIT, VALIDATE, PLAN).
7. Strict safety: forbidding any 'apply' or 'destroy' invocation and zero managed resources.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from core.exceptions import ConfigurationError, TerraformExecutionError
from core.models import AWSAuthMethod, AWSIdentity
from services.aws_auth_service import AWSCredentialsContainer, AWSAuthService
from services.terraform_diagnostic_service import TerraformCredentialDiagnostic


@pytest.fixture
def mock_auth_service():
    service = MagicMock(spec=AWSAuthService)
    identity = AWSIdentity(
        account_id="082229155432",
        arn="arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/Session",
        user_id="AROATEST:Session",
        region="ap-south-1",
        auth_method=AWSAuthMethod.PROFILE,
        profile_name="cloud-chat-deploy",
        is_valid=True,
    )
    creds = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.PROFILE,
        profile_name="cloud-chat-deploy",
        region="ap-south-1",
        access_key_id="ASIAEXAMPLEKEY12345",
        secret_access_key="SECRET_KEY_M9_DIAG_TEST",
        session_token="SESSION_TOKEN_M9_DIAG_TEST",
    )
    service.get_active_identity.return_value = identity
    service.get_active_credentials.return_value = creds
    service.authenticate_profile.return_value = identity
    service.redact_secrets.side_effect = lambda text, **kw: creds.redact_secrets(text)
    return service


def test_diagnostic_success_identity_and_region_propagation(mock_auth_service):
    """Verify successful diagnostic correctly verifies identity, region, and subprocess environment."""
    diag = TerraformCredentialDiagnostic(auth_service=mock_auth_service, terraform_binary="terraform")

    captured_envs = []

    def mock_run(cmd, cwd, env, timeout, creds):
        captured_envs.append(dict(env))
        cmd_str = " ".join(cmd)
        if "init" in cmd_str:
            return {"exit_code": 0, "stdout": "Terraform has been successfully initialized!", "stderr": ""}
        elif "validate" in cmd_str:
            return {"exit_code": 0, "stdout": '{"valid": true}', "stderr": ""}
        elif "plan" in cmd_str:
            plan_stdout = (
                'Changes to Outputs:\n'
                '  + caller_account_id = "082229155432"\n'
                '  + caller_arn        = "arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/Session"\n'
                '  + caller_user_id    = "AROATEST:Session"\n'
            )
            return {"exit_code": 0, "stdout": plan_stdout, "stderr": ""}
        elif "show" in cmd_str:
            show_json = (
                '{"format_version": "1.0", "planned_values": {"outputs": {'
                '"caller_account_id": {"value": "082229155432"}, '
                '"caller_arn": {"value": "arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/Session"}, '
                '"caller_user_id": {"value": "AROATEST:Session"}'
                '}}}'
            )
            return {"exit_code": 0, "stdout": show_json, "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    with patch.object(diag, "_run_command", side_effect=mock_run):
        res = diag.run_diagnostic(
            profile_name="cloud-chat-deploy",
            region="ap-south-1",
            expected_account_id="082229155432",
            expected_role_substr="CloudChatTerraformDeployRole",
        )

    assert res["success"] is True
    assert res["stage"] == "COMPLETED"
    assert res["terraform_account_id"] == "082229155432"
    assert "CloudChatTerraformDeployRole" in res["terraform_arn"]
    assert res["target_region"] == "ap-south-1"

    # Verify subprocess environment propagation
    assert len(captured_envs) >= 3
    for env in captured_envs:
        assert env.get("AWS_PROFILE") == "cloud-chat-deploy"
        assert env.get("AWS_DEFAULT_REGION") == "ap-south-1"
        assert env.get("AWS_REGION") == "ap-south-1"
        assert env.get("AWS_SDK_LOAD_CONFIG") == "1"
        assert env.get("AWS_EC2_METADATA_DISABLED") == "true"
        assert env.get("AWS_ACCESS_KEY_ID") == "ASIAEXAMPLEKEY12345"


def test_diagnostic_credential_redaction(mock_auth_service):
    """Verify secrets and tokens are redacted from command stdout, stderr, and logs."""
    diag = TerraformCredentialDiagnostic(auth_service=mock_auth_service, terraform_binary="terraform")
    creds = mock_auth_service.get_active_credentials()

    raw_stderr = (
        "Failed AWS call with secret SECRET_KEY_M9_DIAG_TEST and session token SESSION_TOKEN_M9_DIAG_TEST"
    )

    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=1, stdout="", stderr=raw_stderr)
        out = diag._run_command(["terraform", "init"], Path("."), {}, 30, creds)

    assert "SECRET_KEY_M9_DIAG_TEST" not in out["stderr"]
    assert "[REDACTED_SECRET_KEY]" in out["stderr"]
    assert "SESSION_TOKEN_M9_DIAG_TEST" not in out["stderr"]
    assert "[REDACTED_SESSION_TOKEN]" in out["stderr"]


def test_diagnostic_identity_mismatch_handling(mock_auth_service):
    """Verify failure when caller identity or role does not match expectations."""
    diag = TerraformCredentialDiagnostic(auth_service=mock_auth_service, terraform_binary="terraform")

    # 1. Pre-check STS account mismatch
    res_acc = diag.run_diagnostic(
        profile_name="cloud-chat-deploy",
        region="ap-south-1",
        expected_account_id="999999999999",
        expected_role_substr="CloudChatTerraformDeployRole",
    )
    assert res_acc["success"] is False
    assert res_acc["stage"] == "AUTHENTICATION"
    assert "STS Account ID mismatch" in res_acc["error"]

    # 2. Pre-check STS role mismatch
    res_role = diag.run_diagnostic(
        profile_name="cloud-chat-deploy",
        region="ap-south-1",
        expected_account_id="082229155432",
        expected_role_substr="SomeOtherRoleName",
    )
    assert res_role["success"] is False
    assert res_role["stage"] == "AUTHENTICATION"
    assert "STS Role mismatch" in res_role["error"]

    # 3. Post-plan Terraform reported identity mismatch
    def mock_run_mismatch(cmd, cwd, env, timeout, creds):
        cmd_str = " ".join(cmd)
        if "init" in cmd_str or "validate" in cmd_str:
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        elif "plan" in cmd_str:
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        elif "show" in cmd_str:
            show_json = (
                '{"planned_values": {"outputs": {'
                '"caller_account_id": {"value": "111122223333"}, '
                '"caller_arn": {"value": "arn:aws:sts::111122223333:assumed-role/WrongRole/Session"}'
                '}}}'
            )
            return {"exit_code": 0, "stdout": show_json, "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    with patch.object(diag, "_run_command", side_effect=mock_run_mismatch):
        res_tf_mismatch = diag.run_diagnostic(
            profile_name="cloud-chat-deploy",
            region="ap-south-1",
            expected_account_id="082229155432",
            expected_role_substr="CloudChatTerraformDeployRole",
        )
    assert res_tf_mismatch["success"] is False
    assert res_tf_mismatch["stage"] == "VERIFY"
    assert "Terraform identity mismatch" in res_tf_mismatch["error"]


def test_diagnostic_expired_or_unavailable_credentials():
    """Verify diagnostic fails fast with clear error when credentials are expired or invalid."""
    auth_service = MagicMock(spec=AWSAuthService)
    auth_service.get_active_credentials.return_value = None
    auth_service.get_active_identity.return_value = None
    auth_service.authenticate_profile.side_effect = ConfigurationError(
        "AWS SSO / Profile session has expired. Please run 'aws sso login --profile cloud-chat-deploy' and retry."
    )
    auth_service.redact_secrets.side_effect = lambda t, **kw: t

    diag = TerraformCredentialDiagnostic(auth_service=auth_service, terraform_binary="terraform")
    res = diag.run_diagnostic(profile_name="cloud-chat-deploy", region="ap-south-1")

    assert res["success"] is False
    assert res["stage"] == "AUTHENTICATION"
    assert "expired" in res["error"].lower()


def test_diagnostic_stage_failures(mock_auth_service):
    """Verify clear failures at INIT, VALIDATE, and PLAN stages."""
    diag = TerraformCredentialDiagnostic(auth_service=mock_auth_service, terraform_binary="terraform")

    # 1. Init failure
    with patch.object(diag, "_run_command", return_value={"exit_code": 1, "stdout": "", "stderr": "Plugin not found"}):
        res_init = diag.run_diagnostic()
        assert res_init["success"] is False
        assert res_init["stage"] == "INIT"
        assert "Plugin not found" in res_init["error"]

    # 2. Validate failure
    def mock_val_fail(cmd, cwd, env, timeout, creds):
        if "init" in " ".join(cmd):
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        return {"exit_code": 1, "stdout": '{"valid": false}', "stderr": "Syntax error in main.tf"}

    with patch.object(diag, "_run_command", side_effect=mock_val_fail):
        res_val = diag.run_diagnostic()
        assert res_val["success"] is False
        assert res_val["stage"] == "VALIDATE"

    # 3. Plan failure
    def mock_plan_fail(cmd, cwd, env, timeout, creds):
        if "init" in " ".join(cmd) or "validate" in " ".join(cmd):
            return {"exit_code": 0, "stdout": '{"valid": true}', "stderr": ""}
        return {"exit_code": 1, "stdout": "", "stderr": "AccessDenied: User is not authorized to perform sts:GetCallerIdentity"}

    with patch.object(diag, "_run_command", side_effect=mock_plan_fail):
        res_plan = diag.run_diagnostic()
        assert res_plan["success"] is False
        assert res_plan["stage"] == "PLAN"
        assert "AccessDenied" in res_plan["error"]


def test_diagnostic_strictly_forbids_apply_and_destroy(mock_auth_service):
    """Verify that 'terraform apply' or 'terraform destroy' immediately raise TerraformExecutionError."""
    diag = TerraformCredentialDiagnostic(auth_service=mock_auth_service, terraform_binary="terraform")

    with pytest.raises(TerraformExecutionError) as exc_apply:
        diag._run_command(["terraform", "apply", "-auto-approve"], Path("."), {}, 30, None)
    assert "strictly forbidden" in str(exc_apply.value).lower()

    with pytest.raises(TerraformExecutionError) as exc_destroy:
        diag._run_command(["terraform", "destroy", "-auto-approve"], Path("."), {}, 30, None)
    assert "strictly forbidden" in str(exc_destroy.value).lower()


def test_diagnostic_configuration_has_zero_managed_resources():
    """Verify that generated diagnostic Terraform configuration contains zero managed resource blocks."""
    diag = TerraformCredentialDiagnostic(terraform_binary="terraform")
    tf_hcl = diag.build_diagnostic_tf_content("ap-south-1")

    assert 'provider "aws"' in tf_hcl
    assert 'region = "ap-south-1"' in tf_hcl
    assert 'data "aws_caller_identity" "current"' in tf_hcl
    assert 'output "caller_account_id"' in tf_hcl
    assert 'output "caller_arn"' in tf_hcl

    # Ensure no managed AWS resources exist
    lines = [line.strip() for line in tf_hcl.splitlines() if line.strip().startswith("resource ")]
    assert len(lines) == 0, f"Diagnostic HCL must not define any managed resources: {lines}"
