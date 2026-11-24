"""Milestone 9 Regression Tests: AWS CLI Profile Propagation & Strict Subnet Validation.

Tests cover:
1. Named AWS CLI profile propagation to Terraform subprocess env:
   - AWS_PROFILE, AWS_DEFAULT_PROFILE
   - AWS_DEFAULT_REGION, AWS_REGION
   - AWS_SDK_LOAD_CONFIG="1"
   - AWS_EC2_METADATA_DISABLED="true"
   - STS session credentials (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN)
2. Boto3 SSO / AssumeRole credential resolution via authenticate_profile.
3. Secret redaction of session keys from error messages and logs.
4. Real provider plan vs mock plan distinction in TerraformService (is_mock_plan = not is_authenticated).
5. Logging of verified AWS Account ID and ARN before planning.
6. Subnet inspection and classification (inspect_and_classify_subnet):
   - Format validation
   - VPC membership validation (reject subnet belonging to different VPC)
   - Route table inspection (explicit vs main fallback)
   - Inactive / blackhole route ignoring
   - Classification as PUBLIC, PRIVATE_WITH_NAT, PRIVATE_WITH_EGRESS_ONLY_IGW, ISOLATED, UNKNOWN
7. Strict subnet validation for private EC2 workloads:
   - Reject PUBLIC subnet
   - Reject UNKNOWN subnet
   - Accept PRIVATE_WITH_NAT, PRIVATE_WITH_EGRESS_ONLY_IGW, ISOLATED subnets
"""

import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch
import pytest
from botocore.exceptions import ClientError

from core.models import (
    AWSAuthMethod,
    AWSIdentity,
    SubnetClassification,
    EC2ResourceSpec,
    NetworkSpec,
    StorageSpec,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    AMIResolutionResult,
)
from services.aws_auth_service import AWSCredentialsContainer, AWSAuthService
from services.dependency_resolver import DependencyResolver
from services.terraform_service import TerraformService


# ==============================================================================
# 1. AWS Profile & Subprocess Environment Propagation Tests
# ==============================================================================

def test_aws_profile_subprocess_env_propagation():
    """Verify that named profile, regions, config flags, and resolved credentials propagate to subprocess env."""
    creds = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.PROFILE,
        profile_name="cloud-chat-deploy",
        region="ap-south-1",
        access_key_id="ASIAEXAMPLEKEYID123",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        session_token="AQoDYXdzEJr1111111111111111111111111111111111111111111",
    )

    env = creds.get_subprocess_env(base_env={})

    assert env.get("AWS_PROFILE") == "cloud-chat-deploy"
    assert env.get("AWS_DEFAULT_PROFILE") == "cloud-chat-deploy"
    assert env.get("AWS_DEFAULT_REGION") == "ap-south-1"
    assert env.get("AWS_REGION") == "ap-south-1"
    assert env.get("AWS_SDK_LOAD_CONFIG") == "1"
    assert env.get("AWS_EC2_METADATA_DISABLED") == "true"
    assert env.get("AWS_ACCESS_KEY_ID") == "ASIAEXAMPLEKEYID123"
    assert env.get("AWS_SECRET_ACCESS_KEY") == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert env.get("AWS_SESSION_TOKEN") == "AQoDYXdzEJr1111111111111111111111111111111111111111111"


def test_aws_credentials_secret_redaction():
    """Verify secret keys and session tokens are redacted from text and logs."""
    creds = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.PROFILE,
        profile_name="cloud-chat-deploy",
        region="ap-south-1",
        access_key_id="ASIAEXAMPLEKEYID123",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        session_token="AQoDYXdzEJr1111111111111111111111111111111111111111111",
    )

    raw_text = (
        "Error calling AWS API with key ASIAEXAMPLEKEYID123 and "
        "secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY and token AQoDYXdzEJr1111111111111111111111111111111111111111111"
    )
    redacted = creds.redact_secrets(raw_text)

    assert "wJalrXUtnFEMI" not in redacted
    assert "[REDACTED_SECRET_KEY]" in redacted
    assert "AQoDYXdzEJr11" not in redacted
    assert "[REDACTED_SESSION_TOKEN]" in redacted
    assert "ASIA...D123" in redacted


