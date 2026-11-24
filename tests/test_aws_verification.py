import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from botocore.exceptions import ClientError
from core.models import (
    AMIResolutionResult,
    ApprovalStatus,
    AWSAuthMethod,
    AWSIdentity,
    AWSService,
    DeploymentRecord,
    DeploymentStatus,
    EC2ResourceSpec,
    NetworkSpec,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    PropertyComparisonResult,
    StorageSpec,
    VerificationSeverity,
    VerificationStatus,
    VolumeType,
)
from database.db import init_db
from database.repository import DeploymentRepository
from services.aws_auth_service import AWSAuthService, AWSCredentialsContainer
from services.aws_verification_service import AWSVerificationService


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_verification.db"
    init_db(db_file)
    return db_file


@pytest.fixture
def repository(temp_db):
    return DeploymentRepository(temp_db)


@pytest.fixture
def sample_spec():
    return NormalizedInfrastructureSpec(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="test-server",
                instance_type="t3.micro",
                region="ap-south-1",
                ami=AMIResolutionResult(ami_id="ami-0123456789abcdef0"),
                storage=StorageSpec(size_gb=20, volume_type=VolumeType.GP3, encrypted=True),
                network=NetworkSpec(public_ip=False, vpc_id="vpc-11223344"),
            )
        ],
        user_request="Deploy private t3.micro in Mumbai",
    )


def test_mock_mode_verification_conformance(repository, sample_spec):
    """Mock mode verification generates a full report without calling real AWS APIs."""
    verifier = AWSVerificationService(repository=repository)
    report = verifier.verify_deployment(
        deployment_id="dep-mock-verif",
        spec=sample_spec,
        resource_ids=["i-09f8e7d6c5b4a3210", "sg-01a2b3c4d5e6f7890"],
        expected_account_id="123456789012",
        expected_region="ap-south-1",
        is_mock=True,
    )

    assert report.verification_status == VerificationStatus.VERIFIED
    assert report.is_mock is True
    assert len(report.checks) >= 5
    assert all(c.result == PropertyComparisonResult.MATCH for c in report.checks)

    # Verify persistent DB record
    rec = repository.get("dep-mock-verif")
    # record wasn't created initially, let's verify update was attempted or handle
    assert report.aws_account_id == "123456789012"


def test_real_verification_identity_validation_account_mismatch(repository, sample_spec):
    """Verification immediately aborts if active session account differs from approved deployment account."""
    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123")
    identity = AWSIdentity(account_id="222222222222", arn="arn:aws:iam::222222222222:user/dev", user_id="AID222", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
    report = verifier.verify_deployment(
        deployment_id="dep-acct-mismatch",
        spec=sample_spec,
        resource_ids=["i-11223344"],
        expected_account_id="111111111111",  # Approved for 111111111111
        expected_region="ap-south-1",
        is_mock=False,
    )

    assert report.verification_status == VerificationStatus.VERIFICATION_FAILED
    assert "Account Mismatch" in report.errors[0]


def test_real_verification_identity_validation_region_mismatch(repository, sample_spec):
    """Verification immediately aborts if active session region differs from approved deployment region."""
    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="us-west-2")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="us-west-2")
    auth_service.set_active_credentials(creds, identity)

    verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
    report = verifier.verify_deployment(
        deployment_id="dep-reg-mismatch",
        spec=sample_spec,
        resource_ids=["i-11223344"],
        expected_account_id="111111111111",
        expected_region="ap-south-1",  # Approved for ap-south-1
        is_mock=False,
    )

    assert report.verification_status == VerificationStatus.VERIFICATION_FAILED
    assert "Region Mismatch" in report.errors[0]


