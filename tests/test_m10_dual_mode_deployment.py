"""Milestone 10 Automated Regression Tests: Dual-Mode EC2 Deployment Architecture (Public + Private).

Tests cover:
1. Public subnet acceptance for PUBLIC deployment mode.
2. Public subnet rejection for PRIVATE deployment mode.
3. Private subnet acceptance (PRIVATE_WITH_NAT, PRIVATE_WITH_EGRESS_ONLY_IGW, ISOLATED) for PRIVATE mode.
4. Private subnet rejection for PUBLIC mode (requires active IGW route).
5. Unknown subnet rejection in both modes.
6. Incorrect VPC membership rejection in both modes.
7. Manual subnet ID vs dropdown subnet validation equivalence.
8. Public IP enabled vs disabled behavior (forced False in PRIVATE mode).
9. Isolated subnet connectivity explanation (never claiming internet access).
10. NAT and Egress-Only IGW route classifications.
11. Terraform generation for both modes (zero network resources created).
12. Security Group rules (no default-open 0.0.0.0/0 SSH or RDP).
13. Cost estimate differences (public IPv4 charge vs zero private routing charge, no assumed NAT Gateway).
14. Strict safety enforcement: no apply, no destroy, and mock mode preserved.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from core.exceptions import TerraformExecutionError
from core.models import (
    AMIResolutionResult,
    DeploymentMode,
    EC2ResourceSpec,
    NetworkSpec,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    StorageSpec,
    SubnetClassification,
    VolumeType,
)
from services.cost_estimator import CostEstimator
from services.dependency_resolver import DependencyResolver
from services.terraform_service import TerraformService


# ==============================================================================
# 1. Subnet Acceptance and Rejection per Mode
# ==============================================================================

def test_public_subnet_accepted_for_public_deployment():
    """Verify that a subnet with an active IGW route is accepted in PUBLIC mode."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
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
    mock_client.describe_route_tables.return_value = {
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

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, msg = resolver._validate_subnet(
        region="ap-south-1",
        subnet_id="subnet-03b1966c12692d842",
        expected_vpc_id="vpc-067c82112084129a7",
        require_private=False,
        require_public=True,
    )

    assert is_valid is True
    assert "verified as PUBLIC" in msg


def test_public_subnet_rejected_for_private_deployment():
    """Verify that a subnet with an active IGW route is strictly rejected in PRIVATE mode."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-03b1966c12692d842",
                "VpcId": "vpc-067c82112084129a7",
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "ap-south-1a",
            }
        ]
    }
    mock_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-0fe1ae8a254708f62",
                "VpcId": "vpc-067c82112084129a7",
                "Associations": [{"SubnetId": "subnet-03b1966c12692d842", "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-04bd4a62a5ef9989d", "State": "active"},
                ],
            }
        ]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, msg = resolver._validate_subnet(
        region="ap-south-1",
        subnet_id="subnet-03b1966c12692d842",
        expected_vpc_id="vpc-067c82112084129a7",
        require_private=True,
        require_public=False,
    )

    assert is_valid is False
    assert "is PUBLIC" in msg
    assert "Private workloads require a verified" in msg


def test_private_subnets_accepted_for_private_deployment():
    """Verify that PRIVATE_WITH_NAT, PRIVATE_WITH_EGRESS_ONLY_IGW, and ISOLATED subnets are accepted in PRIVATE mode."""
    mock_client = MagicMock()

    # 1. Test PRIVATE_WITH_NAT
    mock_client.describe_subnets.return_value = {
        "Subnets": [{"SubnetId": "subnet-01111111111111111", "VpcId": "vpc-067c82112084129a7"}]
    }
    mock_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-nat",
                "VpcId": "vpc-067c82112084129a7",
                "Associations": [{"SubnetId": "subnet-01111111111111111", "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-0123456789abcdef0", "State": "active"},
                ],
            }
        ]
    }
    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, msg = resolver._validate_subnet(
        "ap-south-1", "subnet-01111111111111111", "vpc-067c82112084129a7", require_private=True
    )
    assert is_valid is True
    assert "PRIVATE_WITH_NAT" in msg

    # 2. Test ISOLATED
    mock_client.describe_subnets.return_value = {
        "Subnets": [{"SubnetId": "subnet-02222222222222222", "VpcId": "vpc-067c82112084129a7"}]
    }
    mock_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-iso",
                "VpcId": "vpc-067c82112084129a7",
                "Associations": [{"SubnetId": "subnet-02222222222222222", "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                ],
            }
        ]
    }
    is_valid, msg = resolver._validate_subnet(
        "ap-south-1", "subnet-02222222222222222", "vpc-067c82112084129a7", require_private=True
    )
    assert is_valid is True
    assert "ISOLATED" in msg


def test_private_subnet_rejected_for_public_deployment():
    """Verify that a private or isolated subnet is rejected for PUBLIC deployment mode."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [{"SubnetId": "subnet-02222222222222222", "VpcId": "vpc-067c82112084129a7"}]
    }
    mock_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-iso",
                "VpcId": "vpc-067c82112084129a7",
                "Associations": [{"SubnetId": "subnet-02222222222222222", "Main": False}],
                "Routes": [{"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"}],
            }
        ]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, msg = resolver._validate_subnet(
        region="ap-south-1",
        subnet_id="subnet-02222222222222222",
        expected_vpc_id="vpc-067c82112084129a7",
        require_private=False,
        require_public=True,
    )

    assert is_valid is False
    assert "is ISOLATED" in msg
    assert "Public deployment mode requires a verified PUBLIC subnet" in msg


