from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from core.models import SecurityScanResult
from services.security_scanner import SecurityScanner


def test_security_scanner_flags_public_ip(tmp_path):
    """Verify security scanner flags direct public IP assignment on EC2."""
    workspace = tmp_path / "ws_public_ip"
    workspace.mkdir()
    tf_content = """
    resource "aws_instance" "app_server" {
      ami                         = "ami-12345678"
      instance_type               = "t3.micro"
      associate_public_ip_address = true
      root_block_device {
        encrypted = true
      }
    }
    """
    scanner = SecurityScanner(checkov_binary=None)  # Use policy engine
    res = scanner.scan_workspace(workspace, tf_files={"main.tf": tf_content})

    assert res.failed_checks >= 1
    finding_rules = [f.rule_id for f in res.findings]
    assert "CKV_AWS_PUBLIC_IP" in finding_rules


def test_security_scanner_flags_unencrypted_ebs(tmp_path):
    """Verify security scanner flags unencrypted EBS volumes."""
    workspace = tmp_path / "ws_unencrypted"
    workspace.mkdir()
    tf_content = """
    resource "aws_instance" "app_server" {
      ami                         = "ami-12345678"
      instance_type               = "t3.micro"
      associate_public_ip_address = false
      root_block_device {
        encrypted = false
      }
    }
    """
    scanner = SecurityScanner(checkov_binary=None)
    res = scanner.scan_workspace(workspace, tf_files={"main.tf": tf_content})

    assert res.failed_checks >= 1
    finding_rules = [f.rule_id for f in res.findings]
    assert "CKV_AWS_EBS_ENCRYPTION" in finding_rules


def test_security_scanner_flags_open_ssh(tmp_path):
    """Verify security scanner flags unrestricted ingress on port 22."""
    workspace = tmp_path / "ws_ssh"
    workspace.mkdir()
    tf_content = """
    resource "aws_security_group" "app_sg" {
      ingress {
        from_port   = 22
        to_port     = 22
        protocol    = "tcp"
        cidr_blocks = ["0.0.0.0/0"]
      }
    }
    """
    scanner = SecurityScanner(checkov_binary=None)
    res = scanner.scan_workspace(workspace, tf_files={"main.tf": tf_content})

    assert any(f.rule_id == "CKV_AWS_SG_SSH_OPEN" for f in res.findings)


def test_security_scanner_passes_secure_configuration(tmp_path):
    """Verify secure configuration with encryption, IMDSv2, EBS optimization, descriptions and private networking passes."""
    workspace = tmp_path / "ws_secure"
    workspace.mkdir()
    tf_content = """
    resource "aws_instance" "app_server" {
      ami                         = "ami-12345678"
      instance_type               = "t3.micro"
      associate_public_ip_address = false
      ebs_optimized               = true
      root_block_device {
        encrypted = true
        volume_type = "gp3"
      }
      metadata_options {
        http_tokens   = "required"
        http_endpoint = "enabled"
      }
    }
    resource "aws_security_group" "app_sg" {
      egress {
        description = "Allow all outbound traffic"
        from_port   = 0
        to_port     = 0
        protocol    = "-1"
        cidr_blocks = ["0.0.0.0/0"]
      }
    }
    """
    scanner = SecurityScanner(checkov_binary=None)
    res = scanner.scan_workspace(workspace, tf_files={"main.tf": tf_content})

    assert res.failed_checks == 0
    assert res.passed_checks >= 3


def test_security_scanner_accepted_risk_uses_deployment_specific_subnet(tmp_path):
    """Verify Accepted Architecture Exception rationale references the exact deployment-specific subnet."""
    workspace = tmp_path / "ws_dynamic_subnet"
    workspace.mkdir()
    custom_subnet = "subnet-067ec34fd443e6195"
    tf_content = f"""
    # public deployment
    resource "aws_instance" "app_server" {{
      ami                         = "ami-12345678"
      instance_type               = "t3.micro"
      associate_public_ip_address = true
      subnet_id                   = "{custom_subnet}"
      root_block_device {{
        encrypted = true
      }}
    }}
    """
    scanner = SecurityScanner(checkov_binary=None)
    res = scanner.scan_workspace(workspace, tf_files={"main.tf": tf_content})

    assert len(res.accepted_findings) == 1
    finding = res.accepted_findings[0]
    assert finding.rule_id == "CKV_AWS_PUBLIC_IP"
    assert finding.disposition == "ACCEPTED_RISK"
    assert finding.rationale is not None
    # Must reference the exact deployment subnet and NOT a hardcoded fallback subnet
    assert custom_subnet in finding.rationale
    assert "subnet-03b1966c12692d842" not in finding.rationale


def test_security_scanner_extract_subnet_id_handles_missing_and_custom_subnets():
    """Verify _extract_subnet_id correctly extracts or handles absent subnets."""
    assert SecurityScanner._extract_subnet_id('subnet_id = "subnet-067ec34fd443e6195"') == "subnet-067ec34fd443e6195"
    assert SecurityScanner._extract_subnet_id('subnet_id = "subnet-11223344556677889"') == "subnet-11223344556677889"
    assert SecurityScanner._extract_subnet_id('resource "aws_instance" "app" {}') is None