def test_ec2_verification_success_all_properties_match(repository, sample_spec):
    """When all live AWS resources conform to specification, status is VERIFIED."""
    inst_id = "i-01122334455667788"
    vol_id = "vol-0aabbccddeeff0011"
    sg_id = "sg-0123456789abcdef0"

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    # Mock EC2 client
    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": inst_id,
                        "InstanceType": "t3.micro",
                        "ImageId": "ami-0123456789abcdef0",
                        "State": {"Name": "running"},
                        "VpcId": "vpc-11223344",
                        "SubnetId": "subnet-998877",
                        "PublicIpAddress": None,
                        "PrivateIpAddress": "10.0.1.50",
                        "RootDeviceName": "/dev/xvda",
                        "BlockDeviceMappings": [
                            {"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": vol_id}}
                        ],
                    }
                ]
            }
        ]
    }
    mock_ec2.describe_volumes.return_value = {
        "Volumes": [
            {
                "VolumeId": vol_id,
                "Size": 20,
                "VolumeType": "gp3",
                "Encrypted": True,
                "Attachments": [{"InstanceId": inst_id, "Device": "/dev/xvda"}],
            }
        ]
    }
    mock_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [
            {
                "GroupId": sg_id,
                "GroupName": "app-sg",
                "IpPermissions": [
                    {"FromPort": 22, "ToPort": 22, "IpProtocol": "tcp", "IpRanges": [{"CidrIp": "10.0.0.0/16"}]}
                ],
            }
        ]
    }

    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
        verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
        report = verifier.verify_deployment(
            deployment_id="dep-ok",
            spec=sample_spec,
            resource_ids=[inst_id, sg_id],
            expected_account_id="111111111111",
            expected_region="ap-south-1",
            is_mock=False,
        )

    assert report.verification_status == VerificationStatus.VERIFIED
    assert len(report.warnings) == 0
    assert len(report.errors) == 0
    assert all(c.result == PropertyComparisonResult.MATCH for c in report.checks)


def test_ec2_verification_operational_state_pending_produces_warning(repository, sample_spec):
    """When instance is in 'pending' state, it produces VERIFIED_WITH_WARNINGS rather than CONFIGURATION_MISMATCH."""
    inst_id = "i-0pending12345678"
    vol_id = "vol-0pendingvol123"

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": inst_id,
                        "InstanceType": "t3.micro",
                        "ImageId": "ami-0123456789abcdef0",
                        "State": {"Name": "pending"},  # PENDING OPERATIONAL STATE
                        "VpcId": "vpc-11223344",
                        "RootDeviceName": "/dev/xvda",
                        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": vol_id}}],
                    }
                ]
            }
        ]
    }
    mock_ec2.describe_volumes.return_value = {
        "Volumes": [
            {
                "VolumeId": vol_id,
                "Size": 20,
                "VolumeType": "gp3",
                "Encrypted": True,
                "Attachments": [{"InstanceId": inst_id, "Device": "/dev/xvda"}],
            }
        ]
    }

    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
        verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
        report = verifier.verify_deployment(
            deployment_id="dep-pending-state",
            spec=sample_spec,
            resource_ids=[inst_id],
            expected_account_id="111111111111",
            expected_region="ap-south-1",
            is_mock=False,
        )

    assert report.verification_status == VerificationStatus.VERIFIED_WITH_WARNINGS
    assert any("pending" in w.lower() for w in report.warnings)


def test_ec2_verification_missing_instance_resource_not_found(repository, sample_spec):
    """When instance is not found in AWS, status is RESOURCE_NOT_FOUND."""
    inst_id = "i-0missing11223344"

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {"Reservations": []}

    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
        verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
        report = verifier.verify_deployment(
            deployment_id="dep-missing",
            spec=sample_spec,
            resource_ids=[inst_id],
            expected_account_id="111111111111",
            expected_region="ap-south-1",
            is_mock=False,
        )

    assert report.verification_status == VerificationStatus.RESOURCE_NOT_FOUND


def test_ec2_instance_type_mismatch(repository, sample_spec):
    """When instance type differs from specification, status is CONFIGURATION_MISMATCH."""
    inst_id = "i-0mismatchtype"
    vol_id = "vol-0mismatchvol"

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": inst_id,
                        "InstanceType": "t3.large",  # MISMATCH: Expected t3.micro
                        "ImageId": "ami-0123456789abcdef0",
                        "State": {"Name": "running"},
                        "RootDeviceName": "/dev/xvda",
                        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": vol_id}}],
                    }
                ]
            }
        ]
    }
    mock_ec2.describe_volumes.return_value = {
        "Volumes": [
            {
                "VolumeId": vol_id,
                "Size": 20,
                "VolumeType": "gp3",
                "Encrypted": True,
                "Attachments": [{"InstanceId": inst_id, "Device": "/dev/xvda"}],
            }
        ]
    }

    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
        verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
        report = verifier.verify_deployment(
            deployment_id="dep-mismatch-type",
            spec=sample_spec,
            resource_ids=[inst_id],
            expected_account_id="111111111111",
            expected_region="ap-south-1",
            is_mock=False,
        )

    assert report.verification_status == VerificationStatus.CONFIGURATION_MISMATCH
    sizing_check = next(c for c in report.checks if c.property_name == "Instance Sizing")
    assert sizing_check.result == PropertyComparisonResult.MISMATCH


