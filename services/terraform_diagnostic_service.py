"""Terraform AWS Credential Propagation Diagnostic Service.

Verifies that Terraform subprocesses receive the exact authenticated AWS credentials
and session context from Cloud-Chat V2's AWS authentication service.

Strict Safety Boundaries:
1. Purely diagnostic — uses ONLY the aws_caller_identity data source.
2. Contains ZERO managed AWS resources (no resource blocks).
3. NEVER executes 'terraform apply' or 'terraform destroy'.
4. Completely isolated in a temporary directory (no state or deployment corruption).
5. All credentials and tokens are strictly redacted from logs and return structures.
6. Returns clear failures with exact failure stages; never silently falls back to mock mode.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional

from config import config
from core.exceptions import ConfigurationError, TerraformExecutionError
from services.aws_auth_service import AWSAuthService, aws_auth_service
from services.region_service import get_selected_region


class TerraformCredentialDiagnostic:
    """Runs an isolated, read-only Terraform diagnostic to confirm AWS credential propagation."""

    def __init__(
        self,
        auth_service: Optional[AWSAuthService] = None,
        terraform_binary: Optional[str] = None,
    ):
        self.auth_service = auth_service or aws_auth_service
        self._terraform_binary = terraform_binary if terraform_binary is not None else shutil.which("terraform")

    def _locate_provider_cache(self) -> Optional[Path]:
        """Locates cached AWS provider plugins in workspaces to enable fast/offline init."""
        # Check standard workspaces directory for downloaded AWS provider
        workspace_root = config.workspace_root
        if workspace_root.exists():
            for p in workspace_root.glob("*/.terraform/providers"):
                if p.is_dir() and any(p.glob("**/terraform-provider-aws*")):
                    return p
        return None

    def build_diagnostic_tf_content(self, region: str) -> str:
        """Generates HCL for the diagnostic workspace with zero managed resources."""
        return f"""terraform {{
  required_version = ">= 1.5.0"
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }}
  }}
}}

provider "aws" {{
  region = "{region}"
}}

# Purely read-only data source to query STS caller identity
data "aws_caller_identity" "current" {{}}

output "caller_account_id" {{
  description = "The AWS Account ID reported by Terraform"
  value       = data.aws_caller_identity.current.account_id
}}

output "caller_arn" {{
  description = "The assumed-role ARN reported by Terraform"
  value       = data.aws_caller_identity.current.arn
}}

