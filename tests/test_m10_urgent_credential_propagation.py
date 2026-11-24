"""Milestone 10 Urgent Fix: AWS Credential Propagation & Subnet Discovery Regression Tests.

Validates that:
1. Exact reproduction: When NoCredentialsError occurs, clear user-facing reconnect guidance is returned.
2. Authenticated AWS profile sessions with frozen STS temporary credentials properly propagate
   to AWSCredentialsContainer and Boto3 sessions without failing with ProfileNotFound or NoCredentialsError.
3. DependencyResolver reuses the authenticated session/credentials from aws_auth_service.
4. Route-table discovery uses the same authenticated session as subnet discovery.
5. DependencyResolver never silently falls back to default credentials, mock discovery, or IMDS
   when a named profile or active identity is configured.
6. Expired session tokens (ExpiredToken) produce a clear reconnect message directing the user to Settings.
7. Sensitive secrets (access keys, secret keys, session tokens) are never logged, exposed, or leaked.
8. Active account, region, and deployment role identity checks are preserved.
"""

from unittest.mock import MagicMock, patch
import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from core.exceptions import ConfigurationError
from core.models import AWSAuthMethod, AWSIdentity, SubnetClassification
from services.aws_auth_service import AWSAuthService, AWSCredentialsContainer
from services.dependency_resolver import DependencyResolver


# ==============================================================================
# 1. Exact Reproduction of "Unable to locate credentials" and Proper Handling
# ==============================================================================

def test_reproduction_unable_to_locate_credentials_returns_clear_message():
    """Reproduces the exact 'Unable to locate credentials' issue and confirms user-friendly guidance."""
    mock_client = MagicMock()
    mock_client.describe_subnets.side_effect = NoCredentialsError()

    resolver = DependencyResolver(ec2_client=mock_client)
    ok, classification, details, explanation = resolver.inspect_and_classify_subnet(
        region="ap-south-1",
        subnet_id="subnet-03b1966c12692d842",
        expected_vpc_id="vpc-067c82112084129a7",
    )

    assert ok is False
    assert classification == SubnetClassification.UNKNOWN
    assert "Unable to locate credentials" in explanation
    assert "Please connect your AWS profile in Settings" in explanation


def test_validate_subnet_handles_no_credentials_error():
    """Confirms _validate_subnet handles NoCredentialsError cleanly with actionable guidance."""
    mock_client = MagicMock()
    mock_client.describe_subnets.side_effect = NoCredentialsError()

    resolver = DependencyResolver(ec2_client=mock_client)
    ok, msg = resolver._validate_subnet(
        region="ap-south-1",
        subnet_id="subnet-03b1966c12692d842",
        expected_vpc_id="vpc-067c82112084129a7",
        require_public=True,
    )

    assert ok is False
    assert "Unable to locate credentials" in msg
    assert "Please connect your AWS profile in Settings" in msg


# ==============================================================================
# 2. Frozen Profile Credentials Propagation to Boto3 Sessions
# ==============================================================================

def test_profile_frozen_credentials_used_by_get_boto3_session():
    """Verifies that frozen STS temporary credentials resolved from a profile session

    are directly utilized by AWSCredentialsContainer.get_boto3_session() without
    re-requiring profile file lookup.
    """
    container = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.PROFILE,
        access_key_id="ASIAEXAMPLEKEY12345",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        session_token="AQoDYXdzEJr1EXAMPLETOKEN",
        profile_name="cloud-chat-deploy",
        region="ap-south-1",
    )

    session = container.get_boto3_session("ap-south-1")
    creds = session.get_credentials()
    assert creds is not None
    assert creds.access_key == "ASIAEXAMPLEKEY12345"
    assert creds.secret_key == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert creds.token == "AQoDYXdzEJr1EXAMPLETOKEN"
    assert session.region_name == "ap-south-1"


