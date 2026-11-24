"""Milestone 11.1 Regression Tests: Critical Pydantic Planner Validation Fix.

Tests cover:
1. NormalizedEC2Spec construction with valid StorageSpec and NetworkSpec instances.
2. Construction from valid dictionaries.
3. Cross-module / reloaded class instance coercion without Pydantic ValidationError.
4. Existing infrastructure planner workflow (planner.plan(interpreted)).
5. Public networking mode and selected VPC/subnet preservation.
6. Storage size, volume type, and encryption preservation.
7. End-to-end flow from InterpretedInfrastructure -> Planner -> TerraformService with M11 security controls.
8. Preservation of plan integrity, approval safeguards, and zero data loss.
9. Compatibility with persisted SQLite deployment record parsed_intent.
"""

import importlib
import json
import pytest
from core.models import (
    AMIResolutionResult,
    AWSService,
    DeploymentMode,
    DeploymentRecord,
    DeploymentStatus,
    EC2ResourceSpec,
    InterpretedInfrastructure,
    NetworkSpec,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    PlannerReadinessStatus,
    StorageSpec,
    SubnetClassification,
    VolumeType,
)
from services.infrastructure_planner import InfrastructurePlanner
from services.terraform_service import TerraformService


# ==============================================================================
# 1. NormalizedEC2Spec Construction with Valid Instances and Dicts
# ==============================================================================

def test_normalized_ec2_spec_construction_with_instances():
    """Verify NormalizedEC2Spec constructs cleanly with StorageSpec and NetworkSpec instances."""
    stg = StorageSpec(size_gb=30, volume_type=VolumeType.GP3, encrypted=True)
    net = NetworkSpec(
        deployment_mode=DeploymentMode.PUBLIC,
        public_ip=True,
        vpc_id="vpc-067c82112084129a7",
        subnet_id="subnet-03b1966c12692d842",
        allowed_ssh_cidrs=["203.0.113.50/32"],
    )
    ami = AMIResolutionResult(ami_id="ami-0dee22c13ea7a9a67", status="resolved")

    spec = NormalizedEC2Spec(
        name="web-server",
        instance_type="t3.micro",
        region="ap-south-1",
        ami=ami,
        storage=stg,
        network=net,
        tags={"Env": "production"},
    )

    assert spec.name == "web-server"
    assert spec.instance_type == "t3.micro"
    assert spec.region == "ap-south-1"
    assert spec.storage.size_gb == 30
    assert spec.storage.volume_type == VolumeType.GP3
    assert spec.storage.encrypted is True
    assert spec.network.deployment_mode == DeploymentMode.PUBLIC
    assert spec.network.public_ip is True
    assert spec.network.vpc_id == "vpc-067c82112084129a7"
    assert spec.network.subnet_id == "subnet-03b1966c12692d842"
    assert spec.network.allowed_ssh_cidrs == ["203.0.113.50/32"]


def test_normalized_ec2_spec_construction_from_dictionaries():
    """Verify NormalizedEC2Spec coerces and validates valid storage, network, and AMI dictionaries."""
    stg_dict = {
        "size_gb": 40,
        "volume_type": "gp3",
        "encrypted": True,
        "iops": 3000,
        "throughput": 125,
    }
    net_dict = {
        "deployment_mode": "PRIVATE",
        "public_ip": False,
        "vpc_id": "vpc-test-999",
        "subnet_id": "subnet-test-888",
    }
    ami_dict = {
        "ami_id": "ami-12345678",
        "image_name": "al2023",
        "status": "resolved",
    }

    spec = NormalizedEC2Spec(
        name="db-server",
        instance_type="t3.medium",
        region="ap-south-1",
        ami=ami_dict,
        storage=stg_dict,
        network=net_dict,
    )

    assert isinstance(spec.storage, StorageSpec)
    assert spec.storage.size_gb == 40
    assert spec.storage.volume_type == VolumeType.GP3
    assert spec.storage.encrypted is True
    assert spec.storage.iops == 3000
    assert spec.storage.throughput == 125

    assert isinstance(spec.network, NetworkSpec)
    assert spec.network.deployment_mode == DeploymentMode.PRIVATE
    assert spec.network.public_ip is False
    assert spec.network.vpc_id == "vpc-test-999"
    assert spec.network.subnet_id == "subnet-test-888"

    assert isinstance(spec.ami, AMIResolutionResult)
    assert spec.ami.ami_id == "ami-12345678"


def test_normalized_ec2_spec_cross_reload_class_identity_coercion():
    """Verify that instances from reloaded or alternative module imports are coerced without ValidationError."""
    import core.models
    # Keep original classes
    orig_storage = StorageSpec(size_gb=15, volume_type=VolumeType.GP3, encrypted=True)
    orig_network = NetworkSpec(
        deployment_mode=DeploymentMode.PUBLIC,
        public_ip=True,
        vpc_id="vpc-reload-123",
        subnet_id="subnet-reload-456",
    )

    # Reload core.models so new class objects are created
    reloaded_models = importlib.reload(core.models)
    ReloadedNormalizedEC2Spec = reloaded_models.NormalizedEC2Spec

    # Construction must succeed without raising pydantic_core.ValidationError
    spec = ReloadedNormalizedEC2Spec(
        name="reload-server",
        instance_type="t3.micro",
        region="ap-south-1",
        storage=orig_storage,
        network=orig_network,
    )

    assert spec.storage.size_gb == 15
    assert spec.network.vpc_id == "vpc-reload-123"
    assert spec.network.subnet_id == "subnet-reload-456"
    assert spec.network.public_ip is True


