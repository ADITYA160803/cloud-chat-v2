from unittest.mock import MagicMock
import pytest
from core.models import (
    AMIResolutionResult,
    DependencyStatus,
    EC2ResourceSpec,
    NetworkSpec,
    StorageSpec,
)
from services.dependency_resolver import DependencyResolver


def test_dependency_resolver_default_networking():
    """Verify standard default networking dependencies."""
    resource = EC2ResourceSpec(
        name="test-server",
        instance_type="t3.micro",
        network=NetworkSpec(use_existing_vpc=False, use_existing_subnet=False),
        storage=StorageSpec(size_gb=10),
    )
    ami_result = AMIResolutionResult(
        ami_id="ami-12345678",
        status="resolved",
    )

    resolver = DependencyResolver()
    deps = resolver.resolve_dependencies("us-east-1", resource, ami_result)

    names = {d.name: d for d in deps}
    assert "AMI" in names
    assert names["AMI"].status == DependencyStatus.SATISFIED
    assert names["AMI"].value == "ami-12345678"

    assert "VPC" in names
    assert names["VPC"].status == DependencyStatus.DEFAULT_AVAILABLE

    assert "Subnet" in names
    assert names["Subnet"].status == DependencyStatus.DEFAULT_AVAILABLE


def test_dependency_resolver_missing_existing_vpc_and_subnet():
    """Verify that requesting existing VPC and subnet without IDs sets status to REQUIRES_SELECTION."""
    resource = EC2ResourceSpec(
        name="private-server",
        instance_type="t3.micro",
        network=NetworkSpec(
            use_existing_vpc=True,
            vpc_id=None,
            use_existing_subnet=True,
            subnet_id=None,
        ),
    )
    ami_result = AMIResolutionResult(ami_id="ami-12345678", status="resolved")

    resolver = DependencyResolver()
    deps = resolver.resolve_dependencies("ap-south-1", resource, ami_result)

    names = {d.name: d for d in deps}
    assert names["VPC"].status == DependencyStatus.REQUIRES_SELECTION
    assert names["Subnet"].status == DependencyStatus.REQUIRES_SELECTION


def test_dependency_resolver_subnet_vpc_mismatch():
    """Verify error when selected subnet does not belong to selected VPC."""
    mock_client = MagicMock()
    # Mock subnet call returning VpcId that differs from expected
    mock_client.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-11111111111111111"}]}
    mock_client.describe_subnets.return_value = {
        "Subnets": [{"SubnetId": "subnet-22222222222222222", "VpcId": "vpc-DIFFERENT00000000"}]
    }

    resource = EC2ResourceSpec(
        instance_type="t3.micro",
        network=NetworkSpec(
            use_existing_vpc=True,
            vpc_id="vpc-11111111111111111",
            use_existing_subnet=True,
            subnet_id="subnet-22222222222222222",
        ),
    )
    ami_result = AMIResolutionResult(ami_id="ami-12345678", status="resolved")

    resolver = DependencyResolver(ec2_client=mock_client)
    deps = resolver.resolve_dependencies("us-east-1", resource, ami_result)

    names = {d.name: d for d in deps}
    assert names["Subnet"].status == DependencyStatus.UNRESOLVED
    assert "belongs to vpc" in names["Subnet"].message.lower()


def test_dependency_resolver_vpc_discovery():
    """Verify read-only VPC discovery using mocked Boto3."""
    mock_client = MagicMock()
    mock_client.describe_vpcs.return_value = {
        "Vpcs": [
            {
                "VpcId": "vpc-0a1b2c3d4e5f67890",
                "CidrBlock": "10.0.0.0/16",
                "IsDefault": False,
                "Tags": [{"Key": "Name", "Value": "prod-vpc"}],
            }
        ]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    vpcs = resolver.discover_vpcs("ap-south-1")

    assert len(vpcs) == 1
    assert vpcs[0]["vpc_id"] == "vpc-0a1b2c3d4e5f67890"
    assert vpcs[0]["name"] == "prod-vpc"
