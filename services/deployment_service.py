import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional
from config import config
from core.exceptions import ApprovalError, ConfigurationError, TerraformExecutionError
from core.models import (
    ApprovalStatus,
    DeploymentRecord,
    DeploymentStatus,
    NormalizedInfrastructureSpec,
    VerificationStatus,
)
from database.repository import DeploymentRepository
from services.aws_auth_service import AWSAuthService, aws_auth_service
from services.aws_verification_service import AWSVerificationService
from services.terraform_service import TerraformService


class DeploymentService:
    """Orchestrates controlled Terraform deployment execution, cryptographic integrity verification,
    and partial deployment tracking."""

    def __init__(
        self,
        repository: Optional[DeploymentRepository] = None,
        auth_service: Optional[AWSAuthService] = None,
        terraform_service: Optional[TerraformService] = None,
        verification_service: Optional[AWSVerificationService] = None,
        terraform_binary: Optional[str] = None,
    ):
        self.repo = repository or DeploymentRepository()
        self.auth_service = auth_service or aws_auth_service
        self.tf_service = terraform_service or TerraformService(repository=self.repo)
        self.verification_service = verification_service or AWSVerificationService(
            repository=self.repo,
            auth_service=self.auth_service,
        )
        self._terraform_binary = terraform_binary if terraform_binary is not None else shutil.which("terraform")

    def execute_deployment(
        self,
        deployment_id: str,
        spec: NormalizedInfrastructureSpec,
        approved_plan_hash: str,
    ) -> Dict[str, Any]:
        """Executes deployment with strict cryptographic plan-hash integrity and target account verification."""
        record = self.repo.get(deployment_id)
        if not record:
            raise ApprovalError(f"Deployment record '{deployment_id}' not found in database.")

        # 1. Concurrency Lock: Prevent simultaneous executions
        if record.status == DeploymentStatus.IN_PROGRESS:
            raise ApprovalError(f"Deployment '{deployment_id}' is already in progress.")

        # 2. Terminal State Lock: Prevent duplicate execution of already executed deployments
        if record.status in (DeploymentStatus.COMPLETED, DeploymentStatus.PARTIALLY_DEPLOYED):
            raise ApprovalError(
                f"Deployment '{deployment_id}' cannot be re-executed because it has already reached terminal status '{record.status.value}'. "
                f"Please initiate a new planning cycle."
            )

        # 3. Explicit Approval Verification
        if record.approval_status != ApprovalStatus.APPROVED:
            raise ApprovalError("Cannot execute deployment: Deployment has not received explicit operator approval.")

        # 4. Cryptographic Plan Integrity Check (Database vs Approved Parameter)
        if record.plan_hash != approved_plan_hash:
            raise ApprovalError(
                f"Plan integrity verification failed! Stored plan hash does not match current plan hash. "
                f"Re-approval is required."
            )

        # 5. Routing: Real vs Mock Mode
        if config.is_real_deployment_allowed():
            return self._execute_real_deployment(deployment_id, spec, record, approved_plan_hash)
        else:
            return self._simulate_mock_deployment(deployment_id, spec, record, approved_plan_hash)

    def _simulate_mock_deployment(
        self,
        deployment_id: str,
        spec: NormalizedInfrastructureSpec,
        record: DeploymentRecord,
        approved_plan_hash: str,
    ) -> Dict[str, Any]:
        """Simulates deployment steps cleanly for mock mode with complete SQLite audit trail."""
        res = spec.resources[0] if spec.resources else None
        target_account = record.aws_account_id or "123456789012"
        target_region = record.aws_region or spec.region
        inst_name = res.name if res else "app-server"
        short_id = deployment_id.replace("-", "")[:8]
        mock_instance_id = f"i-sim-{short_id}"
        mock_sg_id = f"sg-sim-{short_id}"
        mock_role_name = f"{inst_name}-role-{short_id}"
        mock_profile_name = f"{inst_name}-profile-{short_id}"

        # Record Execution Start
        self.repo.record_execution_start(
            deployment_id=deployment_id,
            execution_mode="mock",
            aws_account_id=target_account,
            aws_identity_arn=record.aws_identity_arn or f"arn:aws:iam::{target_account}:role/MockDeploymentRole",
            aws_region=target_region,
            credential_source_type="mock",
        )

        mock_logs = (
            f"=== MOCK DEPLOYMENT LOG ===\n"
            f"Execution Mode: MOCK (Simulation)\n"
            f"Target Account: {target_account}\n"
            f"Target Region: {target_region}\n"
            f"Applying compiled plan: tfplan\n"
            f"aws_iam_role.app_role: Creating...\n"
            f"aws_iam_role.app_role: Creation complete after 1s [id={mock_role_name}]\n"
            f"aws_iam_role_policy_attachment.app_role_ssm: Creating...\n"
            f"aws_iam_role_policy_attachment.app_role_ssm: Creation complete after 1s\n"
            f"aws_iam_instance_profile.app_instance_profile: Creating...\n"
            f"aws_iam_instance_profile.app_instance_profile: Creation complete after 1s [id={mock_profile_name}]\n"
            f"aws_security_group.app_sg: Creating...\n"
            f"aws_security_group.app_sg: Creation complete after 2s [id={mock_sg_id}]\n"
            f"aws_instance.app_server: Creating...\n"
            f"aws_instance.app_server: Creation complete after 5s [id={mock_instance_id}]\n"
            f"Apply complete! Resources: 5 added, 0 changed, 0 destroyed.\n"
        )

        # Record Execution Completion
        self.repo.record_execution_complete(
            deployment_id=deployment_id,
            status=DeploymentStatus.COMPLETED,
            exit_code=0,
            aws_resource_ids=[mock_instance_id, mock_sg_id, mock_role_name, mock_profile_name],
            execution_logs=mock_logs,
        )

        # Run simulated verification
        verification_results = self.verify_deployment(
            deployment_id=deployment_id,
            spec=spec,
            instance_id=mock_instance_id,
            resource_ids=[mock_instance_id, mock_sg_id],
            is_mock=True,
            account_id=target_account,
            region=target_region,
        )

        return {
            "deployment_id": deployment_id,
            "status": "COMPLETED (MOCK)",
            "execution_mode": "mock",
            "instance_id": mock_instance_id,
            "security_group_id": mock_sg_id,
            "private_ip": "10.0.1.45",
            "public_ip": "3.109.12.88" if (res and res.network.public_ip) else None,
            "region": target_region,
            "account_id": target_account,
            "verification": verification_results,
            "logs": mock_logs,
        }

    def _execute_real_deployment(
        self,
        deployment_id: str,
        spec: NormalizedInfrastructureSpec,
        record: DeploymentRecord,
        approved_plan_hash: str,
    ) -> Dict[str, Any]:
        """Executes real Terraform deployment using active in-memory AWS credentials.

        CRITICAL SECURITY & SAFETY RULES:
        1. Only executed if ENABLE_MOCK_MODE=False AND ENABLE_REAL_TERRAFORM_APPLY=True.
        2. In-memory credentials only — never written to disk or configuration files.
        3. Credentials passed strictly via subprocess environment variables.
        4. Immediate recalculation of configuration & binary plan hash before apply.
        5. Target AWS Account and Region binding verified; mismatch revokes approval.
        6. All output and logs redacted of secrets before persistence or return.
        7. Exact command: `terraform apply -input=false -auto-approve tfplan`.
        8. On failure, NEVER auto-destroy or trigger rollback. Current state is parsed.
        9. Clearly distinguishes SUCCEEDED, PARTIALLY_DEPLOYED, FAILED, and EXECUTION_STATUS_UNKNOWN.
        """
        # Guard: Ensure deliberate configuration
        if not config.is_real_deployment_allowed():
            raise ConfigurationError(
                "Real Terraform Apply is blocked: ENABLE_REAL_TERRAFORM_APPLY must be True and "
                "ENABLE_MOCK_MODE must be False."
            )

        # Validate Active AWS Credentials
        os.environ.setdefault("AWS_SDK_LOAD_CONFIG", "1")
        creds = self.auth_service.get_active_credentials()
        identity = self.auth_service.get_active_identity()
        if not creds or not identity or not identity.is_valid:
            target_region = record.aws_region or spec.region or "ap-south-1"
            # Attempt to authenticate using named profile if session in memory is uninitialized
            for prof in [os.environ.get("AWS_PROFILE"), "cloud-chat-deploy", "default"]:
                if prof:
                    try:
                        identity = self.auth_service.authenticate_profile(prof, region=target_region)
                        if identity and identity.is_valid:
                            creds = self.auth_service.get_active_credentials()
                            break
                    except Exception:
                        continue

        if not creds or not identity or not identity.is_valid:
            raise ApprovalError(
                "No active authenticated AWS session found. Please authenticate with AWS in Settings before deploying."
            )

        # Target Account & Region Integrity Binding
        if record.aws_account_id and record.aws_account_id != identity.account_id:
            self.repo.update_approval(deployment_id, ApprovalStatus.REVOKED)
            raise ApprovalError(
                f"Security mismatch: Plan was approved for AWS Account '{record.aws_account_id}', "
                f"but active session is Account '{identity.account_id}'. Approval is revoked; re-approval is required."
            )

        if record.aws_region and record.aws_region != identity.region:
            self.repo.update_approval(deployment_id, ApprovalStatus.REVOKED)
            raise ApprovalError(
                f"Security mismatch: Plan was approved for Region '{record.aws_region}', "
                f"but active session is Region '{identity.region}'. Approval is revoked; re-approval is required."
            )

        # Locate Workspace & Compiled Plan Artifact
        workspace_dir = self.tf_service.get_workspace_dir(deployment_id)
        tfplan_path = workspace_dir / "tfplan"
        if not tfplan_path.exists():
            err_msg = f"Compiled plan artifact 'tfplan' not found in workspace '{workspace_dir}'. Planning must precede apply."
            self.repo.record_execution_complete(
                deployment_id=deployment_id,
                status=DeploymentStatus.FAILED,
                exit_code=-1,
                error_message=err_msg,
            )
            raise TerraformExecutionError(err_msg)

        # MANDATORY SAFETY CHECK: Block deployment if plan contains destructions or replacements
        if record.plan_summary:
            to_destroy = record.plan_summary.get("to_destroy", 0)
            is_destructive = record.plan_summary.get("is_destructive", False)
            if to_destroy > 0 or is_destructive:
                err_msg = (
                    f"Deployment blocked: Approved plan contains destructive changes ({to_destroy} resource(s) to destroy). "
                    f"Destructive replacements are not permitted in Cloud-Chat without explicit recovery."
                )
                self.repo.record_execution_complete(
                    deployment_id=deployment_id,
                    status=DeploymentStatus.FAILED,
                    exit_code=-1,
                    error_message=err_msg,
                )
                raise ApprovalError(err_msg)

        # MANDATORY STATE SAFETY CHECK: Block apply if workspace state contains tainted resources
        tainted_resources = self.detect_tainted_resources(deployment_id)
        if tainted_resources:
            err_msg = (
                f"Cannot execute Terraform apply: Workspace state contains tainted resource(s): "
                f"{', '.join(tainted_resources)}. Applying with tainted resources would destroy and replace "
                f"existing AWS resources. State reconciliation or untainting is required."
            )
            self.repo.record_execution_complete(
                deployment_id=deployment_id,
                status=DeploymentStatus.FAILED,
                exit_code=-1,
                error_message=err_msg,
            )
            raise ApprovalError(err_msg)

        # MANDATORY STATE BACKUP: Back up existing state before execution
        self.backup_state(deployment_id)

        # MANDATORY PRE-APPLY HASH RECALCULATION:
        # Verify that neither .tf configuration files nor the binary tfplan artifact were modified or replaced on disk.
        current_tf_files = {}
        for p in sorted(workspace_dir.glob("*.tf")):
            current_tf_files[p.name] = p.read_text(encoding="utf-8")
        current_plan_bytes = tfplan_path.read_bytes()
        recalculated_hash = self.tf_service.calculate_plan_hash(current_tf_files, current_plan_bytes)

        if recalculated_hash != record.plan_hash or recalculated_hash != approved_plan_hash:
            self.repo.update_approval(deployment_id, ApprovalStatus.REVOKED)
            err_msg = (
                f"Plan integrity verification failed! The Terraform files or binary tfplan on disk "
                f"have been altered since approval (recalculated: {recalculated_hash[:12]}..., "
                f"expected: {record.plan_hash[:12] if record.plan_hash else 'None'}...). "
                f"Approval is revoked and re-planning is required."
            )
            self.repo.record_execution_complete(
                deployment_id=deployment_id,
                status=DeploymentStatus.FAILED,
                exit_code=-1,
                error_message=err_msg,
            )
            raise ApprovalError(err_msg)

        # Record Execution Start in SQLite
        self.repo.record_execution_start(
            deployment_id=deployment_id,
            execution_mode="real",
            aws_account_id=identity.account_id,
            aws_identity_arn=identity.arn,
            aws_region=identity.region,
            credential_source_type=identity.auth_method.value,
        )

        # Prepare Subprocess Environment with In-Memory Credentials
        subprocess_env = creds.get_subprocess_env()

        # Build Command: EXACT apply invocation
        tf_bin = self._terraform_binary or "terraform"
        cmd = [tf_bin, "apply", "-input=false", "-auto-approve", "tfplan"]

        execution_status_unknown = False
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace_dir),
                env=subprocess_env,
                capture_output=True,
                text=True,
                timeout=config.terraform_timeout_seconds,
            )
            raw_stdout = proc.stdout or ""
            raw_stderr = proc.stderr or ""
            exit_code = proc.returncode

        except subprocess.TimeoutExpired:
            raw_stdout = ""
            raw_stderr = f"Terraform apply execution timed out after {config.terraform_timeout_seconds} seconds."
            exit_code = -1
            execution_status_unknown = True

        except Exception as e:
            raw_stdout = ""
            raw_stderr = f"Terraform subprocess execution error: {str(e)}"
            exit_code = -1
            execution_status_unknown = True

        # Redact secrets from all stdout and stderr
        clean_stdout = creds.redact_secrets(raw_stdout)
        clean_stderr = creds.redact_secrets(raw_stderr)
        full_logs = (
            f"=== TERRAFORM APPLY STDOUT ===\n{clean_stdout}\n"
            f"=== TERRAFORM APPLY STDERR ===\n{clean_stderr}"
        ).strip()

        # Persist redacted execution log file to workspace
        (workspace_dir / "apply.log").write_text(full_logs, encoding="utf-8")

        # Parse Provisioned / Partial Resources from Terraform State (with state file fallback)
        state_resources = self._extract_state_resources(workspace_dir, subprocess_env, creds)
        resource_ids = [r["id"] for r in state_resources if r.get("id")]

        if exit_code == 0:
            # 1. SUCCEEDED / COMPLETED (Only if independent AWS verification passes)
            primary_instance_id = next((r["id"] for r in state_resources if r["type"] == "aws_instance"), "unknown")
            primary_sg_id = next((r["id"] for r in state_resources if r["type"] == "aws_security_group"), None)

            verification_results = self.verify_deployment(
                deployment_id=deployment_id,
                spec=spec,
                instance_id=primary_instance_id,
                resource_ids=resource_ids,
                is_mock=False,
                account_id=identity.account_id,
                region=identity.region,
            )

            verif_status = verification_results.get("overall_status")
            if verif_status == VerificationStatus.CONFIGURATION_MISMATCH.value:
                final_status = DeploymentStatus.PARTIALLY_DEPLOYED
                err_msg = f"Terraform apply succeeded, but live AWS verification reported configuration mismatch: {verification_results.get('summary')}"
            else:
                final_status = DeploymentStatus.COMPLETED
                err_msg = None

            self.repo.record_execution_complete(
                deployment_id=deployment_id,
                status=final_status,
                exit_code=0,
                aws_resource_ids=resource_ids,
                execution_logs=full_logs,
                error_message=err_msg,
            )

            return {
                "deployment_id": deployment_id,
                "status": "SUCCEEDED" if final_status == DeploymentStatus.COMPLETED else "PARTIALLY_DEPLOYED",
                "execution_mode": "real",
                "instance_id": primary_instance_id,
                "security_group_id": primary_sg_id,
                "resource_ids": resource_ids,
                "resources": state_resources,
                "region": identity.region,
                "account_id": identity.account_id,
                "verification": verification_results,
                "logs": full_logs,
                "error": err_msg,
            }

        elif execution_status_unknown and not resource_ids:
            # 2. EXECUTION_STATUS_UNKNOWN (Process timed out, crashed, or state undetermined)
            err_summary = clean_stderr.strip()[:400] or "Execution ended abnormally with unconfirmed cloud state."
            self.repo.record_execution_complete(
                deployment_id=deployment_id,
                status=DeploymentStatus.EXECUTION_STATUS_UNKNOWN,
                exit_code=exit_code,
                aws_resource_ids=resource_ids,
                execution_logs=full_logs,
                error_message=f"Execution status unknown: {err_summary}",
            )
            return {
                "deployment_id": deployment_id,
                "status": "EXECUTION_STATUS_UNKNOWN",
                "execution_mode": "real",
                "exit_code": exit_code,
                "resource_ids": resource_ids,
                "error": err_summary,
                "logs": full_logs,
                "notice": "Terraform execution ended unexpectedly. Cloud state cannot be fully confirmed. Resources will NOT be destroyed.",
            }

        else:
            # 3. FAILED or PARTIALLY_DEPLOYED (State inspected, never auto-destroy)
            iam_error = self.extract_iam_permission_error(full_logs)
            if iam_error:
                err_summary = iam_error["message"]
            else:
                err_summary = clean_stderr.strip()[:400] or f"Process failed with non-zero exit code: {exit_code}"

            if len(resource_ids) > 0:
                final_status = DeploymentStatus.PARTIALLY_DEPLOYED
                status_str = "PARTIALLY_DEPLOYED"
            else:
                final_status = DeploymentStatus.FAILED
                status_str = "FAILED"

            self.repo.record_execution_complete(
                deployment_id=deployment_id,
                status=final_status,
                exit_code=exit_code,
                aws_resource_ids=resource_ids,
                execution_logs=full_logs,
                error_message=f"Terraform apply terminated [{status_str}]: {err_summary}",
            )

            res_dict = {
                "deployment_id": deployment_id,
                "status": status_str,
                "execution_mode": "real",
                "exit_code": exit_code,
                "partial_resource_ids": resource_ids,
                "resources": state_resources,
                "error": err_summary,
                "logs": full_logs,
                "notice": "Safe state preserved: Cloud-Chat never destroys partially provisioned resources.",
            }
            if iam_error:
                res_dict["iam_error"] = iam_error
            return res_dict

    def _extract_state_resources(
        self,
        workspace_dir: Path,
        env: Dict[str, str],
        creds: Any,
    ) -> List[Dict[str, Any]]:
        """Extracts provisioned resource IDs from terraform show -json with fallback to local state file."""
        tf_bin = self._terraform_binary or "terraform"
        try:
            proc = subprocess.run(
                [tf_bin, "show", "-json"],
                cwd=str(workspace_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0 and proc.stdout:
                data = json.loads(proc.stdout)
                values = data.get("values", {}).get("root_module", {}).get("resources", [])
                resources = []
                for r in values:
                    rtype = r.get("type", "")
                    raddr = r.get("address", "")
                    rvals = r.get("values", {})
                    rid = rvals.get("id")
                    if rid:
                        resources.append({
                            "address": raddr,
                            "type": rtype,
                            "id": rid,
                            "public_ip": rvals.get("public_ip"),
                            "private_ip": rvals.get("private_ip"),
                        })
                if resources:
                    return resources
        except Exception:
            pass

        # Fallback: Read terraform.tfstate directly from workspace if present
        return self._read_state_file_fallback(workspace_dir)

    def _read_state_file_fallback(self, workspace_dir: Path) -> List[Dict[str, Any]]:
        """Fallback direct parser for terraform.tfstate JSON file."""
        state_file = workspace_dir / "terraform.tfstate"
        if not state_file.exists():
            return []
        try:
            content = state_file.read_text(encoding="utf-8")
            data = json.loads(content)
            resources = []
            for r in data.get("resources", []):
                rtype = r.get("type", "")
                rname = r.get("name", "")
                instances = r.get("instances", [])
                for inst in instances:
                    attributes = inst.get("attributes", {})
                    rid = attributes.get("id")
                    if rid:
                        resources.append({
                            "address": f"{rtype}.{rname}",
                            "type": rtype,
                            "id": rid,
                            "public_ip": attributes.get("public_ip"),
                            "private_ip": attributes.get("private_ip"),
                        })
            return resources
        except Exception:
            return []

    def verify_deployment(
        self,
        deployment_id: str,
        spec: NormalizedInfrastructureSpec,
        instance_id: str,
        resource_ids: Optional[List[str]] = None,
        is_mock: bool = False,
        account_id: Optional[str] = None,
        region: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Independently verifies provisioned infrastructure against approved requirements."""
        res_ids = resource_ids or ([instance_id] if instance_id and instance_id != "unknown" else [])
        report = self.verification_service.verify_deployment(
            deployment_id=deployment_id,
            spec=spec,
            resource_ids=res_ids,
            expected_account_id=account_id,
            expected_region=region,
            is_mock=is_mock,
        )

        formatted_checks = [
            {
                "check": f"{c.category}: {c.property_name}",
                "category": c.category,
                "property_name": c.property_name,
                "expected": str(c.expected_value),
                "actual": str(c.actual_value),
                "status": "PASS" if c.result.value == "MATCH" else ("WARN" if c.severity.value == "WARNING" else "FAIL"),
                "result": c.result.value,
                "severity": c.severity.value,
                "explanation": c.explanation or "",
            }
            for c in report.checks
        ]

        return {
            "overall_status": report.verification_status.value,
            "status": report.verification_status.value,
            "checks": formatted_checks,
            "warnings": report.warnings,
            "errors": report.errors,
            "summary": report.summary,
            "verified_at": report.verified_at.isoformat(),
            "report": report.model_dump(mode="json"),
        }

    def backup_state(self, deployment_id: str) -> Optional[Path]:
        """Backs up the Terraform state file before recovery, reconciliation, or apply operations.
        
        CRITICAL SAFETY RULE: Always creates a timestamped copy before modifying state.
        """
        workspace_dir = self.tf_service.get_workspace_dir(deployment_id)
        state_path = workspace_dir / "terraform.tfstate"
        if not state_path.exists():
            return None
        timestamp = int(time.time())
        backup_path = workspace_dir / f"terraform.tfstate.backup_{timestamp}"
        try:
            shutil.copyfile(state_path, backup_path)
            shutil.copyfile(state_path, workspace_dir / "terraform.tfstate.backup_pre_apply")
            return backup_path
        except Exception:
            return None

    def detect_tainted_resources(self, deployment_id: str) -> List[str]:
        """Inspects workspace terraform.tfstate for any tainted resources to prevent replacement."""
        workspace_dir = self.tf_service.get_workspace_dir(deployment_id)
        state_path = workspace_dir / "terraform.tfstate"
        if not state_path.exists():
            return []
        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            tainted = []
            for r in state_data.get("resources", []):
                rtype = r.get("type", "")
                rname = r.get("name", "")
                for inst in r.get("instances", []):
                    if inst.get("status") == "tainted":
                        inst_id = inst.get("attributes", {}).get("id") or "unassigned"
                        tainted.append(f"{rtype}.{rname} ({inst_id})")
            return tainted
        except Exception:
            return []

    @staticmethod
    def extract_iam_permission_error(error_text: str) -> Optional[Dict[str, str]]:
        """Inspects execution stderr/logs for AWS AccessDenied / UnauthorizedOperation errors,
        extracting the specific missing action.
        
        CRITICAL: Never automatically broaden IAM permissions or modify AWS IAM policies.
        """
        if not error_text:
            return None

        # Pattern 1: "... not authorized to perform: <service>:<action> on resource: ..."
        m1 = re.search(r"not authorized to perform:\s*([a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+)", error_text, re.IGNORECASE)
        if m1:
            missing_perm = m1.group(1)
            return {
                "error_type": "AccessDenied",
                "missing_permission": missing_perm,
                "message": f"AWS IAM AccessDenied: Missing required permission '{missing_perm}'. Please update the IAM policy in the AWS console to grant this permission.",
            }

        # Pattern 2: "operation error <SERVICE>: <Action>, ... api error UnauthorizedOperation"
        m2 = re.search(r"operation error ([A-Za-z0-9]+):\s*([A-Za-z0-9]+).*?(?:UnauthorizedOperation|AccessDenied)", error_text, re.DOTALL | re.IGNORECASE)
        if m2:
            svc = m2.group(1).lower()
            act = m2.group(2)
            missing_perm = f"{svc}:{act}"
            return {
                "error_type": "UnauthorizedOperation",
                "missing_permission": missing_perm,
                "message": f"AWS IAM AccessDenied: Missing required permission '{missing_perm}'. Please update the IAM policy in the AWS console to grant this permission.",
            }

        # Pattern 3: General UnauthorizedOperation / AccessDenied with context
        if "UnauthorizedOperation" in error_text or "AccessDenied" in error_text:
            m3 = re.search(r"(?:creating|updating|reading|deleting)\s+([A-Za-z0-9\s]+):.*?(?:UnauthorizedOperation|AccessDenied)", error_text, re.IGNORECASE)
            context = m3.group(1).strip() if m3 else "AWS resource operation"
            return {
                "error_type": "AccessDenied",
                "missing_permission": "unknown",
                "message": f"AWS IAM AccessDenied during {context}: Role is not authorized to perform this operation. Please grant the required IAM permissions in your AWS account and retry.",
            }

        return None
