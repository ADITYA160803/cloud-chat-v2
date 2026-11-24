from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from core.models import (
    AMIResolutionResult,
    AWSService,
    DependencyStatus,
    EC2ResourceSpec,
    NetworkSpec,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    StorageSpec,
    VolumeType,
)
from services.aws_auth_service import AWSCredentialsContainer, aws_auth_service
from services.dependency_resolver import DependencyResolver
from services.security_scanner import SecurityScanner
from services.terraform_service import TerraformService


def test_terraform_generator_private_instance_zero_ingress_rules():
    """Verify private instance defaults to exactly zero inbound ingress rules in Terraform."""
    service = TerraformService()
    spec = NormalizedInfrastructureSpec(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="private-srv",
                instance_type="t3.micro",
                region="ap-south-1",
                network=NetworkSpec(
                    public_ip=False,
                    allowed_ssh_cidrs=[],
                    vpc_id="vpc-12345678",
                    subnet_id="subnet-12345678",
                ),
            )
        ],
        user_request="Deploy private EC2",
    )
    files = service.generate_configuration(spec)
    main_tf = files["main.tf"]

    # Security group must not have an ingress block
    assert "ingress {" not in main_tf
    # Must have egress block with description
    assert "egress {" in main_tf
    assert "Allow all outbound traffic" in main_tf


def test_terraform_generator_ssh_ingress_restricted_to_trusted_cidr():
    """Verify SSH ingress is restricted to specified trusted CIDRs and never 0.0.0.0/0."""
    service = TerraformService()
    spec = NormalizedInfrastructureSpec(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="bastion-srv",
                instance_type="t3.micro",
                region="ap-south-1",
                network=NetworkSpec(
                    public_ip=False,
                    allowed_ssh_cidrs=["203.0.113.50/32"],
                    vpc_id="vpc-12345678",
                    subnet_id="subnet-12345678",
                ),
            )
        ],
        user_request="Deploy EC2 with admin SSH",
    )
    files = service.generate_configuration(spec)
    main_tf = files["main.tf"]

    assert "ingress {" in main_tf
    assert "from_port   = 22" in main_tf
    assert 'cidr_blocks = ["203.0.113.50/32"]' in main_tf
    assert "0.0.0.0/0" not in main_tf.split("ingress {")[1].split("}")[0]


def test_terraform_generator_imds_and_ebs_optimized_enforcement():
    """Verify generated Terraform enforces IMDSv2, EBS optimization, encryption and monitoring."""
    service = TerraformService()
    spec = NormalizedInfrastructureSpec(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="hardened-srv",
                instance_type="t3.micro",
                region="ap-south-1",
            )
        ],
    )
    files = service.generate_configuration(spec)
    main_tf = files["main.tf"]

    assert "http_tokens" in main_tf and '"required"' in main_tf
    assert "http_endpoint" in main_tf and '"enabled"' in main_tf
    assert "ebs_optimized" in main_tf and "true" in main_tf
    assert "monitoring" in main_tf and "true" in main_tf
    assert "encrypted" in main_tf and "true" in main_tf


def test_security_scanner_zero_high_findings_on_generated_tf(tmp_path):
    """Verify policy scanner produces zero HIGH findings on standard generated Terraform."""
    service = TerraformService()
    spec = NormalizedInfrastructureSpec(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="clean-srv",
                instance_type="t3.micro",
                region="ap-south-1",
                network=NetworkSpec(
                    public_ip=False,
                    allowed_ssh_cidrs=["203.0.113.10/32"],
                    vpc_id="vpc-12345678",
                    subnet_id="subnet-12345678",
                ),
            )
        ],
    )
    files = service.generate_configuration(spec)

    scanner = SecurityScanner(checkov_binary=None)
    result = scanner.scan_workspace(tmp_path, tf_files=files)

    high_findings = [f for f in result.findings if f.severity == "HIGH"]
    assert len(high_findings) == 0
    assert result.failed_checks == 0


def test_dependency_resolver_subnet_mismatch_fails_validation():
    """Verify subnet belonging to a different VPC fails validation."""
    mock_client = MagicMock()
    mock_client.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-11111111"}]}
    mock_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-22222222",
                "VpcId": "vpc-99999999",  # Belongs to a different VPC
                "AvailabilityZone": "ap-south-1a",
                "CidrBlock": "10.0.1.0/24",
            }
        ]
    }

    resolver = DependencyResolver(ec2_client=mock_client)
    resource = EC2ResourceSpec(
        name="test",
        instance_type="t3.micro",
        network=NetworkSpec(
            use_existing_vpc=True,
            vpc_id="vpc-11111111",
            use_existing_subnet=True,
            subnet_id="subnet-22222222",
        ),
    )
    ami_result = AMIResolutionResult(ami_id="ami-12345678", status="resolved")
    deps = resolver.resolve_dependencies("ap-south-1", resource, ami_result)

    subnet_dep = next(d for d in deps if d.name == "Subnet")
    assert subnet_dep.status == DependencyStatus.UNRESOLVED
    assert "belongs to VPC 'vpc-99999999', not selected VPC 'vpc-11111111'" in subnet_dep.message


def test_terraform_subprocess_env_injection_and_secret_redaction(monkeypatch):
    """Verify TerraformService._run_command injects credentials and redacts sensitive data."""
    from core.models import AWSAuthMethod
    creds = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.ACCESS_KEY,
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="ap-south-1",
    )
    monkeypatch.setattr(aws_auth_service, "_active_credentials", creds)

    service = TerraformService()

    # Verify secret redaction
    leak_str = "Error with secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY in output"
    redacted = aws_auth_service.redact_secrets(leak_str)
    assert "wJalrXUtnFEMI" not in redacted
    assert "[REDACTED_SECRET_KEY]" in redacted

    # Verify subprocess env includes AWS vars
    env = creds.get_subprocess_env()
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"
    assert env["AWS_SECRET_ACCESS_KEY"] == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert env["AWS_DEFAULT_REGION"] == "ap-south-1"