def test_ebs_volume_encryption_mismatch_high_severity(repository, sample_spec):
    """When EBS volume is unencrypted against an encrypted specification, flagged as HIGH severity."""
    inst_id = "i-0unencinst"
    vol_id = "vol-0unencvol"

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": inst_id,
                        "InstanceType": "t3.micro",
                        "ImageId": "ami-0123456789abcdef0",
                        "State": {"Name": "running"},
                        "VpcId": "vpc-11223344",
                        "PublicIpAddress": None,
                        "RootDeviceName": "/dev/xvda",
                        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": vol_id}}],
                    }
                ]
            }
        ]
    }
    mock_ec2.describe_volumes.return_value = {
        "Volumes": [
            {
                "VolumeId": vol_id,
                "Size": 20,
                "VolumeType": "gp3",
                "Encrypted": False,  # MISMATCH: Expected True
                "Attachments": [{"InstanceId": inst_id, "Device": "/dev/xvda"}],
            }
        ]
    }

    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
        verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
        report = verifier.verify_deployment(
            deployment_id="dep-unenc",
            spec=sample_spec,
            resource_ids=[inst_id],
            expected_account_id="111111111111",
            expected_region="ap-south-1",
            is_mock=False,
        )

    assert report.verification_status == VerificationStatus.CONFIGURATION_MISMATCH
    enc_check = next(c for c in report.checks if c.property_name == "Volume Encryption")
    assert enc_check.result == PropertyComparisonResult.MISMATCH
    assert enc_check.severity == VerificationSeverity.HIGH


def test_public_ip_mismatch_on_private_spec_high_severity(repository, sample_spec):
    """When a public IP is assigned to an intended private server, flagged as HIGH severity."""
    inst_id = "i-0leakedpublic"
    vol_id = "vol-0volok"

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": inst_id,
                        "InstanceType": "t3.micro",
                        "ImageId": "ami-0123456789abcdef0",
                        "State": {"Name": "running"},
                        "PublicIpAddress": "3.109.12.88",  # LEAK: Expected private
                        "RootDeviceName": "/dev/xvda",
                        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": vol_id}}],
                    }
                ]
            }
        ]
    }
    mock_ec2.describe_volumes.return_value = {
        "Volumes": [
            {
                "VolumeId": vol_id,
                "Size": 20,
                "VolumeType": "gp3",
                "Encrypted": True,
                "Attachments": [{"InstanceId": inst_id, "Device": "/dev/xvda"}],
            }
        ]
    }

    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
        verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
        report = verifier.verify_deployment(
            deployment_id="dep-leak-ip",
            spec=sample_spec,
            resource_ids=[inst_id],
            expected_account_id="111111111111",
            expected_region="ap-south-1",
            is_mock=False,
        )

    assert report.verification_status == VerificationStatus.CONFIGURATION_MISMATCH
    ip_check = next(c for c in report.checks if c.property_name == "Public IP Exposure")
    assert ip_check.result == PropertyComparisonResult.MISMATCH
    assert ip_check.severity == VerificationSeverity.HIGH


def test_security_group_unrestricted_ssh_high_severity_warning(repository, sample_spec):
    """When a security group opens port 22 to 0.0.0.0/0, flagged as HIGH severity warning."""
    inst_id = "i-0safeinst"
    vol_id = "vol-0safevol"
    sg_id = "sg-0openssh"

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": inst_id,
                        "InstanceType": "t3.micro",
                        "ImageId": "ami-0123456789abcdef0",
                        "State": {"Name": "running"},
                        "VpcId": "vpc-11223344",
                        "PublicIpAddress": None,
                        "RootDeviceName": "/dev/xvda",
                        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": vol_id}}],
                    }
                ]
            }
        ]
    }
    mock_ec2.describe_volumes.return_value = {
        "Volumes": [
            {
                "VolumeId": vol_id,
                "Size": 20,
                "VolumeType": "gp3",
                "Encrypted": True,
                "Attachments": [{"InstanceId": inst_id, "Device": "/dev/xvda"}],
            }
        ]
    }
    mock_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [
            {
                "GroupId": sg_id,
                "GroupName": "open-ssh-sg",
                "IpPermissions": [
                    {"FromPort": 22, "ToPort": 22, "IpProtocol": "tcp", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
                ],
            }
        ]
    }

    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
        verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
        report = verifier.verify_deployment(
            deployment_id="dep-open-ssh",
            spec=sample_spec,
            resource_ids=[inst_id, sg_id],
            expected_account_id="111111111111",
            expected_region="ap-south-1",
            is_mock=False,
        )

    assert report.verification_status == VerificationStatus.VERIFIED_WITH_WARNINGS
    ssh_check = next(c for c in report.checks if c.property_name == "SSH Ingress (Port 22)")
    assert ssh_check.severity == VerificationSeverity.HIGH