def test_aws_auth_service_client_creation_with_frozen_credentials():
    """Verifies that AWSAuthService.get_boto3_client creates a valid client using frozen credentials."""
    auth_service = AWSAuthService()
    container = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.PROFILE,
        access_key_id="ASIAEXAMPLEKEY12345",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        session_token="AQoDYXdzEJr1EXAMPLETOKEN",
        profile_name="cloud-chat-deploy",
        region="ap-south-1",
    )
    identity = AWSIdentity(
        account_id="082229155432",
        arn="arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/Session",
        user_id="AROAIEXAMPLE:Session",
        region="ap-south-1",
        auth_method=AWSAuthMethod.PROFILE,
        profile_name="cloud-chat-deploy",
        is_valid=True,
    )
    auth_service.set_active_credentials(container, identity)

    client = auth_service.get_boto3_client("ec2", region_name="ap-south-1")
    assert client is not None
    assert client.meta.region_name == "ap-south-1"


# ==============================================================================
# 3. DependencyResolver Credential Propagation and Route Table Discovery
# ==============================================================================

def test_dependency_resolver_propagates_auth_service_credentials():
    """Verifies that DependencyResolver uses the active auth_service to discover subnets and route tables."""
    auth_service = AWSAuthService()
    mock_ec2_client = MagicMock()
    mock_ec2_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-03b1966c12692d842",
                "VpcId": "vpc-067c82112084129a7",
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "ap-south-1a",
                "MapPublicIpOnLaunch": True,
            }
        ]
    }
    mock_ec2_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-0fe1ae8a254708f62",
                "VpcId": "vpc-067c82112084129a7",
                "Associations": [{"SubnetId": "subnet-03b1966c12692d842", "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                    {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-04bd4a62a5ef9989d", "State": "active"},
                ],
            }
        ]
    }

    # Patch get_boto3_client on auth_service to return our mock
    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2_client) as mock_get_client:
        container = AWSCredentialsContainer(
            auth_method=AWSAuthMethod.PROFILE,
            access_key_id="ASIAEXAMPLEKEY12345",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="AQoDYXdzEJr1EXAMPLETOKEN",
            profile_name="cloud-chat-deploy",
            region="ap-south-1",
        )
        identity = AWSIdentity(
            account_id="082229155432",
            arn="arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/Session",
            user_id="AROAIEXAMPLE:Session",
            region="ap-south-1",
            auth_method=AWSAuthMethod.PROFILE,
            profile_name="cloud-chat-deploy",
            is_valid=True,
        )
        auth_service.set_active_credentials(container, identity)

        resolver = DependencyResolver(auth_service=auth_service)
        subnets = resolver.discover_subnets("ap-south-1", vpc_id="vpc-067c82112084129a7", refresh=True)

        assert len(subnets) == 1
        assert subnets[0]["subnet_id"] == "subnet-03b1966c12692d842"
        assert subnets[0]["classification"] == "PUBLIC"
        assert mock_get_client.called
        assert mock_ec2_client.describe_subnets.called
        assert mock_ec2_client.describe_route_tables.called


def test_route_table_discovery_uses_same_authenticated_session():
    """Verifies that route-table discovery specifically uses the authenticated session and client."""
    auth_service = AWSAuthService()
    mock_ec2_client = MagicMock()
    mock_ec2_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-0fe1ae8a254708f62",
                "VpcId": "vpc-067c82112084129a7",
                "Associations": [],
                "Routes": [],
            }
        ]
    }

    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2_client):
        container = AWSCredentialsContainer(
            auth_method=AWSAuthMethod.PROFILE,
            access_key_id="ASIAEXAMPLEKEY12345",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="AQoDYXdzEJr1EXAMPLETOKEN",
            profile_name="cloud-chat-deploy",
            region="ap-south-1",
        )
        auth_service.set_active_credentials(container, None)

        resolver = DependencyResolver(auth_service=auth_service)
        rtbs = resolver.discover_route_tables("ap-south-1", vpc_id="vpc-067c82112084129a7")
        assert len(rtbs) == 1
        assert rtbs[0]["RouteTableId"] == "rtb-0fe1ae8a254708f62"