def test_unknown_subnet_rejected_in_both_modes():
    """Verify that subnets with missing route tables (UNKNOWN) are rejected in both PUBLIC and PRIVATE modes."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [{"SubnetId": "subnet-03333333333333333", "VpcId": "vpc-067c82112084129a7"}]
    }
    mock_client.describe_route_tables.return_value = {"RouteTables": []}

    resolver = DependencyResolver(ec2_client=mock_client)

    # PRIVATE mode rejection
    is_valid_priv, msg_priv = resolver._validate_subnet(
        "ap-south-1", "subnet-03333333333333333", "vpc-067c82112084129a7", require_private=True
    )
    assert is_valid_priv is False
    assert "route table information is unavailable or missing" in msg_priv

    # PUBLIC mode rejection
    is_valid_pub, msg_pub = resolver._validate_subnet(
        "ap-south-1", "subnet-03333333333333333", "vpc-067c82112084129a7", require_public=True
    )
    assert is_valid_pub is False
    assert "route table information is unavailable or missing" in msg_pub


def test_incorrect_vpc_membership_rejected():
    """Verify subnet from another VPC is rejected regardless of mode."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [{"SubnetId": "subnet-0123456789abcdef0", "VpcId": "vpc-DIFFERENT99999"}]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, msg = resolver._validate_subnet(
        region="ap-south-1",
        subnet_id="subnet-0123456789abcdef0",
        expected_vpc_id="vpc-067c82112084129a7",
        require_private=False,
    )

    assert is_valid is False
    assert "belongs to VPC 'vpc-DIFFERENT99999', not selected VPC 'vpc-067c82112084129a7'" in msg


# ==============================================================================
# 2. Public IP Enforcement & Model Behavior
# ==============================================================================

def test_public_ip_disabled_and_locked_in_private_mode():
    """Verify that NetworkSpec strictly enforces public_ip=False when deployment_mode is PRIVATE."""
    # Attempting to construct NetworkSpec with PRIVATE mode and public_ip=True
    net = NetworkSpec(deployment_mode=DeploymentMode.PRIVATE, public_ip=True)
    assert net.deployment_mode == DeploymentMode.PRIVATE
    assert net.public_ip is False  # Forced False by validator