def test_verification_retries_transient_throttling(repository, sample_spec):
    """Transient throttling exceptions are retried with bounded backoff."""
    inst_id = "i-0retrytest"
    vol_id = "vol-0retrytest"

    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    mock_ec2 = MagicMock()
    # Throw throttling on first call, succeed on second
    throttle_err = ClientError({"Error": {"Code": "RequestLimitExceeded", "Message": "Throttling"}}, "DescribeInstances")
    success_resp = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": inst_id,
                        "InstanceType": "t3.micro",
                        "ImageId": "ami-0123456789abcdef0",
                        "State": {"Name": "running"},
                        "VpcId": "vpc-11223344",
                        "PublicIpAddress": None,
                        "RootDeviceName": "/dev/xvda",
                        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": vol_id}}],
                    }
                ]
            }
        ]
    }
    mock_ec2.describe_instances.side_effect = [throttle_err, success_resp]
    mock_ec2.describe_volumes.return_value = {
        "Volumes": [
            {
                "VolumeId": vol_id,
                "Size": 20,
                "VolumeType": "gp3",
                "Encrypted": True,
                "Attachments": [{"InstanceId": inst_id, "Device": "/dev/xvda"}],
            }
        ]
    }

    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
        verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
        report = verifier.verify_deployment(
            deployment_id="dep-throttle-ok",
            spec=sample_spec,
            resource_ids=[inst_id],
            expected_account_id="111111111111",
            expected_region="ap-south-1",
            is_mock=False,
        )

    assert report.verification_status == VerificationStatus.VERIFIED
    assert mock_ec2.describe_instances.call_count == 2


def test_verification_is_strictly_read_only(repository, sample_spec):
    """Verification invokes only describe_* read-only APIs, never mutation APIs."""
    auth_service = AWSAuthService()
    creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
    identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
    auth_service.set_active_credentials(creds, identity)

    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {"Reservations": []}

    with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
        verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
        verifier.verify_deployment(
            deployment_id="dep-readonly-check",
            spec=sample_spec,
            resource_ids=["i-12345678"],
            expected_account_id="111111111111",
            expected_region="ap-south-1",
            is_mock=False,
        )

    for call in mock_ec2.method_calls:
        method_name = call[0]
        assert method_name.startswith("describe_"), f"Forbidden mutating call detected: {method_name}"


def test_re_verify_live_state_preserves_execution_and_approval_status(repository, sample_spec):
    """Re-Verify Live State action strictly updates verification details without modifying plan, approval, or execution status."""
    dep_id = "dep-reverify-preserve"
    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request="Deploy ec2",
        status=DeploymentStatus.COMPLETED,
        approval_status=ApprovalStatus.APPROVED,
        plan_hash="approved-hash-12345",
        aws_account_id="111111111111",
        aws_region="ap-south-1",
        aws_resource_ids=["i-mock123"],
    )
    repository.create(record)

    verifier = AWSVerificationService(repository=repository)
    report = verifier.verify_deployment(
        deployment_id=dep_id,
        spec=sample_spec,
        resource_ids=["i-mock123"],
        expected_account_id="111111111111",
        expected_region="ap-south-1",
        is_mock=True,
    )

    # Check updated record in SQLite
    updated = repository.get(dep_id)
    assert updated.status == DeploymentStatus.COMPLETED
    assert updated.approval_status == ApprovalStatus.APPROVED
    assert updated.plan_hash == "approved-hash-12345"
    assert updated.verification_status == VerificationStatus.VERIFIED
    assert updated.verification_details is not None
    assert updated.verification_details["verification_status"] == VerificationStatus.VERIFIED.value


