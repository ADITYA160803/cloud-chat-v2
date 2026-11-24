import json
from pathlib import Path
import pytest
from core.models import (
    ApprovalStatus,
    DeploymentRecord,
    DeploymentStatus,
    InterpretedInfrastructure,
    NormalizedInfrastructureSpec,
    VerificationStatus,
)
from database.db import init_db
from database.repository import DeploymentRepository
from services.infrastructure_planner import InfrastructurePlanner
from services.terraform_service import TerraformService
from ui.verification_view import _extract_resources_from_state
from ui.resources_view import extract_deployment_resources


@pytest.fixture
def temp_repo(tmp_path):
    db_file = tmp_path / "test_cloud_chat.db"
    init_db(db_file)
    return DeploymentRepository(db_path=db_file)


def test_reconstruct_normalized_spec_from_parsed_intent():
    """Verify that normalized infrastructure spec can be reconstructed from parsed_intent without loss."""
    raw_intent = {
        "service": "ec2",
        "region": "ap-south-1",
        "resources": [{
            "name": "app-server",
            "service": "ec2",
            "instance_type": "t3.micro",
            "operating_system": "amazon-linux-2023",
            "storage_gb": 8,
            "volume_type": "gp3",
            "storage_encrypted": True,
            "public_ip": True,
            "network": {
                "vpc_id": "vpc-067c82112084129a7",
                "subnet_id": "subnet-067ec34fd443e6195",
                "subnet_type": "public",
                "public_ip": True,
                "inbound_rules": [],
                "outbound_rules": [
                    {"protocol": "tcp", "from_port": 80, "to_port": 80, "cidr_blocks": ["0.0.0.0/0"]},
                    {"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr_blocks": ["0.0.0.0/0"]},
                ]
            },
            "iam_role": "AmazonSSMManagedInstanceCore",
            "use_existing_vpc": True,
            "use_existing_subnet": True,
        }],
        "assumptions": [],
        "user_request": "Deploy public EC2 in ap-south-1",
    }

    interp = InterpretedInfrastructure.model_validate(raw_intent)
    planner_res = InfrastructurePlanner().plan(interp)
    norm_spec = planner_res.normalized_spec

    assert norm_spec is not None
    assert norm_spec.region == "ap-south-1"
    assert len(norm_spec.resources) == 1
    res = norm_spec.resources[0]
    assert res.instance_type == "t3.micro"
    assert res.network.vpc_id == "vpc-067c82112084129a7"
    assert res.network.subnet_id == "subnet-067ec34fd443e6195"
    assert res.network.public_ip is True


def test_extract_resources_from_tfstate(tmp_path):
    """Verify that real resource IDs, public IP, and private IP are parsed from workspace tfstate without mock defaults."""
    state_file = tmp_path / "terraform.tfstate"
    state_content = {
        "version": 4,
        "terraform_version": "1.16.5",
        "resources": [
            {
                "type": "aws_instance",
                "name": "app_server",
                "instances": [{
                    "attributes": {
                        "id": "i-01ef153cd68711430",
                        "instance_type": "t3.micro",
                        "public_ip": "52.66.51.85",
                        "private_ip": "10.0.2.48",
                        "subnet_id": "subnet-067ec34fd443e6195",
                    }
                }]
            },
            {
                "type": "aws_security_group",
                "name": "app_sg",
                "instances": [{
                    "attributes": {
                        "id": "sg-06b5b95c6766bca27",
                        "vpc_id": "vpc-067c82112084129a7",
                    }
                }]
            },
            {
                "type": "aws_iam_role",
                "name": "app_role",
                "instances": [{
                    "attributes": {
                        "id": "app-server-role-20261122140107195200000001",
                    }
                }]
            }
        ]
    }
    state_file.write_text(json.dumps(state_content), encoding="utf-8")

    res_info = _extract_resources_from_state(tmp_path)
    assert res_info["instance_id"] == "i-01ef153cd68711430"
    assert res_info["public_ip"] == "52.66.51.85"
    assert res_info["private_ip"] == "10.0.2.48"
    assert res_info["security_group_id"] == "sg-06b5b95c6766bca27"
    assert res_info["vpc_id"] == "vpc-067c82112084129a7"
    assert res_info["subnet_id"] == "subnet-067ec34fd443e6195"


def test_repository_update_normalized_spec_and_record(temp_repo):
    """Verify repository update_normalized_spec and update_deployment_record."""
    rec = DeploymentRecord(
        deployment_id="dep-test-verif-01",
        user_request="Deploy t3.micro",
        service="ec2",
        status=DeploymentStatus.PARTIALLY_DEPLOYED,
    )
    temp_repo.create(rec)

    spec_data = {"service": "ec2", "region": "ap-south-1", "resources": []}
    plan_data = {"resources_to_create": 5}

    ok = temp_repo.update_normalized_spec("dep-test-verif-01", spec_data, plan_data)
    assert ok is True

    fetched = temp_repo.get("dep-test-verif-01")
    assert fetched.normalized_spec == spec_data
    assert fetched.plan_summary == plan_data

    # Update to COMPLETED with real resource IDs
    ok2 = temp_repo.update_deployment_record(
        deployment_id="dep-test-verif-01",
        status=DeploymentStatus.COMPLETED,
        aws_resource_ids=["i-01ef153cd68711430", "sg-06b5b95c6766bca27"],
        terraform_exit_code=0,
    )
    assert ok2 is True

    fetched2 = temp_repo.get("dep-test-verif-01")
    assert fetched2.status == DeploymentStatus.COMPLETED
    assert fetched2.aws_resource_ids == ["i-01ef153cd68711430", "sg-06b5b95c6766bca27"]
    assert fetched2.terraform_exit_code == 0