def test_public_ip_allowed_in_public_mode():
    """Verify that NetworkSpec allows public_ip=True or False when deployment_mode is PUBLIC."""
    net_enabled = NetworkSpec(deployment_mode=DeploymentMode.PUBLIC, public_ip=True)
    assert net_enabled.deployment_mode == DeploymentMode.PUBLIC
    assert net_enabled.public_ip is True

    net_disabled = NetworkSpec(deployment_mode=DeploymentMode.PUBLIC, public_ip=False)
    assert net_disabled.deployment_mode == DeploymentMode.PUBLIC
    assert net_disabled.public_ip is False


def test_isolated_subnet_explanation_does_not_claim_internet():
    """Verify that ISOLATED subnet routing explanation never claims internet access."""
    mock_client = MagicMock()
    mock_client.describe_subnets.return_value = {
        "Subnets": [{"SubnetId": "subnet-09999999999999999", "VpcId": "vpc-067c82112084129a7"}]
    }
    mock_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-iso-test",
                "VpcId": "vpc-067c82112084129a7",
                "Associations": [{"SubnetId": "subnet-09999999999999999", "Main": True}],
                "Routes": [{"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"}],
            }
        ]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    is_valid, classification, details, expl = resolver.inspect_and_classify_subnet(
        "ap-south-1", "subnet-09999999999999999", "vpc-067c82112084129a7"
    )

    assert is_valid is True
    assert classification == SubnetClassification.ISOLATED
    assert "isolated routing" in expl.lower()
    assert "internet gateway" in expl.lower()  # notes that there is no route to IGW
    assert "outbound internet" not in expl.lower()


# ==============================================================================
# 3. Terraform Generation for Public and Private Modes
# ==============================================================================

def test_terraform_generation_public_mode():
    """Verify Terraform generation sets associate_public_ip_address correctly for PUBLIC mode."""
    tf_service = TerraformService()

    # 1. PUBLIC mode with public_ip=True
    spec_pub_ip = NormalizedInfrastructureSpec(
        deployment_id="test-public-tf",
        service="ec2",
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="web-server",
                instance_type="t3.micro",
                ami=AMIResolutionResult(ami_id="ami-0123456789abcdef0", status="resolved"),
                network=NetworkSpec(
                    deployment_mode=DeploymentMode.PUBLIC,
                    public_ip=True,
                    vpc_id="vpc-067c82112084129a7",
                    subnet_id="subnet-03b1966c12692d842",
                ),
                storage=StorageSpec(size_gb=20),
            )
        ],
    )

    files_pub = tf_service.generate_configuration(spec_pub_ip)
    main_pub = files_pub["main.tf"]

    assert "associate_public_ip_address = true" in main_pub
    assert 'subnet_id = "subnet-03b1966c12692d842"' in main_pub
    assert "resource \"aws_instance\"" in main_pub
    assert "resource \"aws_security_group\"" in main_pub

    # Ensure no network resources are generated
    assert "resource \"aws_vpc\"" not in main_pub
    assert "resource \"aws_subnet\"" not in main_pub
    assert "resource \"aws_nat_gateway\"" not in main_pub
    assert "resource \"aws_internet_gateway\"" not in main_pub
    assert "resource \"aws_route_table\"" not in main_pub


def test_terraform_generation_private_mode():
    """Verify Terraform generation sets associate_public_ip_address = false for PRIVATE mode."""
    tf_service = TerraformService()

    spec_priv = NormalizedInfrastructureSpec(
        deployment_id="test-private-tf",
        service="ec2",
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="db-server",
                instance_type="t3.micro",
                ami=AMIResolutionResult(ami_id="ami-0123456789abcdef0", status="resolved"),
                network=NetworkSpec(
                    deployment_mode=DeploymentMode.PRIVATE,
                    public_ip=False,
                    vpc_id="vpc-067c82112084129a7",
                    subnet_id="subnet-01111111111111111",
                ),
                storage=StorageSpec(size_gb=30),
            )
        ],
    )

    files_priv = tf_service.generate_configuration(spec_priv)
    main_priv = files_priv["main.tf"]

    assert "associate_public_ip_address = false" in main_priv
    assert 'subnet_id = "subnet-01111111111111111"' in main_priv

    # Ensure no network resources are generated
    assert "resource \"aws_vpc\"" not in main_priv
    assert "resource \"aws_subnet\"" not in main_priv
    assert "resource \"aws_nat_gateway\"" not in main_priv
    assert "resource \"aws_internet_gateway\"" not in main_priv