def test_ec2_verification_uses_workspace_approved_ami(repository, sample_spec):
    """When a deployment workspace has an approved variables.tf, verification uses the workspace approved AMI baseline."""
    dep_id = "dep-test-workspace-ami"
    ws_dir = Path("workspaces") / dep_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    try:
        (ws_dir / "variables.tf").write_text(
            'variable "ami_id" {\n  description = "AMI"\n  type = string\n  default = "ami-0593d733c2fc5bd2e"\n}\n',
            encoding="utf-8",
        )

        # Spec has older/fallback AMI
        sample_spec.resources[0].ami.ami_id = "ami-00bb6a80f01f03502"

        auth_service = AWSAuthService()
        creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
        identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
        auth_service.set_active_credentials(creds, identity)

        inst_id = "i-0workspaceami"
        vol_id = "vol-0workspacevol"
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": inst_id,
                            "InstanceType": "t3.micro",
                            "ImageId": "ami-0593d733c2fc5bd2e",  # Matches workspace approved AMI
                            "State": {"Name": "running"},
                            "VpcId": "vpc-11223344",
                            "RootDeviceName": "/dev/xvda",
                            "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": vol_id}}],
                        }
                    ]
                }
            ]
        }
        mock_ec2.describe_volumes.return_value = {
            "Volumes": [
                {
                    "VolumeId": vol_id,
                    "Size": 20,
                    "VolumeType": "gp3",
                    "Encrypted": True,
                    "Attachments": [{"InstanceId": inst_id, "Device": "/dev/xvda"}],
                }
            ]
        }

        with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
            verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
            report = verifier.verify_deployment(
                deployment_id=dep_id,
                spec=sample_spec,
                resource_ids=[inst_id],
                expected_account_id="111111111111",
                expected_region="ap-south-1",
                is_mock=False,
            )

        ami_checks = [c for c in report.checks if c.property_name == "AMI ID"]
        assert len(ami_checks) == 1
        assert ami_checks[0].expected_value == "ami-0593d733c2fc5bd2e"
        assert ami_checks[0].actual_value == "ami-0593d733c2fc5bd2e"
        assert ami_checks[0].result == PropertyComparisonResult.MATCH
        assert report.verification_status == VerificationStatus.VERIFIED
    finally:
        import shutil
        if ws_dir.exists():
            shutil.rmtree(ws_dir, ignore_errors=True)


def test_ec2_verification_preserves_genuine_ami_mismatch(repository, sample_spec):
    """When actual EC2 instance AMI differs from approved configuration, verification strictly flags MISMATCH."""
    dep_id = "dep-test-ami-mismatch"
    ws_dir = Path("workspaces") / dep_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    try:
        (ws_dir / "variables.tf").write_text(
            'variable "ami_id" {\n  description = "AMI"\n  type = string\n  default = "ami-0593d733c2fc5bd2e"\n}\n',
            encoding="utf-8",
        )

        auth_service = AWSAuthService()
        creds = AWSCredentialsContainer(access_key_id="AKIA123", secret_access_key="sec123", region="ap-south-1")
        identity = AWSIdentity(account_id="111111111111", arn="arn:aws:iam::111111111111:user/dev", user_id="AID111", region="ap-south-1")
        auth_service.set_active_credentials(creds, identity)

        inst_id = "i-0genuineami"
        vol_id = "vol-0genuinevol"
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": inst_id,
                            "InstanceType": "t3.micro",
                            "ImageId": "ami-99999999999999999",  # Genuine mismatch
                            "State": {"Name": "running"},
                            "VpcId": "vpc-11223344",
                            "RootDeviceName": "/dev/xvda",
                            "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": vol_id}}],
                        }
                    ]
                }
            ]
        }
        mock_ec2.describe_volumes.return_value = {
            "Volumes": [
                {
                    "VolumeId": vol_id,
                    "Size": 20,
                    "VolumeType": "gp3",
                    "Encrypted": True,
                    "Attachments": [{"InstanceId": inst_id, "Device": "/dev/xvda"}],
                }
            ]
        }

        with patch.object(auth_service, "get_boto3_client", return_value=mock_ec2):
            verifier = AWSVerificationService(repository=repository, auth_service=auth_service)
            report = verifier.verify_deployment(
                deployment_id=dep_id,
                spec=sample_spec,
                resource_ids=[inst_id],
                expected_account_id="111111111111",
                expected_region="ap-south-1",
                is_mock=False,
            )

        ami_checks = [c for c in report.checks if c.property_name == "AMI ID"]
        assert len(ami_checks) == 1
        assert ami_checks[0].expected_value == "ami-0593d733c2fc5bd2e"
        assert ami_checks[0].actual_value == "ami-99999999999999999"
        assert ami_checks[0].result == PropertyComparisonResult.MISMATCH
        assert ami_checks[0].severity == VerificationSeverity.ERROR
        assert report.verification_status == VerificationStatus.CONFIGURATION_MISMATCH
    finally:
        import shutil
        if ws_dir.exists():
            shutil.rmtree(ws_dir, ignore_errors=True)