def test_extract_deployment_resources_real_deployment():
    """Verify that extract_deployment_resources extracts real AWS deployment attributes accurately."""
    rec = DeploymentRecord(
        deployment_id="98c69e2e-d5b2-4a37-942d-3f2c0eafc300",
        user_request="Deploy public EC2 in ap-south-1",
        status=DeploymentStatus.COMPLETED,
        execution_mode="real",
        aws_account_id="082229155432",
        aws_region="ap-south-1",
        aws_resource_ids=["i-01ef153cd68711430", "sg-06b5b95c6766bca27"],
        verification_status=VerificationStatus.VERIFIED,
        verification_details={
            "resources": [{
                "resource_type": "aws_instance",
                "resource_id": "i-01ef153cd68711430",
                "raw_details": {
                    "InstanceId": "i-01ef153cd68711430",
                    "InstanceType": "t3.micro",
                    "State": "running",
                    "VpcId": "vpc-067c82112084129a7",
                    "SubnetId": "subnet-067ec34fd443e6195",
                    "PublicIp": "52.66.51.85",
                    "PrivateIp": "10.0.2.48",
                }
            }],
            "checks": [
                {"property_name": "Group Existence", "actual_value": "sg-06b5b95c6766bca27"},
                {"property_name": "Public IP Exposure", "actual_value": "52.66.51.85"},
            ]
        }
    )

    info = extract_deployment_resources(rec)
    assert info["instance_id"] == "i-01ef153cd68711430"
    assert info["public_ip"] == "52.66.51.85"
    assert info["private_ip"] == "10.0.2.48"
    assert info["security_group_id"] == "sg-06b5b95c6766bca27"
    assert info["instance_type"] == "t3.micro"
    assert info["instance_state"] == "running"
    assert info["vpc_id"] == "vpc-067c82112084129a7"
    assert info["subnet_id"] == "subnet-067ec34fd443e6195"


def test_extract_deployment_resources_missing_values_show_not_available():
    """Verify that any missing/unassigned value is strictly reported as 'Not available'."""
    rec = DeploymentRecord(
        deployment_id="dep-empty-01",
        user_request="Bare intent without applied state",
        status=DeploymentStatus.DRAFT,
        aws_resource_ids=[],
        verification_details=None,
    )

    info = extract_deployment_resources(rec)
    assert info["instance_id"] == "Not available"
    assert info["public_ip"] == "Not available"
    assert info["private_ip"] == "Not available"
    assert info["security_group_id"] == "Not available"
    assert info["instance_type"] == "Not available"
    assert info["instance_state"] == "Not available"
    assert info["vpc_id"] == "Not available"
    assert info["subnet_id"] == "Not available"


def test_cross_deployment_duplicate_handling():
    """Verify that the same resource ID is never duplicated across multiple deployment cards."""
    rec_a = DeploymentRecord(
        deployment_id="dep-alpha-01",
        user_request="First deployment",
        status=DeploymentStatus.COMPLETED,
        aws_resource_ids=["i-dup-1111", "sg-dup-2222"],
    )

    rec_b = DeploymentRecord(
        deployment_id="dep-beta-02",
        user_request="Second deployment attempting duplicate ID reuse",
        status=DeploymentStatus.COMPLETED,
        aws_resource_ids=["i-dup-1111", "sg-dup-2222"],
    )

    seen_inst = set()
    seen_sg = set()

    info_a = extract_deployment_resources(rec_a, seen_instances=seen_inst, seen_sgs=seen_sg)
    assert info_a["instance_id"] == "i-dup-1111"
    assert info_a["security_group_id"] == "sg-dup-2222"

    # Second deployment with duplicate IDs must NOT duplicate the resource
    info_b = extract_deployment_resources(rec_b, seen_instances=seen_inst, seen_sgs=seen_sg)
    assert info_b["instance_id"] == "Not available"
    assert info_b["security_group_id"] == "Not available"


def test_no_resource_merging_across_different_deployments():
    """Verify that resources from different deployments are strictly isolated and never merged."""
    rec_x = DeploymentRecord(
        deployment_id="dep-x-01",
        user_request="Deployment X",
        status=DeploymentStatus.COMPLETED,
        aws_resource_ids=["i-unique-x", "sg-unique-x"],
    )

    rec_y = DeploymentRecord(
        deployment_id="dep-y-02",
        user_request="Deployment Y",
        status=DeploymentStatus.COMPLETED,
        aws_resource_ids=["i-unique-y", "sg-unique-y"],
    )

    seen_inst = set()
    seen_sg = set()

    info_x = extract_deployment_resources(rec_x, seen_instances=seen_inst, seen_sgs=seen_sg)
    info_y = extract_deployment_resources(rec_y, seen_instances=seen_inst, seen_sgs=seen_sg)

    assert info_x["instance_id"] == "i-unique-x"
    assert info_x["security_group_id"] == "sg-unique-x"
    assert info_y["instance_id"] == "i-unique-y"
    assert info_y["security_group_id"] == "sg-unique-y"
    assert info_x["instance_id"] != info_y["instance_id"]
    assert info_x["security_group_id"] != info_y["security_group_id"]