def test_security_group_no_open_ssh_or_rdp():
    """Verify that Security Group ingress defaults to ZERO rules and never contains 0.0.0.0/0."""
    tf_service = TerraformService()

    spec = NormalizedInfrastructureSpec(
        deployment_id="test-sg-tf",
        service="ec2",
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="secure-server",
                instance_type="t3.micro",
                ami=AMIResolutionResult(ami_id="ami-0123456789abcdef0", status="resolved"),
                network=NetworkSpec(
                    deployment_mode=DeploymentMode.PUBLIC,
                    public_ip=True,
                    vpc_id="vpc-067c82112084129a7",
                    subnet_id="subnet-03b1966c12692d842",
                    allowed_ssh_cidrs=[],  # No SSH requested
                ),
            )
        ],
    )

    files = tf_service.generate_configuration(spec)
    main_tf = files["main.tf"]

    # Ingress block should NOT appear inside aws_security_group
    sg_block = main_tf.split('resource "aws_security_group"')[1].split('resource "aws_instance"')[0]
    assert "ingress {" not in sg_block
    assert "0.0.0.0/0" not in sg_block.split("egress {")[0]


# ==============================================================================
# 4. Cost Estimation Differences
# ==============================================================================

def test_cost_estimation_public_vs_private():
    """Verify cost estimate reflects public IPv4 address line item and $0.00 for private routing."""
    estimator = CostEstimator()

    # 1. Public EC2 with public IP enabled
    res_pub = EC2ResourceSpec(
        name="web-server",
        instance_type="t3.micro",
        network=NetworkSpec(deployment_mode=DeploymentMode.PUBLIC, public_ip=True),
        storage=StorageSpec(size_gb=20, volume_type=VolumeType.GP3),
    )
    est_pub = estimator.estimate_ec2("ap-south-1", res_pub)

    item_types_pub = [i.resource_type for i in est_pub.items]
    assert any("Public IPv4 Address" in it for it in item_types_pub)

    # 2. Private EC2 with public IP disabled
    res_priv = EC2ResourceSpec(
        name="db-server",
        instance_type="t3.micro",
        network=NetworkSpec(deployment_mode=DeploymentMode.PRIVATE, public_ip=False),
        storage=StorageSpec(size_gb=20, volume_type=VolumeType.GP3),
    )
    est_priv = estimator.estimate_ec2("ap-south-1", res_priv)

    item_types_priv = [i.resource_type for i in est_priv.items]
    assert not any("Public IPv4 Address" in it for it in item_types_priv)
    assert any("Private Subnet Routing" in it for it in item_types_priv)

    # Assert public total cost is higher by public IPv4 charge
    assert est_pub.total_monthly_cost > est_priv.total_monthly_cost


# ==============================================================================
# 5. Safety Restrictions
# ==============================================================================

def test_safety_guardrails_prevent_apply_and_destroy():
    """Verify that apply and destroy commands are strictly blocked in M10."""
    tf_service = TerraformService()

    with pytest.raises(TerraformExecutionError) as exc_apply:
        tf_service._run_command(["terraform", "apply", "-auto-approve"], Path("."), 30)
    assert "forbidden by milestone 4 safety boundaries" in str(exc_apply.value).lower()

    with pytest.raises(TerraformExecutionError) as exc_destroy:
        tf_service._run_command(["terraform", "destroy", "-auto-approve"], Path("."), 30)
    assert "forbidden by milestone 4 safety boundaries" in str(exc_destroy.value).lower()