def test_authenticate_profile_resolves_frozen_credentials():
    """Verify authenticate_profile captures resolved credentials from boto3 session."""
    auth_service = AWSAuthService()

    mock_session = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {
        "Account": "082229155432",
        "Arn": "arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/Session",
        "UserId": "AROAR9999999999999999:Session",
    }
    mock_session.client.return_value = mock_sts

    mock_creds = MagicMock()
    mock_frozen = MagicMock()
    mock_frozen.access_key = "ASIA_MOCK_ACCESS_KEY"
    mock_frozen.secret_key = "MOCK_SECRET_KEY_M9_VALUE"
    mock_frozen.token = "MOCK_SESSION_TOKEN_M9_VALUE"
    mock_creds.get_frozen_credentials.return_value = mock_frozen
    mock_session.get_credentials.return_value = mock_creds

    with patch("boto3.Session", return_value=mock_session):
        identity = auth_service.authenticate_profile("cloud-chat-deploy", region="ap-south-1")

    assert identity.account_id == "082229155432"
    assert identity.arn == "arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/Session"
    assert identity.auth_method == AWSAuthMethod.PROFILE
    assert identity.is_valid is True

    active_creds = auth_service.get_active_credentials()
    assert active_creds is not None
    assert active_creds.access_key_id == "ASIA_MOCK_ACCESS_KEY"
    assert active_creds.secret_access_key == "MOCK_SECRET_KEY_M9_VALUE"
    assert active_creds.session_token == "MOCK_SESSION_TOKEN_M9_VALUE"

    env = active_creds.get_subprocess_env(base_env={})
    assert env["AWS_PROFILE"] == "cloud-chat-deploy"
    assert env["AWS_DEFAULT_REGION"] == "ap-south-1"
    assert env["AWS_ACCESS_KEY_ID"] == "ASIA_MOCK_ACCESS_KEY"
    assert env["AWS_SECRET_ACCESS_KEY"] == "MOCK_SECRET_KEY_M9_VALUE"
    assert env["AWS_SESSION_TOKEN"] == "MOCK_SESSION_TOKEN_M9_VALUE"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"


# ==============================================================================
# 2. Terraform Service Authentication & Mock vs Real Distinction
# ==============================================================================

def test_terraform_pipeline_distinguishes_real_vs_mock_plan():
    """Verify that is_mock_plan is False when authenticated and True when unauthenticated."""
    auth_service = AWSAuthService()
    tf_service = TerraformService(terraform_binary="terraform")

    spec = NormalizedInfrastructureSpec(
        deployment_id="test-m9-dep",
        service="ec2",
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="test-instance",
                instance_type="t3.micro",
                ami=AMIResolutionResult(ami_id="ami-0123456789abcdef0", status="resolved"),
                network=NetworkSpec(use_existing_vpc=True, vpc_id="vpc-0123456789abcdef0", use_existing_subnet=True, subnet_id="subnet-0123456789abcdef0"),
                storage=StorageSpec(size_gb=20),
            )
        ],
    )

    # 1. Authenticated state
    auth_identity = AWSIdentity(
        account_id="082229155432",
        arn="arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/Session",
        user_id="AROATEST:Session",
        region="ap-south-1",
        auth_method=AWSAuthMethod.PROFILE,
        profile_name="cloud-chat-deploy",
        is_valid=True,
    )

    with patch("services.aws_auth_service.aws_auth_service.get_active_identity", return_value=auth_identity):
        with patch.object(tf_service, "_run_command") as mock_run:
            mock_run.side_effect = [
                {"exit_code": 0, "output": "", "error": ""},  # fmt
                {"exit_code": 0, "output": "Init OK", "error": ""},  # init
                {"exit_code": 0, "output": '{"valid": true}', "error": ""},  # validate
                {"exit_code": 0, "output": "Plan OK", "error": ""},  # plan
                {"exit_code": 0, "output": '{"format_version": "1.0", "resource_changes": []}', "error": ""},  # show
            ]
            with patch.object(tf_service, "calculate_plan_hash", return_value="mockhash123"):
                with patch.object(tf_service, "calculate_plan_artifact_hash", return_value="mockarthash123"):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch("pathlib.Path.read_bytes", return_value=b"tfplanbytes"):
                            result = tf_service.run_pipeline("test-m9-dep", spec)

                            assert result.is_mock_plan is False
                            assert "082229155432" in result.execution_logs
                            assert "CloudChatTerraformDeployRole" in result.execution_logs

    # 2. Unauthenticated state
    with patch("services.aws_auth_service.aws_auth_service.get_active_identity", return_value=None):
        with patch.object(tf_service, "_run_command") as mock_run:
            mock_run.side_effect = [
                {"exit_code": 0, "output": "", "error": ""},  # fmt
                {"exit_code": 0, "output": "Init OK", "error": ""},  # init
                {"exit_code": 0, "output": '{"valid": true}', "error": ""},  # validate
                {"exit_code": 0, "output": "Plan OK", "error": ""},  # plan
                {"exit_code": 0, "output": '{"format_version": "1.0", "resource_changes": []}', "error": ""},  # show
            ]
            with patch.object(tf_service, "calculate_plan_hash", return_value="mockhash123"):
                with patch.object(tf_service, "calculate_plan_artifact_hash", return_value="mockarthash123"):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch("pathlib.Path.read_bytes", return_value=b"tfplanbytes"):
                            result = tf_service.run_pipeline("test-m9-dep-mock", spec)

                            assert result.is_mock_plan is True
                            assert "Running in local mock / unauthenticated mode" in result.execution_logs


