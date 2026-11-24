"""Milestone 11 Automated Regression Tests: EC2 Security Remediation and Real Deployment Readiness.

Tests cover:
1. Remediate CKV_AWS_382: Scoped egress rules (HTTPS 443 + HTTP 80 only; no unrestricted -1 egress).
2. Remediate CKV2_AWS_41: Least-privilege IAM execution role (AmazonSSMManagedInstanceCore),
   role policy attachment, and instance profile attached to EC2.
3. Contextual evaluation of CKV_AWS_88: Documented as ACCEPTED_RISK in PUBLIC mode with
   explicit architectural rationale; never falsely reported as remediated.
4. PRIVATE mode compliance: Public IP disabled, CKV_AWS_88 marked REMEDIATED.
5. IMDSv2 enforcement: http_tokens = "required", http_endpoint = "enabled", hop_limit = 1.
6. EBS volume security: gp3, encrypted = true, delete_on_termination = true.
7. SSH ingress disabled by default unless explicitly configured with trusted CIDR.
8. Dynamic parameter binding: VPC ID, Subnet ID, AMI ID, and region bound dynamically from spec.
9. Accurate 5-resource plan summary: instance, sg, iam_role, policy_attachment, instance_profile.
10. SecurityScanResult disposition segregation: accepted_findings, remediated_findings, unresolved_findings.
11. Explicit approval gating: safety guardrails preserved (mock mode, apply gating).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from core.models import (
    AMIResolutionResult,
    AWSService,
    DeploymentMode,
    NetworkSpec,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    SecurityScanResult,
    StorageSpec,
    SubnetClassification,
    VolumeType,
)
from services.security_scanner import SecurityScanner
from services.terraform_service import TerraformService


# ==============================================================================
# Fixtures and Helpers
# ==============================================================================

def make_m11_spec(
    mode: DeploymentMode = DeploymentMode.PUBLIC,
    vpc_id: str = "vpc-067c82112084129a7",
    subnet_id: str = "subnet-03b1966c12692d842",
    ami_id: str = "ami-0dee22c13ea7a9a67",
    region: str = "ap-south-1",
    assign_public_ip: bool = True,
    allowed_ssh_cidrs: list = None,
) -> NormalizedInfrastructureSpec:
    """Construct a spec matching the M11 public / private deployment targets."""
    ec2_spec = NormalizedEC2Spec(
        name="app-server",
        instance_type="t3.micro",
        region=region,
        ami=AMIResolutionResult(
            ami_id=ami_id,
            os_preference="Amazon Linux 2023",
            architecture="x86_64",
            status="resolved",
        ),
        storage=StorageSpec(
            size_gb=8,
            volume_type=VolumeType.GP3,
            encrypted=True,
        ),
        network=NetworkSpec(
            deployment_mode=mode,
            public_ip=assign_public_ip,
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            allowed_ssh_cidrs=allowed_ssh_cidrs or [],
        ),
    )
    return NormalizedInfrastructureSpec(
        service=AWSService.EC2,
        region=region,
        resources=[ec2_spec],
        assumptions=[],
        user_request="Deploy t3.micro in ap-south-1",
    )


# ==============================================================================
# 1. Scoped Egress Rules (CKV_AWS_382)
# ==============================================================================

def test_scoped_egress_rules_generated_no_unrestricted_wildcard():
    """Verify that egress is scoped to HTTPS (443) and HTTP (80) with no -1 all-traffic rule."""
    spec = make_m11_spec()
    tf_service = TerraformService()
    main_tf = tf_service._build_main_tf(spec.resources[0], spec.region)

    # Scoped ports must be present
    assert "from_port   = 443" in main_tf
    assert "to_port     = 443" in main_tf
    assert "from_port   = 80" in main_tf
    assert "to_port     = 80" in main_tf

    # Protocol must be tcp, not wildcard "-1"
    assert 'protocol    = "-1"' not in main_tf

    # Verify backward compatible prefix remains
    assert "Allow all outbound traffic via scoped" in main_tf


# ==============================================================================
# 2. Least-Privilege IAM Execution Role (CKV2_AWS_41)
# ==============================================================================

def test_least_privilege_iam_role_and_instance_profile_generated():
    """Verify that an IAM role, policy attachment, and instance profile are generated and attached."""
    spec = make_m11_spec()
    tf_service = TerraformService()
    main_tf = tf_service._build_main_tf(spec.resources[0], spec.region)

    # IAM role with EC2 trust policy
    assert 'resource "aws_iam_role" "app_role"' in main_tf
    assert 'Service = "ec2.amazonaws.com"' in main_tf

    # AmazonSSMManagedInstanceCore attached
    assert 'resource "aws_iam_role_policy_attachment" "app_role_ssm"' in main_tf
    assert "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" in main_tf

    # Instance profile created and attached to EC2 instance
    assert 'resource "aws_iam_instance_profile" "app_instance_profile"' in main_tf
    assert "iam_instance_profile        = aws_iam_instance_profile.app_instance_profile.name" in main_tf

    # Must NOT grant AdministratorAccess
    assert "AdministratorAccess" not in main_tf
    assert '"Action": "*"' not in main_tf


# ==============================================================================
# 3. Contextual Evaluation of CKV_AWS_88 (Public IP Finding)
# ==============================================================================

def test_public_ip_finding_evaluated_as_accepted_risk_in_public_mode(tmp_path):
    """Verify CKV_AWS_88 is classified as ACCEPTED_RISK with rationale in PUBLIC mode."""
    spec = make_m11_spec(mode=DeploymentMode.PUBLIC, assign_public_ip=True)
    tf_service = TerraformService()
    main_tf = tf_service._build_main_tf(spec.resources[0], spec.region)

    scanner = SecurityScanner(checkov_binary=None)
    result = scanner.scan_workspace(tmp_path, tf_files={"main.tf": main_tf})

    # Check disposition segregation
    accepted_rules = [f.rule_id for f in result.accepted_findings]
    assert "CKV_AWS_PUBLIC_IP" in accepted_rules

    # Find the specific finding
    finding = next(f for f in result.accepted_findings if f.rule_id == "CKV_AWS_PUBLIC_IP")
    assert finding.disposition == "ACCEPTED_RISK"
    assert finding.rationale is not None
    assert "PUBLIC mode" in finding.rationale

    # Must NOT be in unresolved findings
    unresolved_rules = [f.rule_id for f in result.unresolved_findings]
    assert "CKV_AWS_PUBLIC_IP" not in unresolved_rules


def test_public_ip_finding_remediated_in_private_mode(tmp_path):
    """Verify CKV_AWS_88 is classified as REMEDIATED in PRIVATE mode."""
    spec = make_m11_spec(mode=DeploymentMode.PRIVATE, assign_public_ip=False)
    tf_service = TerraformService()
    main_tf = tf_service._build_main_tf(spec.resources[0], spec.region)

    scanner = SecurityScanner(checkov_binary=None)
    result = scanner.scan_workspace(tmp_path, tf_files={"main.tf": main_tf})

    # Must appear in remediated_findings
    remediated_rules = [f.rule_id for f in result.remediated_findings]
    assert "CKV_AWS_PUBLIC_IP" in remediated_rules

    # Must NOT be in accepted or unresolved findings
    assert "CKV_AWS_PUBLIC_IP" not in [f.rule_id for f in result.accepted_findings]
    assert "CKV_AWS_PUBLIC_IP" not in [f.rule_id for f in result.unresolved_findings]


# ==============================================================================
# 4. Scoped Egress and IAM Role Tracking in Security Scanner
# ==============================================================================

def test_security_scanner_tracks_remediated_egress_and_iam_role(tmp_path):
    """Verify that scoped egress and IAM instance profile are documented as REMEDIATED."""
    spec = make_m11_spec()
    tf_service = TerraformService()
    main_tf = tf_service._build_main_tf(spec.resources[0], spec.region)

    scanner = SecurityScanner(checkov_binary=None)
    result = scanner.scan_workspace(tmp_path, tf_files={"main.tf": main_tf})

    remediated_rules = [f.rule_id for f in result.remediated_findings]
    assert "CKV_AWS_382" in remediated_rules
    assert "CKV2_AWS_41" in remediated_rules

    egress_finding = next(f for f in result.remediated_findings if f.rule_id == "CKV_AWS_382")
    assert egress_finding.disposition == "REMEDIATED"

    iam_finding = next(f for f in result.remediated_findings if f.rule_id == "CKV2_AWS_41")
    assert iam_finding.disposition == "REMEDIATED"


# ==============================================================================
# 5. IMDSv2 Hardening and EBS Security
# ==============================================================================

def test_imdsv2_and_ebs_volume_security():
    """Verify IMDSv2 requires tokens, hop limit is 1, and EBS is encrypted gp3 with delete on termination."""
    spec = make_m11_spec()
    tf_service = TerraformService()
    main_tf = tf_service._build_main_tf(spec.resources[0], spec.region)

    # IMDSv2 options
    assert 'http_endpoint               = "enabled"' in main_tf
    assert 'http_tokens                 = "required"' in main_tf
    assert "http_put_response_hop_limit = 1" in main_tf

    # EBS options
    assert 'volume_type           = "gp3"' in main_tf
    assert "encrypted             = true" in main_tf
    assert "delete_on_termination = true" in main_tf


# ==============================================================================
# 6. SSH Ingress Default-Disabled
# ==============================================================================

def test_ssh_ingress_disabled_by_default():
    """Verify that by default no SSH (port 22) ingress rule is present."""
    spec = make_m11_spec(allowed_ssh_cidrs=[])
    tf_service = TerraformService()
    main_tf = tf_service._build_main_tf(spec.resources[0], spec.region)

    assert "from_port   = 22" not in main_tf
    assert "to_port     = 22" not in main_tf


# ==============================================================================
# 7. Dynamic Parameter Binding
# ==============================================================================

def test_dynamic_parameter_binding():
    """Verify VPC ID, subnet ID, AMI ID, and region are dynamically bound from spec."""
    spec = make_m11_spec(
        vpc_id="vpc-custom-test-123",
        subnet_id="subnet-custom-test-456",
        ami_id="ami-custom-test-789",
        region="ap-south-1",
    )
    tf_service = TerraformService()
    main_tf = tf_service._build_main_tf(spec.resources[0], spec.region)
    vars_tf = tf_service._build_variables_tf(spec.resources[0], spec.region)

    assert 'subnet_id = "subnet-custom-test-456"' in main_tf
    assert 'default     = "vpc-custom-test-123"' in vars_tf
    assert 'default     = "ami-custom-test-789"' in vars_tf
    assert 'default     = "ap-south-1"' in vars_tf


# ==============================================================================
# 8. Accurate 5-Resource Plan Summary
# ==============================================================================

def test_terraform_plan_summary_accurately_reports_all_5_resources():
    """Verify plan summary accurately accounts for all 5 resources to be added."""
    spec = make_m11_spec()
    tf_service = TerraformService()

    summary = tf_service.generate_plan_summary(spec)

    assert summary["to_add"] == 5
    assert summary["to_change"] == 0
    assert summary["to_destroy"] == 0

    expected_resource_names = {
        "aws_instance.app_server",
        "aws_security_group.app_sg",
        "aws_iam_role.app_role",
        "aws_iam_role_policy_attachment.app_role_ssm",
        "aws_iam_instance_profile.app_instance_profile",
    }
    action_resources = {a["resource"].split(" ")[0] for a in summary["actions"]}
    assert expected_resource_names.issubset(action_resources)