# ==============================================================================
# 4. Strict Prevention of Silent Fallback to Default Credentials or IMDS
# ==============================================================================

def test_no_silent_fallback_when_named_profile_credentials_missing():
    """When a named profile / identity is configured, but credentials are missing,

    DependencyResolver must NOT silently fall back to default credentials or IMDS.
    It must raise ConfigurationError with instructions to reconnect.
    """
    auth_service = AWSAuthService()
    identity = AWSIdentity(
        account_id="082229155432",
        arn="arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/Session",
        user_id="AROAIEXAMPLE:Session",
        region="ap-south-1",
        auth_method=AWSAuthMethod.PROFILE,
        profile_name="cloud-chat-deploy",
        is_valid=True,
    )
    # Set identity but NO active credentials (simulating cleared or lost in-memory container)
    auth_service.set_active_credentials(None, identity)

    resolver = DependencyResolver(auth_service=auth_service)

    with pytest.raises(ConfigurationError) as exc_info:
        resolver._get_client("ap-south-1")

    assert "cloud-chat-deploy" in str(exc_info.value)
    assert "Please reconnect your AWS profile in Settings" in str(exc_info.value)


# ==============================================================================
# 5. Session Expiration Guidance
# ==============================================================================

def test_expired_session_token_produces_clear_reconnect_message():
    """When an ExpiredToken ClientError is encountered during discovery,

    DependencyResolver must return an explicit reconnect message.
    """
    mock_client = MagicMock()
    error_response = {
        "Error": {
            "Code": "ExpiredToken",
            "Message": "The security token included in the request is expired",
        }
    }
    mock_client.describe_subnets.side_effect = ClientError(error_response, "DescribeSubnets")

    resolver = DependencyResolver(ec2_client=mock_client)
    ok, classification, details, explanation = resolver.inspect_and_classify_subnet(
        region="ap-south-1",
        subnet_id="subnet-03b1966c12692d842",
        expected_vpc_id="vpc-067c82112084129a7",
    )

    assert ok is False
    assert classification == SubnetClassification.UNKNOWN
    assert "session token has expired" in explanation.lower()
    assert "Please reconnect your AWS profile in Settings" in explanation


def test_validate_subnet_expired_session_message():
    """_validate_subnet returns a clear reconnect message on expired tokens."""
    mock_client = MagicMock()
    error_response = {
        "Error": {
            "Code": "ExpiredToken",
            "Message": "The security token included in the request is expired",
        }
    }
    mock_client.describe_subnets.side_effect = ClientError(error_response, "DescribeSubnets")

    resolver = DependencyResolver(ec2_client=mock_client)
    ok, msg = resolver._validate_subnet(
        region="ap-south-1",
        subnet_id="subnet-03b1966c12692d842",
        expected_vpc_id="vpc-067c82112084129a7",
        require_public=True,
    )

    assert ok is False
    assert "session token has expired" in msg.lower()
    assert "Please reconnect your AWS profile in Settings" in msg


# ==============================================================================
# 6. Sensitive Secrets Protection
# ==============================================================================

def test_secrets_redaction_never_exposes_keys_or_tokens():
    """Confirms that secret access keys and session tokens are strictly redacted from output."""
    container = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.ACCESS_KEY,
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        session_token="AQoDYXdzEJr1EXAMPLETOKEN1234567890",
        region="ap-south-1",
    )

    raw_error = (
        "ClientError: Auth failure with wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY "
        "and token AQoDYXdzEJr1EXAMPLETOKEN1234567890 for key AKIAIOSFODNN7EXAMPLE"
    )
    redacted = container.redact_secrets(raw_error)

    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in redacted
    assert "AQoDYXdzEJr1EXAMPLETOKEN1234567890" not in redacted
    assert "[REDACTED_SECRET_KEY]" in redacted
    assert "[REDACTED_SESSION_TOKEN]" in redacted
    assert "AKIA...MPLE" in redacted