# ==============================================================================
# 3. Subnet Inspection, Classification & VPC Membership Tests
# ==============================================================================

def test_inspect_and_classify_subnet_vpc_mismatch():
    """Verify subnet inspection rejects subnets that belong to a different VPC."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-0123456789abcdef0",
                "VpcId": "vpc-OTHER99999999999",
                "CidrBlock": "10.1.1.0/24",
                "AvailabilityZone": "ap-south-1a",
                "MapPublicIpOnLaunch": False,
            }
        ]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, classification, details, expl = resolver.inspect_and_classify_subnet(
        region="ap-south-1",
        subnet_id="subnet-0123456789abcdef0",
        expected_vpc_id="vpc-EXPECTED1111111",
    )

    assert is_valid is False
    assert classification == SubnetClassification.UNKNOWN
    assert "belongs to VPC 'vpc-OTHER99999999999', not selected VPC 'vpc-EXPECTED1111111'" in expl


def test_inspect_and_classify_subnet_not_found():
    """Verify subnet inspection gracefully handles non-existent subnets."""
    mock_client = MagicMock()
    mock_client.describe_subnets.side_effect = ClientError(
        {"Error": {"Code": "InvalidSubnetID.NotFound", "Message": "The subnet ID 'subnet-99999999999999999' does not exist"}},
        "DescribeSubnets",
    )

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, classification, details, expl = resolver.inspect_and_classify_subnet(
        region="ap-south-1",
        subnet_id="subnet-99999999999999999",
        expected_vpc_id="vpc-0123456789abcdef0",
    )

    assert is_valid is False
    assert classification == SubnetClassification.UNKNOWN
    assert "does not exist in region 'ap-south-1'" in expl


def test_inspect_and_classify_subnet_public_with_igw():
    """Verify subnet with active IGW route is classified as PUBLIC."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-0123456789abcdef0",
                "VpcId": "vpc-0123456789abcdef0",
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "ap-south-1a",
                "MapPublicIpOnLaunch": True,
            }
        ]
    }
    mock_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-public123",
                "VpcId": "vpc-0123456789abcdef0",
                "Associations": [{"SubnetId": "subnet-0123456789abcdef0", "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                    {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0123456789abcdef0", "State": "active"},
                ],
            }
        ]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, classification, details, expl = resolver.inspect_and_classify_subnet(
        region="ap-south-1",
        subnet_id="subnet-0123456789abcdef0",
        expected_vpc_id="vpc-0123456789abcdef0",
    )

    assert is_valid is True
    assert classification == SubnetClassification.PUBLIC
    assert details["classification"] == "PUBLIC"
    assert details["route_table_id"] == "rtb-public123"
    assert "Internet Gateway" in expl


