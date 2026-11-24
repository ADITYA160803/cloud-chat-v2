from unittest.mock import MagicMock
import pytest
from core.models import (
    AMIResolutionResult,
    AWSService,
    EC2ResourceSpec,
    InterpretedInfrastructure,
    NetworkSpec,
    PlannerReadinessStatus,
    StorageSpec,
    VolumeType,
)
from services.ami_resolver import AMIResolver
from services.infrastructure_planner import InfrastructurePlanner


def test_planner_ready_when_dependencies_satisfied():
    """Verify planner sets status READY when all requirements and dependencies are satisfied."""
    interpreted = InterpretedInfrastructure(
        service=AWSService.EC2,
        region="us-east-1",
        resources=[
            EC2ResourceSpec(
                name="app-server",
                instance_type="t3.micro",
                storage=StorageSpec(size_gb=20, volume_type=VolumeType.GP3, encrypted=True),
                network=NetworkSpec(public_ip=False, use_existing_vpc=False, use_existing_subnet=False),
            )
        ],
        user_request="Create a private EC2 instance in us-east-1 with t3.micro",
    )

    # Mock AMI resolver returning resolved AMI
    mock_ami_resolver = MagicMock()
    mock_ami_resolver.resolve.return_value = AMIResolutionResult(
        ami_id="ami-0123456789abcdef0",
        image_name="al2023-latest",
        status="resolved",
    )

    planner = InfrastructurePlanner(ami_resolver=mock_ami_resolver)
    result = planner.plan(interpreted)

    assert result.readiness == PlannerReadinessStatus.READY
    assert len(result.validation_errors) == 0
    assert result.normalized_spec is not None
    assert result.normalized_spec.region == "us-east-1"
    assert result.normalized_spec.resources[0].network.public_ip is False
    assert result.normalized_spec.resources[0].storage.encrypted is True
    assert result.cost_estimate is not None
    assert result.cost_estimate.total_monthly_cost > 0


def test_planner_needs_user_input_for_missing_existing_vpc_subnet():
    """Verify planner returns NEEDS_USER_INPUT when existing VPC/subnet are requested without IDs."""
    interpreted = InterpretedInfrastructure(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            EC2ResourceSpec(
                name="mumbai-server",
                instance_type="t3.micro",
                storage=StorageSpec(size_gb=8, volume_type=VolumeType.GP3, encrypted=True),
                network=NetworkSpec(
                    public_ip=False,
                    use_existing_vpc=True,
                    vpc_id=None,
                    use_existing_subnet=True,
                    subnet_id=None,
                ),
            )
        ],
        user_request="Create private EC2 in Mumbai with t3.micro, 8 GB gp3, using my existing VPC and subnet",
    )

    mock_ami_resolver = MagicMock()
    mock_ami_resolver.resolve.return_value = AMIResolutionResult(
        ami_id="ami-0123456789abcdef0",
        status="resolved",
    )

    planner = InfrastructurePlanner(ami_resolver=mock_ami_resolver)
    result = planner.plan(interpreted)

    # Must NOT be Ready!
    assert result.readiness == PlannerReadinessStatus.NEEDS_USER_INPUT
    assert any("VPC" in m for m in result.missing_information)
    assert any("Subnet" in m for m in result.missing_information)


def test_planner_validation_failed_for_invalid_storage():
    """Verify planner returns VALIDATION_FAILED when storage limits are violated."""
    interpreted = InterpretedInfrastructure(
        service=AWSService.EC2,
        region="us-east-1",
        resources=[
            EC2ResourceSpec(
                instance_type="t3.micro",
                storage=StorageSpec(size_gb=100, volume_type=VolumeType.GP3, iops=100),  # gp3 iops must be >= 3000
            )
        ],
        user_request="Deploy instance with invalid 100 gp3 IOPS",
    )

    planner = InfrastructurePlanner()
    result = planner.plan(interpreted)

    assert result.readiness == PlannerReadinessStatus.VALIDATION_FAILED
    assert any("iops" in err.lower() for err in result.validation_errors)



def test_planner_aws_lookup_unavailable_when_credentials_absent():
    """Verify planner returns AWS_LOOKUP_UNAVAILABLE when AMI cannot be resolved without AWS access."""
    interpreted = InterpretedInfrastructure(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            EC2ResourceSpec(
                instance_type="t3.micro",
                ami_id=None,
                network=NetworkSpec(use_existing_vpc=False, use_existing_subnet=False),
            )
        ],
        user_request="Create EC2 in Mumbai",
    )

    mock_ami_resolver = MagicMock()
    mock_ami_resolver.resolve.return_value = AMIResolutionResult(
        ami_id=None,
        status="unresolved",
        message="AWS lookup unavailable without credentials.",
    )

    planner = InfrastructurePlanner(ami_resolver=mock_ami_resolver)
    result = planner.plan(interpreted)

    assert result.readiness == PlannerReadinessStatus.AWS_LOOKUP_UNAVAILABLE
    assert result.ami_resolution.ami_id is None
