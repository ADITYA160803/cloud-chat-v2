from datetime import datetime, timezone
import os
import re
import time
from typing import Any, Dict, List, Optional
import boto3
from botocore.exceptions import ClientError, EndpointConnectionError
from config import config
from core.exceptions import ConfigurationError
from core.models import (
    DeploymentVerificationReport,
    NormalizedInfrastructureSpec,
    PropertyComparisonResult,
    PropertyVerificationCheck,
    ResourceVerificationDetails,
    VerificationSeverity,
    VerificationStatus,
)
from database.repository import DeploymentRepository
from services.aws_auth_service import AWSAuthService, aws_auth_service


class AWSVerificationService:
    """Performs independent read-only AWS API resource inspection and specification conformance verification."""

    def __init__(
        self,
        repository: Optional[DeploymentRepository] = None,
        auth_service: Optional[AWSAuthService] = None,
    ):
        self.repo = repository or DeploymentRepository()
        self.auth_service = auth_service or aws_auth_service

    def verify_deployment(
        self,
        deployment_id: str,
        spec: NormalizedInfrastructureSpec,
        resource_ids: List[str],
        expected_account_id: Optional[str] = None,
        expected_region: Optional[str] = None,
        is_mock: bool = False,
    ) -> DeploymentVerificationReport:
        """Independently verifies live AWS resources against the approved specification.

        CRITICAL SAFETY RULES:
        1. Read-only Boto3 calls only (describe_*). Zero mutating operations.
        2. Strictly validates Account ID and Region to prevent cross-account inspection.
        3. Separates resource existence, operational state, and configuration conformance.
        4. In mock mode, reports simulated conformance without making real AWS API calls.
        """
        target_account = expected_account_id or "123456789012"
        target_region = expected_region or spec.region

        # 1. Mock Mode Simulation Conformance
        if is_mock:
            return self._verify_mock_conformance(
                deployment_id=deployment_id,
                spec=spec,
                resource_ids=resource_ids,
                account_id=target_account,
                region=target_region,
            )

        # 2. Real AWS Verification: Pre-Inspection Identity Validation
        os.environ.setdefault("AWS_SDK_LOAD_CONFIG", "1")
        identity = self.auth_service.get_active_identity()
        if not identity or not identity.is_valid:
            # Attempt to authenticate using named profile if session in memory is uninitialized
            for prof in [os.environ.get("AWS_PROFILE"), "cloud-chat-deploy", "default"]:
                if prof:
                    try:
                        identity = self.auth_service.authenticate_profile(prof, region=target_region)
                        if identity and identity.is_valid:
                            break
                    except Exception:
                        continue

        if not identity or not identity.is_valid:
            report = DeploymentVerificationReport(
                deployment_id=deployment_id,
                aws_account_id=target_account,
                aws_region=target_region,
                verification_status=VerificationStatus.VERIFICATION_FAILED,
                errors=["No active authenticated AWS session found for verification. Please authenticate in Settings."],
                summary="AWS Verification failed: unauthenticated session.",
            )
            self.repo.record_verification_report(deployment_id, report.verification_status, report.model_dump(mode="json"))
            return report

        # Validate Account & Region Match
        if expected_account_id and identity.account_id != expected_account_id:
            report = DeploymentVerificationReport(
                deployment_id=deployment_id,
                aws_account_id=identity.account_id,
                aws_region=identity.region,
                verification_status=VerificationStatus.VERIFICATION_FAILED,
                errors=[
                    f"Account Mismatch: Approved for AWS Account '{expected_account_id}', "
                    f"but active session is Account '{identity.account_id}'."
                ],
                summary="AWS Verification aborted: target account mismatch.",
            )
            self.repo.record_verification_report(deployment_id, report.verification_status, report.model_dump(mode="json"))
            return report

        if expected_region and identity.region != expected_region:
            report = DeploymentVerificationReport(
                deployment_id=deployment_id,
                aws_account_id=identity.account_id,
                aws_region=identity.region,
                verification_status=VerificationStatus.VERIFICATION_FAILED,
                errors=[
                    f"Region Mismatch: Approved for Region '{expected_region}', "
                    f"but active session is Region '{identity.region}'."
                ],
                summary="AWS Verification aborted: target region mismatch.",
            )
            self.repo.record_verification_report(deployment_id, report.verification_status, report.model_dump(mode="json"))
            return report

        # 3. Perform Live Read-Only AWS Inspection
        return self._inspect_live_aws_resources(
            deployment_id=deployment_id,
            spec=spec,
            resource_ids=resource_ids,
            account_id=identity.account_id,
            region=identity.region,
        )

    def _inspect_live_aws_resources(
        self,
        deployment_id: str,
        spec: NormalizedInfrastructureSpec,
        resource_ids: List[str],
        account_id: str,
        region: str,
    ) -> DeploymentVerificationReport:
        """Queries live AWS APIs via Boto3 to inspect EC2, EBS, and Security Groups."""
        checks: List[PropertyVerificationCheck] = []
        resources_details: List[ResourceVerificationDetails] = []
        warnings: List[str] = []
        errors: List[str] = []

        try:
            ec2_client = self.auth_service.get_boto3_client("ec2", region_name=region)
        except Exception as e:
            report = DeploymentVerificationReport(
                deployment_id=deployment_id,
                aws_account_id=account_id,
                aws_region=region,
                verification_status=VerificationStatus.VERIFICATION_FAILED,
                errors=[f"Failed to initialize AWS EC2 client: {str(e)}"],
                summary="Verification failed: Boto3 client initialization error.",
            )
            self.repo.record_verification_report(deployment_id, report.verification_status, report.model_dump(mode="json"))
            return report

        ec2_spec = spec.resources[0] if spec.resources else None
        instance_ids = [r for r in resource_ids if r.startswith("i-")]
        sg_ids = [r for r in resource_ids if r.startswith("sg-")]

        # --- A. EC2 Instance Inspection ---
        if instance_ids:
            for inst_id in instance_ids:
                inst_report = self._inspect_ec2_instance(
                    ec2_client=ec2_client,
                    instance_id=inst_id,
                    spec=ec2_spec,
                    checks=checks,
                    warnings=warnings,
                    errors=errors,
                    deployment_id=deployment_id,
                )
                resources_details.append(inst_report)
        elif ec2_spec:
            # Expected instance but none found in resource IDs
            checks.append(
                PropertyVerificationCheck(
                    category="EC2",
                    property_name="Instance Existence",
                    expected_value=f"Instance for {ec2_spec.name}",
                    actual_value="None",
                    result=PropertyComparisonResult.NOT_FOUND,
                    explanation="No provisioned EC2 instance ID was identified in the deployment state.",
                    severity=VerificationSeverity.ERROR,
                )
            )

        # --- B. Security Group Ingress Inspection ---
        if sg_ids:
            for sg_id in sg_ids:
                self._inspect_security_group(
                    ec2_client=ec2_client,
                    sg_id=sg_id,
                    spec=ec2_spec,
                    checks=checks,
                    warnings=warnings,
                    errors=errors,
                )

        # --- C. Determine Final Verification Status ---
        status = self._evaluate_overall_status(checks=checks, warnings=warnings, errors=errors)

        summary = self._generate_summary(status=status, checks=checks, warnings=warnings, errors=errors)

        report = DeploymentVerificationReport(
            deployment_id=deployment_id,
            aws_account_id=account_id,
            aws_region=region,
            verification_status=status,
            verified_at=datetime.now(timezone.utc),
            resource_ids=resource_ids,
            checks=checks,
            resources=resources_details,
            warnings=warnings,
            errors=errors,
            is_mock=False,
            summary=summary,
        )

        # Persist report to SQLite
        self.repo.record_verification_report(deployment_id, report.verification_status, report.model_dump(mode="json"))
        return report

    def _resolve_expected_ami(self, deployment_id: Optional[str], spec: Any) -> Optional[str]:
        """Resolves the deployment-specific approved AMI ID baseline.

        Checks the deployment's approved Terraform configuration (e.g. variables.tf)
        in the workspace. If the deployment workspace specifies an approved AMI, that
        approved AMI forms the authoritative baseline. Otherwise, falls back to the
        approved specification (spec.ami.ami_id).
        """
        if deployment_id:
            try:
                from pathlib import Path
                import re
                ws_dir = Path("workspaces") / deployment_id
                vars_path = ws_dir / "variables.tf"
                if vars_path.exists():
                    content = vars_path.read_text(encoding="utf-8")
                    m = re.search(r'variable\s+"ami_id"\s*\{[^}]*default\s*=\s*"([^"]+)"', content)
                    if m and m.group(1).strip():
                        return m.group(1).strip()
                tfvars_path = ws_dir / "terraform.tfvars"
                if tfvars_path.exists():
                    content = tfvars_path.read_text(encoding="utf-8")
                    m = re.search(r'ami_id\s*=\s*"([^"]+)"', content)
                    if m and m.group(1).strip():
                        return m.group(1).strip()
            except Exception:
                pass

        if spec:
            if hasattr(spec, "ami") and spec.ami:
                ami_obj = spec.ami
                if hasattr(ami_obj, "ami_id") and ami_obj.ami_id:
                    return str(ami_obj.ami_id).strip()
                if isinstance(ami_obj, dict) and ami_obj.get("ami_id"):
                    return str(ami_obj["ami_id"]).strip()
            elif isinstance(spec, dict):
                ami_dict = spec.get("ami")
                if isinstance(ami_dict, dict) and ami_dict.get("ami_id"):
                    return str(ami_dict["ami_id"]).strip()
                elif hasattr(ami_dict, "ami_id") and ami_dict.ami_id:
                    return str(ami_dict.ami_id).strip()

        return None

    def _inspect_ec2_instance(
        self,
        ec2_client: Any,
        instance_id: str,
        spec: Any,
        checks: List[PropertyVerificationCheck],
        warnings: List[str],
        errors: List[str],
        deployment_id: Optional[str] = None,
    ) -> ResourceVerificationDetails:
        """Inspects instance existence, operational state, type, VPC, and attached EBS volumes."""
        details = ResourceVerificationDetails(
            resource_type="aws_instance",
            resource_id=instance_id,
            exists=False,
            operational_state="not_found",
        )

        # Bounded retry call to describe_instances
        resp = self._call_with_retry(
            lambda: ec2_client.describe_instances(InstanceIds=[instance_id]),
            max_retries=3,
        )

        if not resp or not resp.get("Reservations") or not resp["Reservations"][0].get("Instances"):
            checks.append(
                PropertyVerificationCheck(
                    category="EC2",
                    property_name="Instance Existence",
                    expected_value=instance_id,
                    actual_value="NOT_FOUND",
                    result=PropertyComparisonResult.NOT_FOUND,
                    explanation=f"Instance '{instance_id}' does not exist in target AWS account and region.",
                    severity=VerificationSeverity.ERROR,
                )
            )
            return details

        inst = resp["Reservations"][0]["Instances"][0]
        details.exists = True
        details.raw_details = {
            "InstanceId": inst.get("InstanceId"),
            "InstanceType": inst.get("InstanceType"),
            "State": inst.get("State", {}).get("Name"),
            "VpcId": inst.get("VpcId"),
            "SubnetId": inst.get("SubnetId"),
            "PublicIp": inst.get("PublicIpAddress"),
            "PrivateIp": inst.get("PrivateIpAddress"),
            "RootDeviceName": inst.get("RootDeviceName"),
        }

        # 1. Existence Check
        checks.append(
            PropertyVerificationCheck(
                category="EC2",
                property_name="Instance Existence",
                expected_value=instance_id,
                actual_value=instance_id,
                result=PropertyComparisonResult.MATCH,
                explanation=f"Instance {instance_id} exists in AWS.",
                severity=VerificationSeverity.INFO,
            )
        )

        # 2. Operational State (Separated from Conformance)
        state_name = inst.get("State", {}).get("Name", "unknown")
        details.operational_state = state_name

        if state_name == "running":
            checks.append(
                PropertyVerificationCheck(
                    category="EC2",
                    property_name="Operational State",
                    expected_value="running",
                    actual_value="running",
                    result=PropertyComparisonResult.MATCH,
                    explanation="EC2 instance is in active running state.",
                    severity=VerificationSeverity.INFO,
                )
            )
        elif state_name == "pending":
            checks.append(
                PropertyVerificationCheck(
                    category="EC2",
                    property_name="Operational State",
                    expected_value="running",
                    actual_value="pending",
                    result=PropertyComparisonResult.MISMATCH,
                    explanation="EC2 instance is currently initializing (pending). Not yet in running state.",
                    severity=VerificationSeverity.WARNING,
                )
            )
            warnings.append(f"Instance '{instance_id}' is in 'pending' operational state.")
        else:
            checks.append(
                PropertyVerificationCheck(
                    category="EC2",
                    property_name="Operational State",
                    expected_value="running",
                    actual_value=state_name,
                    result=PropertyComparisonResult.MISMATCH,
                    explanation=f"EC2 instance is in non-running state: '{state_name}'.",
                    severity=VerificationSeverity.ERROR,
                )
            )

        if not spec:
            return details

        # 3. Instance Sizing Conformance
        actual_type = inst.get("InstanceType", "")
        expected_type = spec.instance_type
        if actual_type.lower() == expected_type.lower():
            checks.append(
                PropertyVerificationCheck(
                    category="EC2",
                    property_name="Instance Sizing",
                    expected_value=expected_type,
                    actual_value=actual_type,
                    result=PropertyComparisonResult.MATCH,
                    explanation=f"Instance type matches specification: {actual_type}.",
                    severity=VerificationSeverity.INFO,
                )
            )
        else:
            checks.append(
                PropertyVerificationCheck(
                    category="EC2",
                    property_name="Instance Sizing",
                    expected_value=expected_type,
                    actual_value=actual_type,
                    result=PropertyComparisonResult.MISMATCH,
                    explanation=f"Instance sizing mismatch: expected {expected_type}, found {actual_type}.",
                    severity=VerificationSeverity.ERROR,
                )
            )

        # 4. AMI Conformance
        actual_ami = inst.get("ImageId", "")
        expected_ami = self._resolve_expected_ami(deployment_id, spec)
        if expected_ami:
            if actual_ami == expected_ami:
                checks.append(
                    PropertyVerificationCheck(
                        category="EC2",
                        property_name="AMI ID",
                        expected_value=expected_ami,
                        actual_value=actual_ami,
                        result=PropertyComparisonResult.MATCH,
                        explanation=f"AMI ID matches approved specification: {actual_ami}.",
                        severity=VerificationSeverity.INFO,
                    )
                )
            else:
                checks.append(
                    PropertyVerificationCheck(
                        category="EC2",
                        property_name="AMI ID",
                        expected_value=expected_ami,
                        actual_value=actual_ami,
                        result=PropertyComparisonResult.MISMATCH,
                        explanation=f"AMI ID mismatch: expected {expected_ami}, found {actual_ami}.",
                        severity=VerificationSeverity.ERROR,
                    )
                )

        # 5. Network & Public IP Conformance
        has_public_ip = bool(inst.get("PublicIpAddress"))
        expected_public = spec.network.public_ip
        actual_public_ip_str = inst.get("PublicIpAddress") or "None"

        if not expected_public and has_public_ip:
            checks.append(
                PropertyVerificationCheck(
                    category="Network",
                    property_name="Public IP Exposure",
                    expected_value="Disabled (Private)",
                    actual_value=f"Enabled ({actual_public_ip_str})",
                    result=PropertyComparisonResult.MISMATCH,
                    explanation=f"Security violation: Specification required private instance, but public IP {actual_public_ip_str} was assigned!",
                    severity=VerificationSeverity.HIGH,
                )
            )
            warnings.append(f"Security Alert: Public IP {actual_public_ip_str} assigned to intended private instance.")
        elif expected_public and not has_public_ip:
            checks.append(
                PropertyVerificationCheck(
                    category="Network",
                    property_name="Public IP Exposure",
                    expected_value="Enabled",
                    actual_value="Disabled (No Public IP)",
                    result=PropertyComparisonResult.MISMATCH,
                    explanation="Specification requested public IP assignment, but none was assigned.",
                    severity=VerificationSeverity.ERROR,
                )
            )
        else:
            checks.append(
                PropertyVerificationCheck(
                    category="Network",
                    property_name="Public IP Exposure",
                    expected_value="Enabled" if expected_public else "Disabled (Private)",
                    actual_value=actual_public_ip_str if expected_public else "Disabled (Private)",
                    result=PropertyComparisonResult.MATCH,
                    explanation="Public IP assignment matches specification.",
                    severity=VerificationSeverity.INFO,
                )
            )

        # VPC & Subnet alignment
        actual_vpc = inst.get("VpcId", "")
        if spec.network.vpc_id:
            checks.append(
                PropertyVerificationCheck(
                    category="Network",
                    property_name="VPC ID",
                    expected_value=spec.network.vpc_id,
                    actual_value=actual_vpc,
                    result=PropertyComparisonResult.MATCH if actual_vpc == spec.network.vpc_id else PropertyComparisonResult.MISMATCH,
                    explanation=f"VPC ID {'matches' if actual_vpc == spec.network.vpc_id else 'differs from'} expected specification.",
                    severity=VerificationSeverity.ERROR if actual_vpc != spec.network.vpc_id else VerificationSeverity.INFO,
                )
            )

        # 6. EBS Root Volume Verification via Actual Device Mapping
        root_device = inst.get("RootDeviceName")
        block_mappings = inst.get("BlockDeviceMappings", [])
        root_mapping = next((b for b in block_mappings if b.get("DeviceName") == root_device), None)

        if root_mapping and root_mapping.get("Ebs", {}).get("VolumeId"):
            root_vol_id = root_mapping["Ebs"]["VolumeId"]
            self._inspect_ebs_volume(
                ec2_client=ec2_client,
                volume_id=root_vol_id,
                expected_device=root_device,
                expected_instance_id=instance_id,
                storage_spec=spec.storage,
                checks=checks,
                warnings=warnings,
                errors=errors,
            )
        elif block_mappings and block_mappings[0].get("Ebs", {}).get("VolumeId"):
            fallback_vol_id = block_mappings[0]["Ebs"]["VolumeId"]
            self._inspect_ebs_volume(
                ec2_client=ec2_client,
                volume_id=fallback_vol_id,
                expected_device=block_mappings[0].get("DeviceName"),
                expected_instance_id=instance_id,
                storage_spec=spec.storage,
                checks=checks,
                warnings=warnings,
                errors=errors,
            )
        else:
            checks.append(
                PropertyVerificationCheck(
                    category="EBS",
                    property_name="Root Volume Attachment",
                    expected_value="Attached EBS Volume",
                    actual_value="None",
                    result=PropertyComparisonResult.NOT_FOUND,
                    explanation="No attached root EBS volume found on EC2 instance.",
                    severity=VerificationSeverity.ERROR,
                )
            )

        return details

    def _inspect_ebs_volume(
        self,
        ec2_client: Any,
        volume_id: str,
        expected_device: Optional[str],
        expected_instance_id: str,
        storage_spec: Any,
        checks: List[PropertyVerificationCheck],
        warnings: List[str],
        errors: List[str],
    ) -> None:
        """Inspects EBS volume existence, attachment mapping, size, volume type, and encryption."""
        resp = self._call_with_retry(
            lambda: ec2_client.describe_volumes(VolumeIds=[volume_id]),
            max_retries=3,
        )

        if not resp or not resp.get("Volumes"):
            checks.append(
                PropertyVerificationCheck(
                    category="EBS",
                    property_name="Volume Existence",
                    expected_value=volume_id,
                    actual_value="NOT_FOUND",
                    result=PropertyComparisonResult.NOT_FOUND,
                    explanation=f"EBS volume {volume_id} was not found in AWS.",
                    severity=VerificationSeverity.ERROR,
                )
            )
            return

        vol = resp["Volumes"][0]
        actual_size = vol.get("Size", 0)
        actual_type = vol.get("VolumeType", "")
        is_encrypted = vol.get("Encrypted", False)
        attachments = vol.get("Attachments", [])

        # 1. Attachment & Mapping Verification
        attached_to_inst = any(
            att.get("InstanceId") == expected_instance_id and (not expected_device or att.get("Device") == expected_device)
            for att in attachments
        )
        checks.append(
            PropertyVerificationCheck(
                category="EBS",
                property_name="Volume Attachment & Device Mapping",
                expected_value=f"Attached to {expected_instance_id} at {expected_device or 'root'}",
                actual_value=f"{'Attached' if attached_to_inst else 'Detached / Wrong Device'}",
                result=PropertyComparisonResult.MATCH if attached_to_inst else PropertyComparisonResult.MISMATCH,
                explanation=f"Volume {volume_id} attachment verified against instance device mapping.",
                severity=VerificationSeverity.INFO if attached_to_inst else VerificationSeverity.ERROR,
            )
        )

        # 2. Volume Sizing Conformance
        expected_size = storage_spec.size_gb
        checks.append(
            PropertyVerificationCheck(
                category="EBS",
                property_name="Volume Storage Size",
                expected_value=f"{expected_size} GiB",
                actual_value=f"{actual_size} GiB",
                result=PropertyComparisonResult.MATCH if actual_size == expected_size else PropertyComparisonResult.MISMATCH,
                explanation=f"EBS volume size {'matches' if actual_size == expected_size else 'differs from'} specification.",
                severity=VerificationSeverity.INFO if actual_size == expected_size else VerificationSeverity.ERROR,
            )
        )

        # 3. Volume Type Conformance
        expected_vol_type = storage_spec.volume_type.value if hasattr(storage_spec.volume_type, "value") else str(storage_spec.volume_type)
        checks.append(
            PropertyVerificationCheck(
                category="EBS",
                property_name="Volume Type",
                expected_value=expected_vol_type,
                actual_value=actual_type,
                result=PropertyComparisonResult.MATCH if actual_type.lower() == expected_vol_type.lower() else PropertyComparisonResult.MISMATCH,
                explanation=f"EBS volume type: {actual_type}.",
                severity=VerificationSeverity.INFO if actual_type.lower() == expected_vol_type.lower() else VerificationSeverity.ERROR,
            )
        )

        # 4. Volume Encryption Conformance
        expected_encrypted = storage_spec.encrypted
        if expected_encrypted and not is_encrypted:
            checks.append(
                PropertyVerificationCheck(
                    category="EBS",
                    property_name="Volume Encryption",
                    expected_value="Encrypted (KMS Enabled)",
                    actual_value="Unencrypted",
                    result=PropertyComparisonResult.MISMATCH,
                    explanation=f"Security violation: Specification required EBS volume encryption, but {volume_id} is unencrypted!",
                    severity=VerificationSeverity.HIGH,
                )
            )
            warnings.append(f"Security Alert: EBS volume '{volume_id}' is unencrypted.")
        else:
            checks.append(
                PropertyVerificationCheck(
                    category="EBS",
                    property_name="Volume Encryption",
                    expected_value="Encrypted" if expected_encrypted else "Unencrypted",
                    actual_value="Encrypted" if is_encrypted else "Unencrypted",
                    result=PropertyComparisonResult.MATCH,
                    explanation=f"EBS encryption state conforms to specification (Encrypted: {is_encrypted}).",
                    severity=VerificationSeverity.INFO,
                )
            )

    def _inspect_security_group(
        self,
        ec2_client: Any,
        sg_id: str,
        spec: Any,
        checks: List[PropertyVerificationCheck],
        warnings: List[str],
        errors: List[str],
    ) -> None:
        """Inspects security group rules and flags unrestricted SSH/RDP ingress as HIGH severity."""
        resp = self._call_with_retry(
            lambda: ec2_client.describe_security_groups(GroupIds=[sg_id]),
            max_retries=3,
        )

        if not resp or not resp.get("SecurityGroups"):
            checks.append(
                PropertyVerificationCheck(
                    category="Security Group",
                    property_name="Group Existence",
                    expected_value=sg_id,
                    actual_value="NOT_FOUND",
                    result=PropertyComparisonResult.NOT_FOUND,
                    explanation=f"Security Group '{sg_id}' not found in AWS.",
                    severity=VerificationSeverity.ERROR,
                )
            )
            return

        sg = resp["SecurityGroups"][0]
        ip_permissions = sg.get("IpPermissions", [])

        # Existence
        checks.append(
            PropertyVerificationCheck(
                category="Security Group",
                property_name="Group Existence",
                expected_value=sg_id,
                actual_value=sg_id,
                result=PropertyComparisonResult.MATCH,
                explanation=f"Security Group {sg_id} exists ({sg.get('GroupName')}).",
                severity=VerificationSeverity.INFO,
            )
        )

        # Ingress Audit for Unrestricted Ports
        unrestricted_ssh = False
        unrestricted_rdp = False

        for rule in ip_permissions:
            from_port = rule.get("FromPort")
            to_port = rule.get("ToPort")
            ip_ranges = [r.get("CidrIp") for r in rule.get("IpRanges", []) if r.get("CidrIp")]
            ipv6_ranges = [r.get("CidrIpv6") for r in rule.get("Ipv6Ranges", []) if r.get("CidrIpv6")]

            all_cidrs = ip_ranges + ipv6_ranges
            is_open = "0.0.0.0/0" in all_cidrs or "::/0" in all_cidrs

            if is_open:
                if from_port is not None and to_port is not None:
                    if from_port <= 22 <= to_port:
                        unrestricted_ssh = True
                    if from_port <= 3389 <= to_port:
                        unrestricted_rdp = True

        if unrestricted_ssh:
            checks.append(
                PropertyVerificationCheck(
                    category="Security Group",
                    property_name="SSH Ingress (Port 22)",
                    expected_value="Restricted CIDR (No 0.0.0.0/0)",
                    actual_value="Exposed to 0.0.0.0/0",
                    result=PropertyComparisonResult.MISMATCH,
                    explanation=f"Security Group {sg_id} permits unrestricted inbound SSH (port 22) from the public Internet.",
                    severity=VerificationSeverity.HIGH,
                )
            )
            warnings.append(f"Security Alert: Security group '{sg_id}' allows unrestricted SSH from 0.0.0.0/0.")

        if unrestricted_rdp:
            checks.append(
                PropertyVerificationCheck(
                    category="Security Group",
                    property_name="RDP Ingress (Port 3389)",
                    expected_value="Restricted CIDR (No 0.0.0.0/0)",
                    actual_value="Exposed to 0.0.0.0/0",
                    result=PropertyComparisonResult.MISMATCH,
                    explanation=f"Security Group {sg_id} permits unrestricted inbound RDP (port 3389) from the public Internet.",
                    severity=VerificationSeverity.HIGH,
                )
            )
            warnings.append(f"Security Alert: Security group '{sg_id}' allows unrestricted RDP from 0.0.0.0/0.")

        if not unrestricted_ssh and not unrestricted_rdp:
            checks.append(
                PropertyVerificationCheck(
                    category="Security Group",
                    property_name="Restricted Administrative Ingress",
                    expected_value="No open 0.0.0.0/0 on SSH/RDP",
                    actual_value="Restricted / Private Ingress",
                    result=PropertyComparisonResult.MATCH,
                    explanation="No unrestricted public administrative ports (22, 3389) detected.",
                    severity=VerificationSeverity.INFO,
                )
            )

    def _verify_mock_conformance(
        self,
        deployment_id: str,
        spec: NormalizedInfrastructureSpec,
        resource_ids: List[str],
        account_id: str,
        region: str,
    ) -> DeploymentVerificationReport:
        """Verifies simulated specification conformance for mock mode without calling AWS."""
        res = spec.resources[0] if spec.resources else None
        mock_inst_id = resource_ids[0] if resource_ids else "i-09f8e7d6c5b4a3210"
        mock_sg_id = resource_ids[1] if len(resource_ids) > 1 else "sg-01a2b3c4d5e6f7890"

        checks = [
            PropertyVerificationCheck(
                category="EC2",
                property_name="Instance Existence",
                expected_value=mock_inst_id,
                actual_value=f"{mock_inst_id} (Simulated Mock ID)",
                result=PropertyComparisonResult.MATCH,
                explanation="Simulated instance exists in mock sandbox.",
                severity=VerificationSeverity.INFO,
            ),
            PropertyVerificationCheck(
                category="EC2",
                property_name="Operational State",
                expected_value="running",
                actual_value="running (Simulated)",
                result=PropertyComparisonResult.MATCH,
                explanation="Simulated operational state confirmed running.",
                severity=VerificationSeverity.INFO,
            ),
            PropertyVerificationCheck(
                category="EC2",
                property_name="Instance Sizing",
                expected_value=res.instance_type if res else "t3.micro",
                actual_value=res.instance_type if res else "t3.micro",
                result=PropertyComparisonResult.MATCH,
                explanation="Simulated instance sizing matches specification.",
                severity=VerificationSeverity.INFO,
            ),
            PropertyVerificationCheck(
                category="EBS",
                property_name="Volume Storage Size",
                expected_value=f"{res.storage.size_gb if res else 20} GiB",
                actual_value=f"{res.storage.size_gb if res else 20} GiB",
                result=PropertyComparisonResult.MATCH,
                explanation="Simulated storage size conforms to specification.",
                severity=VerificationSeverity.INFO,
            ),
            PropertyVerificationCheck(
                category="EBS",
                property_name="Volume Encryption",
                expected_value="Encrypted (KMS Enabled)" if (res and res.storage.encrypted) else "Disabled",
                actual_value="Encrypted (KMS Enabled)" if (res and res.storage.encrypted) else "Disabled",
                result=PropertyComparisonResult.MATCH,
                explanation="Simulated EBS volume encryption verified.",
                severity=VerificationSeverity.INFO,
            ),
            PropertyVerificationCheck(
                category="Network",
                property_name="Public IP Exposure",
                expected_value="Enabled" if (res and res.network.public_ip) else "Disabled (Private)",
                actual_value="Enabled" if (res and res.network.public_ip) else "Disabled (Private)",
                result=PropertyComparisonResult.MATCH,
                explanation="Simulated networking exposure verified against specification.",
                severity=VerificationSeverity.INFO,
            ),
            PropertyVerificationCheck(
                category="Security Group",
                property_name="Restricted Administrative Ingress",
                expected_value="Restricted Ingress",
                actual_value="Restricted (Simulated)",
                result=PropertyComparisonResult.MATCH,
                explanation="Simulated security group rules verified.",
                severity=VerificationSeverity.INFO,
            ),
        ]

        report = DeploymentVerificationReport(
            deployment_id=deployment_id,
            aws_account_id=account_id,
            aws_region=region,
            verification_status=VerificationStatus.VERIFIED,
            verified_at=datetime.now(timezone.utc),
            resource_ids=[mock_inst_id, mock_sg_id],
            checks=checks,
            resources=[
                ResourceVerificationDetails(
                    resource_type="aws_instance",
                    resource_id=mock_inst_id,
                    exists=True,
                    operational_state="running",
                    checks=checks[:3],
                    raw_details={"InstanceId": mock_inst_id, "State": "running", "Mock": True},
                )
            ],
            warnings=[],
            errors=[],
            is_mock=True,
            summary="All simulated mock infrastructure properties conform to the approved specification.",
        )

        self.repo.record_verification_report(deployment_id, report.verification_status, report.model_dump(mode="json"))
        return report

    def _call_with_retry(self, fn: Any, max_retries: int = 3, backoff: float = 0.05) -> Optional[Any]:
        """Executes a read-only Boto3 call with bounded retry backoff for transient API errors."""
        retries = 0
        current_backoff = backoff
        while retries < max_retries:
            try:
                return fn()
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("InvalidInstanceID.NotFound", "InvalidGroup.NotFound", "InvalidVolume.NotFound"):
                    return None
                if code in ("RequestLimitExceeded", "ThrottlingException", "Throttling"):
                    retries += 1
                    time.sleep(current_backoff)
                    current_backoff *= 2
                    continue
                # Authentication or permission failures
                return None
            except EndpointConnectionError:
                retries += 1
                time.sleep(current_backoff)
                continue
            except Exception:
                return None
        return None

    def _evaluate_overall_status(
        self,
        checks: List[PropertyVerificationCheck],
        warnings: List[str],
        errors: List[str],
    ) -> VerificationStatus:
        """Determines final verification status based on property check results and severities."""
        if any(c.result == PropertyComparisonResult.NOT_FOUND for c in checks):
            return VerificationStatus.RESOURCE_NOT_FOUND

        # Configuration mismatches on core resources (EC2 sizing, AMI, encryption, public/private IP exposure)
        has_core_mismatch = any(
            c.category in ("EC2", "EBS", "Network")
            and c.result == PropertyComparisonResult.MISMATCH
            and c.severity in (VerificationSeverity.ERROR, VerificationSeverity.HIGH)
            for c in checks
        )
        if has_core_mismatch:
            return VerificationStatus.CONFIGURATION_MISMATCH

        if any(c.result == PropertyComparisonResult.UNABLE_TO_VERIFY for c in checks):
            return VerificationStatus.PARTIALLY_VERIFIED

        if warnings or any(c.severity in (VerificationSeverity.WARNING, VerificationSeverity.HIGH) for c in checks):
            return VerificationStatus.VERIFIED_WITH_WARNINGS

        return VerificationStatus.VERIFIED

    def _generate_summary(
        self,
        status: VerificationStatus,
        checks: List[PropertyVerificationCheck],
        warnings: List[str],
        errors: List[str],
    ) -> str:
        passed = sum(1 for c in checks if c.result == PropertyComparisonResult.MATCH)
        total = len(checks)
        if status == VerificationStatus.VERIFIED:
            return f"All {total} live AWS infrastructure properties verified and conform to the approved specification."
        elif status == VerificationStatus.VERIFIED_WITH_WARNINGS:
            return f"{passed} of {total} checks passed with {len(warnings)} non-critical warning(s)."
        elif status == VerificationStatus.CONFIGURATION_MISMATCH:
            return f"Configuration mismatch detected: one or more live cloud properties differ from approved specification."
        elif status == VerificationStatus.RESOURCE_NOT_FOUND:
            return "Expected AWS infrastructure resources were not found in the target account/region."
        else:
            return f"Verification completed with status: {status.value}."