output "caller_user_id" {{
  description = "The caller user ID reported by Terraform"
  value       = data.aws_caller_identity.current.user_id
}}
"""

    def run_diagnostic(
        self,
        profile_name: str = "cloud-chat-deploy",
        region: str = "ap-south-1",
        expected_account_id: str = "082229155432",
        expected_role_substr: str = "CloudChatTerraformDeployRole",
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """Executes the complete diagnostic: Auth -> Init -> Validate -> Plan -> Identity Verification."""
        logs: List[str] = []

        def log(msg: str):
            logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

        log(f"Starting Terraform AWS Credential Propagation Diagnostic for profile '{profile_name}' in '{region}'")

        if not self._terraform_binary:
            err = "Terraform binary not found in system PATH."
            log(f"ERROR: {err}")
            return {
                "success": False,
                "stage": "PRECHECK",
                "error": err,
                "logs": "\n".join(logs),
            }

        # 1. AWS Authentication Resolution via AWSAuthService
        log("Step 1: Authenticating AWS profile via AWSAuthService...")
        try:
            creds = self.auth_service.get_active_credentials()
            identity = self.auth_service.get_active_identity()

            # If not active or different profile, authenticate now
            if not creds or not identity or identity.profile_name != profile_name:
                identity = self.auth_service.authenticate_profile(profile_name, region)
                creds = self.auth_service.get_active_credentials()

            if not identity or not identity.is_valid:
                err = f"Failed to authenticate AWS profile '{profile_name}': Active identity is invalid."
                log(f"ERROR: {err}")
                return {
                    "success": False,
                    "stage": "AUTHENTICATION",
                    "error": err,
                    "logs": "\n".join(logs),
                }

            log(f"Active identity authenticated via STS: Account={identity.account_id}, Region={identity.region}")
            log(f"STS Principal ARN: {identity.arn}")

        except Exception as e:
            err = self.auth_service.redact_secrets(str(e))
            log(f"ERROR during profile authentication: {err}")
            return {
                "success": False,
                "stage": "AUTHENTICATION",
                "error": err,
                "logs": "\n".join(logs),
            }

        # Verify initial STS identity matches expectations before running Terraform
        if expected_account_id and identity.account_id != expected_account_id:
            err = f"STS Account ID mismatch: expected '{expected_account_id}', but authenticated as '{identity.account_id}'."
            log(f"ERROR: {err}")
            return {
                "success": False,
                "stage": "AUTHENTICATION",
                "error": err,
                "logs": "\n".join(logs),
            }

        if expected_role_substr and expected_role_substr not in identity.arn:
            err = f"STS Role mismatch: expected ARN containing '{expected_role_substr}', but got '{identity.arn}'."
            log(f"ERROR: {err}")
            return {
                "success": False,
                "stage": "AUTHENTICATION",
                "error": err,
                "logs": "\n".join(logs),
            }

        # 2. Build Subprocess Environment with In-Memory Frozen Credentials
        log("Step 2: Constructing subprocess environment from active credentials...")
        sub_env = creds.get_subprocess_env()
        sub_env["AWS_DEFAULT_REGION"] = region
        sub_env["AWS_REGION"] = region
        sub_env["AWS_SDK_LOAD_CONFIG"] = "1"
        sub_env["AWS_EC2_METADATA_DISABLED"] = "true"

        # 3. Create Isolated Diagnostic Workspace in Temp Directory
        with tempfile.TemporaryDirectory(prefix="cloudchat_tf_diag_") as temp_dir:
            workspace_dir = Path(temp_dir)
            log(f"Diagnostic temporary workspace created at: {workspace_dir}")

            tf_content = self.build_diagnostic_tf_content(region)
            (workspace_dir / "main.tf").write_text(tf_content, encoding="utf-8")

            # 4. Terraform Init
            log("Step 3: Running 'terraform init'...")
            init_cmd = [self._terraform_binary, "init", "-input=false", "-no-color"]
            plugin_dir = self._locate_provider_cache()
            if plugin_dir:
                init_cmd.append(f"-plugin-dir={plugin_dir.resolve()}")
                log(f"Using provider cache at: {plugin_dir}")

            r_init = self._run_command(init_cmd, workspace_dir, sub_env, timeout, creds)
            log(r_init["stdout"] or "Init completed.")
            if r_init["exit_code"] != 0:
                err = f"Terraform init failed: {r_init['stderr'] or r_init['stdout']}"
                log(f"ERROR: {err}")
                return {
                    "success": False,
                    "stage": "INIT",
                    "error": err,
                    "logs": "\n".join(logs),
                }

            # 5. Terraform Validate
            log("Step 4: Running 'terraform validate'...")
            val_cmd = [self._terraform_binary, "validate", "-json", "-no-color"]
            r_val = self._run_command(val_cmd, workspace_dir, sub_env, timeout, creds)
            val_valid = False
            try:
                val_data = json.loads(r_val["stdout"])
                val_valid = val_data.get("valid", False)
            except Exception:
                val_valid = r_val["exit_code"] == 0

            if not val_valid:
                err = f"Terraform validate failed: {r_val['stderr'] or r_val['stdout']}"
                log(f"ERROR: {err}")
                return {
                    "success": False,
                    "stage": "VALIDATE",
                    "error": err,
                    "logs": "\n".join(logs),
                }
            log("Terraform validation passed.")

            # 6. Terraform Plan (purely read-only planning, no apply or destroy)
            log("Step 5: Running 'terraform plan -out=tfplan'...")
            plan_cmd = [self._terraform_binary, "plan", "-out=tfplan", "-input=false", "-no-color"]
            r_plan = self._run_command(plan_cmd, workspace_dir, sub_env, timeout, creds)
            log(r_plan["stdout"])
            if r_plan["exit_code"] != 0:
                err = f"Terraform plan failed (exit code {r_plan['exit_code']}): {r_plan['stderr'] or r_plan['stdout']}"
                log(f"ERROR: {err}")
                return {
                    "success": False,
                    "stage": "PLAN",
                    "error": err,
                    "logs": "\n".join(logs),
                }
            log("Terraform plan artifact generated successfully (tfplan).")

            # 7. Parse Plan via 'terraform show -json tfplan'
            log("Step 6: Parsing plan artifact via 'terraform show -json tfplan'...")
            show_cmd = [self._terraform_binary, "show", "-json", "tfplan"]
            r_show = self._run_command(show_cmd, workspace_dir, sub_env, timeout, creds)
            if r_show["exit_code"] != 0:
                err = f"Terraform show failed: {r_show['stderr'] or r_show['stdout']}"
                log(f"ERROR: {err}")
                return {
                    "success": False,
                    "stage": "SHOW",
                    "error": err,
                    "logs": "\n".join(logs),
                }

            tf_account_id = None
            tf_arn = None
            tf_user_id = None

            try:
                plan_json = json.loads(r_show["stdout"])
                # Extract outputs from planned_values
                planned_outputs = plan_json.get("planned_values", {}).get("outputs", {})
                if "caller_account_id" in planned_outputs:
                    tf_account_id = planned_outputs["caller_account_id"].get("value")
                if "caller_arn" in planned_outputs:
                    tf_arn = planned_outputs["caller_arn"].get("value")
                if "caller_user_id" in planned_outputs:
                    tf_user_id = planned_outputs["caller_user_id"].get("value")

                # Fallback to output_changes if present
                output_changes = plan_json.get("output_changes", {})
                if not tf_account_id and "caller_account_id" in output_changes:
                    tf_account_id = output_changes["caller_account_id"].get("after")
                if not tf_arn and "caller_arn" in output_changes:
                    tf_arn = output_changes["caller_arn"].get("after")
                if not tf_user_id and "caller_user_id" in output_changes:
                    tf_user_id = output_changes["caller_user_id"].get("after")

            except Exception as e:
                log(f"Warning: Failed to parse plan JSON: {e}")

            # Fallback regex parsing on stdout if JSON outputs are deferred
            if not tf_account_id:
                m_acc = re.search(r'caller_account_id\s*=\s*"?([0-9]{12})"?', r_plan["stdout"])
                if m_acc:
                    tf_account_id = m_acc.group(1)
            if not tf_arn:
                m_arn = re.search(r'caller_arn\s*=\s*"?([^\s"\n]+)"?', r_plan["stdout"])
                if m_arn:
                    tf_arn = m_arn.group(1)
            if not tf_user_id:
                m_uid = re.search(r'caller_user_id\s*=\s*"?([^\s"\n]+)"?', r_plan["stdout"])
                if m_uid:
                    tf_user_id = m_uid.group(1)

            log(f"Terraform Reported Identity:")
            log(f"  Account ID: {tf_account_id}")
            log(f"  ARN:        {tf_arn}")
            log(f"  User ID:    {tf_user_id}")

            # 8. Verify Reported Identity Against Expected Requirements
            if not tf_account_id or tf_account_id != expected_account_id:
                err = f"Terraform identity mismatch: expected Account '{expected_account_id}', but Terraform reported '{tf_account_id}'."
                log(f"ERROR: {err}")
                return {
                    "success": False,
                    "stage": "VERIFY",
                    "error": err,
                    "terraform_account_id": tf_account_id,
                    "terraform_arn": tf_arn,
                    "logs": "\n".join(logs),
                }

            if not tf_arn or expected_role_substr not in tf_arn:
                err = f"Terraform role mismatch: expected ARN containing '{expected_role_substr}', but Terraform reported '{tf_arn}'."
                log(f"ERROR: {err}")
                return {
                    "success": False,
                    "stage": "VERIFY",
                    "error": err,
                    "terraform_account_id": tf_account_id,
                    "terraform_arn": tf_arn,
                    "logs": "\n".join(logs),
                }

            log("VERIFICATION SUCCESSFUL: Terraform independently verified the expected AWS identity!")

            return {
                "success": True,
                "stage": "COMPLETED",
                "terraform_account_id": tf_account_id,
                "terraform_arn": tf_arn,
                "terraform_user_id": tf_user_id,
                "target_region": region,
                "logs": "\n".join(logs),
            }

    def _run_command(
        self,
        cmd: List[str],
        cwd: Path,
        env: Dict[str, str],
        timeout: int,
        creds: Any,
    ) -> Dict[str, Any]:
        """Runs a subprocess command with safety enforcement and secret redaction."""
        cmd_str = " ".join(cmd).lower()
        if "apply" in cmd_str or "destroy" in cmd_str:
            raise TerraformExecutionError(f"Command '{cmd_str}' is strictly forbidden by diagnostic safety guardrails.")

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            stdout = creds.redact_secrets(proc.stdout) if creds else proc.stdout
            stderr = creds.redact_secrets(proc.stderr) if creds else proc.stderr
            return {
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        except subprocess.TimeoutExpired:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command '{' '.join(cmd)}' timed out after {timeout} seconds.",
            }
        except Exception as e:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": creds.redact_secrets(str(e)) if creds else str(e),
            }


# Singleton instance
terraform_credential_diagnostic = TerraformCredentialDiagnostic()