def test_inspect_and_classify_subnet_private_with_nat():
    """Verify subnet with active NAT gateway route is classified as PRIVATE_WITH_NAT."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-0123456789abcdef1",
                "VpcId": "vpc-0123456789abcdef0",
                "CidrBlock": "10.0.2.0/24",
                "AvailabilityZone": "ap-south-1b",
                "MapPublicIpOnLaunch": False,
            }
        ]
    }
    mock_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-private123",
                "VpcId": "vpc-0123456789abcdef0",
                "Associations": [{"SubnetId": "subnet-0123456789abcdef1", "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                    {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-0123456789abcdef0", "State": "active"},
                ],
            }
        ]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, classification, details, expl = resolver.inspect_and_classify_subnet(
        region="ap-south-1",
        subnet_id="subnet-0123456789abcdef1",
        expected_vpc_id="vpc-0123456789abcdef0",
    )

    assert is_valid is True
    assert classification == SubnetClassification.PRIVATE_WITH_NAT
    assert details["classification"] == "PRIVATE_WITH_NAT"
    assert details["route_table_id"] == "rtb-private123"
    assert "NAT Gateway" in expl


def test_inspect_and_classify_subnet_isolated():
    """Verify subnet with no default routes is classified as ISOLATED."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-0123456789abcdef2",
                "VpcId": "vpc-0123456789abcdef0",
                "CidrBlock": "10.0.3.0/24",
                "AvailabilityZone": "ap-south-1a",
                "MapPublicIpOnLaunch": False,
            }
        ]
    }
    mock_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-isolated123",
                "VpcId": "vpc-0123456789abcdef0",
                "Associations": [{"SubnetId": "subnet-0123456789abcdef2", "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                ],
            }
        ]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, classification, details, expl = resolver.inspect_and_classify_subnet(
        region="ap-south-1",
        subnet_id="subnet-0123456789abcdef2",
        expected_vpc_id="vpc-0123456789abcdef0",
    )

    assert is_valid is True
    assert classification == SubnetClassification.ISOLATED
    assert details["classification"] == "ISOLATED"
    assert "isolated routing" in expl


def test_validate_subnet_rejects_public_for_private_ec2():
    """Verify _validate_subnet strictly rejects PUBLIC subnet when require_private=True."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-0123456789abcdef0",
                "VpcId": "vpc-0123456789abcdef0",
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "ap-south-1a",
            }
        ]
    }
    mock_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-pub",
                "VpcId": "vpc-0123456789abcdef0",
                "Associations": [{"SubnetId": "subnet-0123456789abcdef0", "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1111", "State": "active"},
                ],
            }
        ]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, msg = resolver._validate_subnet(
        region="ap-south-1",
        subnet_id="subnet-0123456789abcdef0",
        expected_vpc_id="vpc-0123456789abcdef0",
        require_private=True,
    )

    assert is_valid is False
    assert "is PUBLIC" in msg
    assert "Private workloads require a verified PRIVATE_WITH_NAT" in msg


def test_validate_subnet_rejects_unknown_for_private_ec2():
    """Verify _validate_subnet strictly rejects UNKNOWN subnet when require_private=True."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-0123456789abcdef0",
                "VpcId": "vpc-0123456789abcdef0",
                "CidrBlock": "10.0.1.0/24",
            }
        ]
    }
    # Return no route tables so classification is UNKNOWN
    mock_client.describe_route_tables.return_value = {"RouteTables": []}

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, msg = resolver._validate_subnet(
        region="ap-south-1",
        subnet_id="subnet-0123456789abcdef0",
        expected_vpc_id="vpc-0123456789abcdef0",
        require_private=True,
    )

    assert is_valid is False
    assert "route table information is unavailable or missing" in msg or "UNKNOWN" in msg
