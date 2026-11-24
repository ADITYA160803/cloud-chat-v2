from unittest.mock import MagicMock, patch
import pytest
from botocore.exceptions import ClientError
from core.exceptions import ConfigurationError, ValidationError
from core.models import AWSAuthMethod, AWSIdentity
from services.aws_auth_service import AWSAuthService, AWSCredentialsContainer


def test_credentials_container_redaction():
    """Verify that AWSCredentialsContainer masks access keys and scrubs secret keys and tokens."""
    creds = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.ACCESS_KEY,
        access_key_id="AKIA1234567890EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        session_token="FwoGZXIvYXdzEJr1EXAMPLETOKENLONGSTRING1234567890",
    )

    log_entry = (
        "Execution output: authenticated with AKIA1234567890EXAMPLE, "
        "secret=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY and token FwoGZXIvYXdzEJr1EXAMPLETOKENLONGSTRING1234567890."
    )

    sanitized = creds.redact_secrets(log_entry)

    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in sanitized
    assert "[REDACTED_SECRET_KEY]" in sanitized
    assert "FwoGZXIvYXdzEJr1EXAMPLETOKENLONGSTRING1234567890" not in sanitized
    assert "[REDACTED_SESSION_TOKEN]" in sanitized
    assert "AKIA1234567890EXAMPLE" not in sanitized
    assert "AKIA...MPLE" in sanitized


def test_credentials_subprocess_env_access_key():
    """Verify environment generation for access key authentication."""
    creds = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.ACCESS_KEY,
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="secret123",
        session_token="token456",
        region="ap-south-1",
    )
    env = creds.get_subprocess_env({"EXISTING_VAR": "keep_this", "AWS_PROFILE": "old_profile"})

    assert env["EXISTING_VAR"] == "keep_this"
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"
    assert env["AWS_SECRET_ACCESS_KEY"] == "secret123"
    assert env["AWS_SESSION_TOKEN"] == "token456"
    assert env["AWS_DEFAULT_REGION"] == "ap-south-1"
    assert env["AWS_REGION"] == "ap-south-1"
    assert "AWS_PROFILE" not in env


def test_credentials_subprocess_env_profile():
    """Verify environment generation for profile authentication."""
    creds = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.PROFILE,
        profile_name="production-sso",
        region="us-west-2",
    )
    env = creds.get_subprocess_env({"AWS_ACCESS_KEY_ID": "old_key"})

    assert env["AWS_PROFILE"] == "production-sso"
    assert env["AWS_REGION"] == "us-west-2"
    assert "AWS_ACCESS_KEY_ID" not in env


@patch("boto3.Session")
def test_authenticate_access_key_success(mock_session_cls):
    """Verify successful access key authentication via STS GetCallerIdentity."""
    mock_session = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {
        "Account": "112233445566",
        "Arn": "arn:aws:iam::112233445566:user/deployer",
        "UserId": "AIDA12345678EXAMPLE",
    }
    mock_session.client.return_value = mock_sts
    mock_session_cls.return_value = mock_session

    auth_service = AWSAuthService()
    identity = auth_service.authenticate_access_key(
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="ap-south-1",
    )

    assert identity.account_id == "112233445566"
    assert identity.arn == "arn:aws:iam::112233445566:user/deployer"
    assert identity.region == "ap-south-1"
    assert identity.is_valid is True
    assert auth_service.get_active_credentials() is not None
    assert auth_service.get_active_identity() == identity


def test_authenticate_access_key_missing_fields():
    """Verify ValidationError when key or secret is missing."""
    auth_service = AWSAuthService()
    with pytest.raises(ValidationError):
        auth_service.authenticate_access_key(access_key_id="", secret_access_key="secret", region="us-east-1")

    with pytest.raises(ValidationError):
        auth_service.authenticate_access_key(access_key_id="key", secret_access_key="", region="us-east-1")


@patch("boto3.Session")
def test_authenticate_profile_success(mock_session_cls):
    """Verify successful profile authentication."""
    mock_session = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {
        "Account": "998877665544",
        "Arn": "arn:aws:sts::998877665544:assumed-role/Admin/session",
        "UserId": "AROA99887766EXAMPLE:session",
    }
    mock_session.client.return_value = mock_sts
    mock_session_cls.return_value = mock_session

    auth_service = AWSAuthService()
    identity = auth_service.authenticate_profile("developer", "us-east-1")

    assert identity.account_id == "998877665544"
    assert identity.profile_name == "developer"
    assert identity.auth_method == AWSAuthMethod.PROFILE


@patch("boto3.Session")
def test_authenticate_profile_expired_sso(mock_session_cls):
    """Verify clear error message when SSO profile token is expired."""
    mock_session = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.side_effect = ClientError(
        {"Error": {"Code": "ExpiredTokenException", "Message": "The security token included in the request is expired"}},
        "GetCallerIdentity",
    )
    mock_session.client.return_value = mock_sts
    mock_session_cls.return_value = mock_session

    auth_service = AWSAuthService()
    with pytest.raises(ConfigurationError) as exc_info:
        auth_service.authenticate_profile("sso-profile", "us-east-1")

    assert "aws sso login" in str(exc_info.value).lower()


def test_clear_credentials():
    """Verify clear wipes memory."""
    auth_service = AWSAuthService()
    auth_service._active_credentials = AWSCredentialsContainer()
    auth_service._active_identity = AWSIdentity(
        account_id="123456789012",
        arn="arn:aws:iam::123456789012:user/test",
        user_id="AID123",
    )

    auth_service.clear()
    assert auth_service.get_active_credentials() is None
    assert auth_service.get_active_identity() is None


@patch("boto3.Session")
def test_authenticate_access_key_error_sanitization(mock_session_cls):
    """Verify that sensitive secret keys are never leaked in error messages or exception strings."""
    secret_key = "MY_SUPER_SECRET_AWS_KEY_12345"
    mock_session = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.side_effect = ClientError(
        {"Error": {"Code": "InvalidClientTokenId", "Message": f"The security token using {secret_key} is invalid"}},
        "GetCallerIdentity",
    )
    mock_session.client.return_value = mock_sts
    mock_session_cls.return_value = mock_session

    auth_service = AWSAuthService()
    with pytest.raises(ConfigurationError) as exc_info:
        auth_service.authenticate_access_key(
            access_key_id="AKIA12345678EXAMPLE",
            secret_access_key=secret_key,
            region="us-east-1",
        )

    err_text = str(exc_info.value)
    assert secret_key not in err_text
    assert "[REDACTED_SECRET]" in err_text or "[REDACTED_SECRET_KEY]" in err_text