# ==============================================================================
# 2. Existing Infrastructure Planner Workflow (Cost View Flow)
# ==============================================================================

def test_infrastructure_planner_workflow_preserves_values_and_succeeds():
    """Verify the exact flow called from ui/cost_view.py: planner.plan(interpreted)."""
    interpreted = InterpretedInfrastructure(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            EC2ResourceSpec(
                name="app-server",
                instance_type="t3.micro",
                storage=StorageSpec(
                    size_gb=12,
                    volume_type=VolumeType.GP3,
                    encrypted=True,
                ),
                network=NetworkSpec(
                    deployment_mode=DeploymentMode.PUBLIC,
                    public_ip=True,
                    vpc_id="vpc-067c82112084129a7",
                    subnet_id="subnet-03b1966c12692d842",
                    allowed_ssh_cidrs=[],
                ),
            )
        ],
        user_request="Launch t3.micro in ap-south-1 with public IP in vpc-067c82112084129a7",
    )

    planner = InfrastructurePlanner()
    plan_result = planner.plan(interpreted)

    # Verify plan result
    assert plan_result is not None
    assert plan_result.normalized_spec is not None
    assert plan_result.normalized_spec.region == "ap-south-1"

    norm_res = plan_result.normalized_spec.resources[0]
    assert norm_res.name == "app-server"
    assert norm_res.instance_type == "t3.micro"

    # Verify storage preservation
    assert norm_res.storage.size_gb == 12
    assert norm_res.storage.volume_type == VolumeType.GP3
    assert norm_res.storage.encrypted is True

    # Verify network preservation
    assert norm_res.network.deployment_mode == DeploymentMode.PUBLIC
    assert norm_res.network.public_ip is True
    assert norm_res.network.vpc_id == "vpc-067c82112084129a7"
    assert norm_res.network.subnet_id == "subnet-03b1966c12692d842"

    # Cost estimate was calculated
    assert plan_result.cost_estimate is not None
    assert plan_result.cost_estimate.total_monthly_cost > 0


def test_planner_from_persisted_sqlite_parsed_intent():
    """Verify that deserializing parsed_intent from SQLite and planning works without error."""
    raw_intent_dict = {
        "service": "ec2",
        "region": "ap-south-1",
        "resources": [
            {
                "name": "persisted-server",
                "instance_type": "t3.micro",
                "storage": {
                    "size_gb": 16,
                    "volume_type": "gp3",
                    "encrypted": True,
                },
                "network": {
                    "deployment_mode": "PUBLIC",
                    "public_ip": True,
                    "vpc_id": "vpc-067c82112084129a7",
                    "subnet_id": "subnet-03b1966c12692d842",
                },
            }
        ],
        "user_request": "Launch t3.micro in ap-south-1",
    }

    # Validate into InterpretedInfrastructure
    interpreted = InterpretedInfrastructure.model_validate(raw_intent_dict)

    planner = InfrastructurePlanner()
    plan_result = planner.plan(interpreted)

    assert plan_result.normalized_spec is not None
    res = plan_result.normalized_spec.resources[0]
    assert res.storage.size_gb == 16
    assert res.storage.volume_type == VolumeType.GP3
    assert res.storage.encrypted is True
    assert res.network.public_ip is True
    assert res.network.vpc_id == "vpc-067c82112084129a7"


# ==============================================================================
# 3. M11 Security Remediation & Terraform Generation Compatibility
# ==============================================================================

def test_planner_output_feeds_m11_terraform_service_cleanly():
    """Verify that normalized spec generated by planner feeds TerraformService without regressions."""
    interpreted = InterpretedInfrastructure(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            EC2ResourceSpec(
                name="m11-server",
                instance_type="t3.micro",
                storage=StorageSpec(size_gb=8, volume_type=VolumeType.GP3, encrypted=True),
                network=NetworkSpec(
                    deployment_mode=DeploymentMode.PUBLIC,
                    public_ip=True,
                    vpc_id="vpc-067c82112084129a7",
                    subnet_id="subnet-03b1966c12692d842",
                ),
            )
        ],
        user_request="Deploy t3.micro in ap-south-1",
    )

    planner = InfrastructurePlanner()
    plan_result = planner.plan(interpreted)
    norm_spec = plan_result.normalized_spec

    tf_service = TerraformService()
    main_tf = tf_service._build_main_tf(norm_spec.resources[0], norm_spec.region)

    # Scoped egress present
    assert "from_port   = 443" in main_tf
    assert "from_port   = 80" in main_tf
    assert 'protocol    = "-1"' not in main_tf

    # IAM role and instance profile present
    assert 'resource "aws_iam_role" "app_role"' in main_tf
    assert 'resource "aws_iam_instance_profile" "app_instance_profile"' in main_tf
    assert "AmazonSSMManagedInstanceCore" in main_tf

    # IMDSv2 and EBS encryption present
    assert 'http_tokens                 = "required"' in main_tf
    assert "http_put_response_hop_limit = 1" in main_tf
    assert "encrypted             = true" in main_tf
    assert 'volume_type           = "gp3"' in main_tf

    # Accurate 5 resources summary
    summary = tf_service.generate_plan_summary(norm_spec)
    assert summary["to_add"] == 5
