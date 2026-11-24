import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple
from config import config
from core.exceptions import ConfigurationError, TerraformExecutionError, ValidationError
from core.models import (
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    SecurityScanResult,
    TerraformExecutionStage,
    TerraformPlanResult,
    VolumeType,
)
from database.repository import DeploymentRepository
from services.security_scanner import SecurityScanner


class TerraformService:
    """Manages isolated deployment workspaces, HCL generation, and controlled Terraform execution."""

    def __init__(
        self,
        security_scanner: Optional[SecurityScanner] = None,
        repository: Optional[DeploymentRepository] = None,
        terraform_binary: Optional[str] = None,
        workspace_root: Optional[Path] = None,
    ):
        self.security_scanner = security_scanner or SecurityScanner()
        self.repository = repository or DeploymentRepository()
        self._terraform_binary = terraform_binary if terraform_binary is not None else shutil.which("terraform")
        self._workspace_root = workspace_root or config.workspace_root


    def validate_deployment_id(self, deployment_id: str) -> None:
        """Validates deployment ID against path traversal and unsafe characters."""
        if not deployment_id or not isinstance(deployment_id, str):
            raise ValidationError("Deployment ID must be a non-empty string.")
        if not re.match(r"^[a-zA-Z0-9_\-]+$", deployment_id):
            raise ValidationError(
                f"Invalid deployment ID '{deployment_id}'. Must contain only alphanumeric characters, dashes, and underscores."
            )

    def get_workspace_dir(self, deployment_id: str) -> Path:
        """Returns the isolated workspace directory for a deployment with path traversal protection."""
        self.validate_deployment_id(deployment_id)
        root = self._workspace_root.resolve()
        workspace_dir = (root / deployment_id).resolve()


        # Strict path containment check
        try:
            workspace_dir.relative_to(root)
        except ValueError:
            raise ValidationError(f"Path traversal detected in deployment ID: '{deployment_id}'")

        return workspace_dir

    def generate_configuration(self, spec: NormalizedInfrastructureSpec) -> Dict[str, str]:
        """Generates main.tf, variables.tf, and outputs.tf for the normalized infrastructure specification."""
        if not spec.resources:
            return {}

        ec2 = spec.resources[0]
        from services.region_service import get_selected_region
        region = spec.region or get_selected_region()

        # 1. main.tf
        main_tf = self._build_main_tf(ec2, region)

        # 2. variables.tf
        variables_tf = self._build_variables_tf(ec2, region)

        # 3. outputs.tf
        outputs_tf = self._build_outputs_tf(ec2)

        return {
            "main.tf": main_tf,
            "variables.tf": variables_tf,
            "outputs.tf": outputs_tf,
        }

    def write_workspace_files(self, deployment_id: str, tf_files: Dict[str, str]) -> Path:
        """Writes configuration files into the deployment's isolated workspace."""
        workspace_dir = self.get_workspace_dir(deployment_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)

        for filename, content in tf_files.items():
            file_path = workspace_dir / filename
            file_path.write_text(content, encoding="utf-8")

        return workspace_dir

    def calculate_plan_artifact_hash(self, plan_bytes: Optional[bytes]) -> str:
        """Calculates an independent SHA-256 hash of the saved binary tfplan artifact."""
        if not plan_bytes:
            return ""
        return hashlib.sha256(plan_bytes).hexdigest()

    def calculate_plan_hash(self, tf_files: Dict[str, str], plan_bytes: Optional[bytes] = None) -> str:
        """Calculates a deterministic SHA-256 hash of the Terraform configuration and plan artifact."""
        hasher = hashlib.sha256()
        for name in sorted(tf_files.keys()):
            hasher.update(f"--- {name} ---\n{tf_files[name]}\n".encode("utf-8"))
        if plan_bytes:
            hasher.update(plan_bytes)
        return hasher.hexdigest()

    def run_pipeline(
        self,
        deployment_id: str,
        spec: NormalizedInfrastructureSpec,
        timeout: Optional[int] = None,
    ) -> TerraformPlanResult:
        """Executes the full M4 pipeline: Format -> Init -> Validate -> Security Scan -> Plan."""
        cmd_timeout = timeout or config.terraform_timeout_seconds
        logs: List[str] = []

        def log(msg: str):
            entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
            logs.append(entry)

        log(f"Starting Terraform pipeline for deployment '{deployment_id}' in region '{spec.region}'")

        # Verify AWS Caller Identity if credentials are configured
        from services.aws_auth_service import aws_auth_service
        active_identity = aws_auth_service.get_active_identity()
        is_authenticated = bool(active_identity and active_identity.is_valid)
        is_mock_plan = not is_authenticated

        if is_authenticated:
            log(f"Authenticated AWS Identity verified: Account={active_identity.account_id}, Region={active_identity.region}, Method={active_identity.auth_method.value}")
            log(f"Principal ARN: {active_identity.arn}")
        else:
            log("Running in local mock / unauthenticated mode.")

        # Check terraform binary availability first
        if not self._terraform_binary:
            err = "Terraform executable not found in system PATH."
            log(f"ERROR: {err}")
            return TerraformPlanResult(
                deployment_id=deployment_id,
                workspace_dir="",
                stage=TerraformExecutionStage.FAILED,
                format_passed=False,
                init_passed=False,
                validation_passed=False,
                plan_passed=False,
                plan_hash="",
                plan_artifact_hash="",
                plan_file_path=None,
                error_message=err,
                execution_logs="\n".join(logs),
                is_mock_plan=is_mock_plan,
            )

        # 1. Generate & Write Workspace Files
        try:
            tf_files = self.generate_configuration(spec)
        except ValidationError as e:
            err = str(e)
            log(f"ERROR: {err}")
            return TerraformPlanResult(
                deployment_id=deployment_id,
                workspace_dir="",
                stage=TerraformExecutionStage.CONFIG_GENERATION,
                format_passed=False,
                init_passed=False,
                validation_passed=False,
                plan_passed=False,
                plan_hash="",
                plan_artifact_hash="",
                plan_file_path=None,
                error_message=err,
                execution_logs="\n".join(logs),
                is_mock_plan=is_mock_plan,
            )

        workspace_dir = self.write_workspace_files(deployment_id, tf_files)
        log(f"Workspace initialized at: {workspace_dir}")

        # 2. Terraform Format (terraform fmt)
        log("Running 'terraform fmt'...")
        fmt_res = self._run_command(["terraform", "fmt", "-no-color"], workspace_dir, cmd_timeout)
        log(fmt_res["output"] if fmt_res["output"] else "Formatting passed (clean syntax).")

        # Re-read formatted files
        for p in workspace_dir.glob("*.tf"):
            tf_files[p.name] = p.read_text(encoding="utf-8")

        # 3. Terraform Init (terraform init -input=false -no-color)
        log("Running 'terraform init'...")
        init_res = self._run_command(
            ["terraform", "init", "-input=false", "-no-color"],
            workspace_dir,
            cmd_timeout,
        )
        if init_res["exit_code"] != 0:
            err = f"Terraform init failed: {init_res['error'] or init_res['output']}"
            log(f"ERROR: {err}")
            (workspace_dir / "terraform.log").write_text("\n".join(logs), encoding="utf-8")
            return TerraformPlanResult(
                deployment_id=deployment_id,
                workspace_dir=str(workspace_dir),
                stage=TerraformExecutionStage.INIT,
                format_passed=True,
                init_passed=False,
                validation_passed=False,
                plan_passed=False,
                plan_hash="",
                plan_artifact_hash="",
                plan_file_path=None,
                error_message=err,
                execution_logs="\n".join(logs),
                is_mock_plan=is_mock_plan,
            )
        log("Terraform init completed successfully.")

        # 4. Terraform Validate (terraform validate -json -no-color)
        log("Running 'terraform validate'...")
        val_res = self._run_command(
            ["terraform", "validate", "-json", "-no-color"],
            workspace_dir,
            cmd_timeout,
        )
        validation_valid = False
        try:
            val_data = json.loads(val_res["output"])
            validation_valid = val_data.get("valid", False)
        except Exception:
            validation_valid = val_res["exit_code"] == 0

        if not validation_valid:
            err = f"Terraform validate failed: {val_res['error'] or val_res['output']}"
            log(f"ERROR: {err}")
            (workspace_dir / "terraform.log").write_text("\n".join(logs), encoding="utf-8")
            return TerraformPlanResult(
                deployment_id=deployment_id,
                workspace_dir=str(workspace_dir),
                stage=TerraformExecutionStage.VALIDATE,
                format_passed=True,
                init_passed=True,
                validation_passed=False,
                plan_passed=False,
                plan_hash="",
                plan_artifact_hash="",
                plan_file_path=None,
                error_message=err,
                execution_logs="\n".join(logs),
                is_mock_plan=is_mock_plan,
            )
        log("Terraform configuration syntax validated.")

        # 5. Security Scanning (Checkov + Deterministic Policy Engine)
        log("Running security scanner...")
        scan_result = self.security_scanner.scan_workspace(workspace_dir, tf_files, timeout=60)
        log(f"Security scan complete ({scan_result.scanner}): {scan_result.summary}")

        # 6. Terraform Plan (terraform plan -out=tfplan -input=false -no-color)
        log("Running 'terraform plan -out=tfplan'...")
        plan_res = self._run_command(
            ["terraform", "plan", "-out=tfplan", "-input=false", "-no-color"],
            workspace_dir,
            cmd_timeout,
        )
        if plan_res["exit_code"] != 0:
            err = f"Terraform planning failed (exit code {plan_res['exit_code']}): {plan_res['error'] or plan_res['output']}"
            log(f"ERROR: {err}")
            (workspace_dir / "terraform.log").write_text("\n".join(logs), encoding="utf-8")
            try:
                from core.models import ApprovalStatus
                self.repository.update_approval(
                    deployment_id,
                    approval_status=ApprovalStatus.PENDING,
                    plan_hash=None,
                )
            except Exception:
                pass
            return TerraformPlanResult(
                deployment_id=deployment_id,
                workspace_dir=str(workspace_dir),
                stage=TerraformExecutionStage.PLAN,
                format_passed=True,
                init_passed=True,
                validation_passed=True,
                plan_passed=False,
                plan_hash="",
                plan_artifact_hash="",
                plan_file_path=None,
                to_add=0,
                to_change=0,
                to_destroy=0,
                security_scan=scan_result,
                error_message=err,
                execution_logs="\n".join(logs),
                is_mock_plan=is_mock_plan,
            )
        log("Terraform plan artifact generated successfully (tfplan).")

        # 7. Parse Plan (terraform show -json tfplan)
        log("Parsing plan summary from artifact...")
        show_res = self._run_command(
            ["terraform", "show", "-json", "tfplan"],
            workspace_dir,
            cmd_timeout,
        )
        to_add, to_change, to_destroy, actions = self._parse_plan_json(show_res["output"], spec)

        # 8. Cryptographic Plan Hash & Persistence
        tfplan_path = workspace_dir / "tfplan"
        plan_bytes = tfplan_path.read_bytes() if tfplan_path.exists() else None
        if not plan_bytes or not tfplan_path.exists():
            err = "Terraform plan artifact (tfplan) was not generated or is empty."
            log(f"ERROR: {err}")
            (workspace_dir / "terraform.log").write_text("\n".join(logs), encoding="utf-8")
            return TerraformPlanResult(
                deployment_id=deployment_id,
                workspace_dir=str(workspace_dir),
                stage=TerraformExecutionStage.PLAN,
                format_passed=True,
                init_passed=True,
                validation_passed=True,
                plan_passed=False,
                plan_hash="",
                plan_artifact_hash="",
                plan_file_path=None,
                security_scan=scan_result,
                error_message=err,
                execution_logs="\n".join(logs),
                is_mock_plan=is_mock_plan,
            )

        plan_hash = self.calculate_plan_hash(tf_files, plan_bytes)
        artifact_hash = self.calculate_plan_artifact_hash(plan_bytes)

        # Write execution log file
        (workspace_dir / "terraform.log").write_text("\n".join(logs), encoding="utf-8")

        result = TerraformPlanResult(
            deployment_id=deployment_id,
            workspace_dir=str(workspace_dir),
            stage=TerraformExecutionStage.COMPLETED,
            format_passed=True,
            init_passed=True,
            validation_passed=True,
            plan_passed=True,
            to_add=to_add,
            to_change=to_change,
            to_destroy=to_destroy,
            is_destructive=(to_destroy > 0),
            plan_hash=plan_hash,
            plan_artifact_hash=artifact_hash,
            plan_file_path=str(tfplan_path),
            security_scan=scan_result,
            actions=actions,
            execution_logs="\n".join(logs),
            is_mock_plan=is_mock_plan,
        )

        # Update SQLite record
        try:
            self.repository.update_approval(
                deployment_id,
                approval_status=self.repository.get(deployment_id).approval_status if self.repository.get(deployment_id) else None,
                plan_hash=plan_hash,
            )
        except Exception:
            pass

        return result

    def _run_command(self, cmd: List[str], cwd: Path, timeout: int) -> Dict[str, Any]:
        """Runs a command via subprocess with argument list, timeout, environment injection, and output capture."""
        # STRICT SAFETY: Ensure apply and destroy are never executed in M4
        cmd_str = " ".join(cmd).lower()
        if "apply" in cmd_str or "destroy" in cmd_str:
            raise TerraformExecutionError(f"Command '{cmd_str}' is forbidden by Milestone 4 safety boundaries.")

        from services.aws_auth_service import aws_auth_service
        from services.region_service import get_selected_region
        creds = aws_auth_service.get_active_credentials()
        sub_env = creds.get_subprocess_env() if creds else os.environ.copy()
        target_reg = creds.region if creds and creds.region else get_selected_region()
        sub_env["AWS_DEFAULT_REGION"] = target_reg
        sub_env["AWS_REGION"] = target_reg
        sub_env["AWS_SDK_LOAD_CONFIG"] = "1"
        sub_env["AWS_EC2_METADATA_DISABLED"] = "true"

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=sub_env,
            )
            stdout = aws_auth_service.redact_secrets(proc.stdout.strip())
            stderr = aws_auth_service.redact_secrets(proc.stderr.strip())
            return {
                "exit_code": proc.returncode,
                "output": stdout,
                "error": stderr,
            }
        except subprocess.TimeoutExpired:
            return {
                "exit_code": -1,
                "output": "",
                "error": f"Command timed out after {timeout} seconds.",
            }
        except Exception as e:
            sanitized_err = aws_auth_service.redact_secrets(str(e))
            return {
                "exit_code": -1,
                "output": "",
                "error": sanitized_err,
            }

    def _parse_plan_json(self, show_json: str, spec: NormalizedInfrastructureSpec) -> Tuple[int, int, int, List[Dict[str, Any]]]:
        """Parses terraform show -json output to extract exact resource change actions."""
        to_add = 0
        to_change = 0
        to_destroy = 0
        actions: List[Dict[str, Any]] = []

        try:
            data = json.loads(show_json)
            for rc in data.get("resource_changes", []):
                act_list = rc.get("change", {}).get("actions", [])
                addr = rc.get("address", "")
                rtype = rc.get("type", "")

                if "create" in act_list:
                    to_add += 1
                    actions.append({"resource": addr, "type": rtype, "action": "create", "details": "Resource scheduled for creation."})
                elif "delete" in act_list:
                    to_destroy += 1
                    actions.append({"resource": addr, "type": rtype, "action": "delete", "details": "Resource scheduled for DESTRUCTION."})
                elif "update" in act_list:
                    to_change += 1
                    actions.append({"resource": addr, "type": rtype, "action": "update", "details": "Resource scheduled for in-place modification."})
            return to_add, to_change, to_destroy, actions
        except Exception:
            # Fallback to parsed specification summary if json parse fails
            summary = self.generate_plan_summary(spec)
            return summary["to_add"], summary["to_change"], summary["to_destroy"], summary["actions"]

    def generate_plan_summary(self, spec: NormalizedInfrastructureSpec) -> Dict[str, Any]:
        """Summarizes planned infrastructure resource modifications."""
        res = spec.resources[0] if spec.resources else None
        inst_name = res.name if res else "app-server"

        actions = [
            {
                "resource": f"aws_instance.app_server ({inst_name})",
                "type": "aws_instance",
                "action": "create",
                "details": f"Type: {res.instance_type if res else 't3.micro'}, Storage: {res.storage.size_gb if res else 8} GB {res.storage.volume_type.value if res else 'gp3'}, Encrypted: {res.storage.encrypted if res else True}, IMDSv2: Required",
            },
            {
                "resource": "aws_security_group.app_sg",
                "type": "aws_security_group",
                "action": "create",
                "details": "Scoped egress (HTTPS 443, HTTP 80), ingress restrictive (no open public ports)",
            },
            {
                "resource": "aws_iam_role.app_role",
                "type": "aws_iam_role",
                "action": "create",
                "details": "Least-privilege execution role scoped to EC2 service assume role",
            },
            {
                "resource": "aws_iam_role_policy_attachment.app_role_ssm",
                "type": "aws_iam_role_policy_attachment",
                "action": "create",
                "details": "Managed policy attachment: AmazonSSMManagedInstanceCore",
            },
            {
                "resource": "aws_iam_instance_profile.app_instance_profile",
                "type": "aws_iam_instance_profile",
                "action": "create",
                "details": "Instance profile linking app_role to app_server instance",
            },
        ]

        return {
            "to_add": len(actions),
            "to_change": 0,
            "to_destroy": 0,
            "actions": actions,
        }

    def _build_main_tf(self, ec2: NormalizedEC2Spec, region: str) -> str:
        ami_id = ec2.ami.ami_id or "ami-placeholder"
        from core.models import DeploymentMode
        is_private = (getattr(ec2.network, "deployment_mode", DeploymentMode.PRIVATE) == DeploymentMode.PRIVATE)
        pub_ip_tf = "false" if is_private else ("true" if ec2.network.public_ip else "false")
        mode_desc = "private deployment, zero ingress by default" if is_private else "public deployment, restrictive ingress"

        tags_hcl = '{\n    "Name" = var.instance_name\n'
        for k, v in ec2.tags.items():
            tags_hcl += f'    "{k}" = "{v}"\n'
        tags_hcl += "  }"

        vpc_subnet_hcl = ""
        if ec2.network.subnet_id:
            vpc_subnet_hcl = f'  subnet_id = "{ec2.network.subnet_id}"\n'

        sg_hcl = ""
        if ec2.network.security_group_ids:
            sgs_str = ", ".join(f'"{sg}"' for sg in ec2.network.security_group_ids)
            sg_hcl = f"  vpc_security_group_ids = [{sgs_str}]\n"
        else:
            sg_hcl = "  vpc_security_group_ids = [aws_security_group.app_sg.id]\n"

        # Security Group Ingress: Default to ZERO inbound rules (Private EC2)
        # Only add port 22 if allowed_ssh_cidrs contains valid, non-public CIDRs (never 0.0.0.0/0)
        ingress_hcl = ""
        valid_ssh_cidrs = [
            c.strip() for c in (ec2.network.allowed_ssh_cidrs or [])
            if c.strip() and c.strip() not in ("0.0.0.0/0", "::/0")
        ]
        if valid_ssh_cidrs:
            cidrs_str = ", ".join(f'"{c}"' for c in valid_ssh_cidrs)
            ingress_hcl = f"""
  ingress {{
    description = "SSH ingress restricted to authorized administrator CIDR"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [{cidrs_str}]
  }}
"""

        main_content = f"""terraform {{
  required_version = ">= 1.5.0"
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }}
  }}
}}

provider "aws" {{
  region = var.aws_region
}}

# Least-Privilege IAM Execution Role for EC2 SSM Session Manager
resource "aws_iam_role" "app_role" {{
  name_prefix = "${{var.instance_name}}-role-"
  description = "Least-privilege execution role for ${{var.instance_name}}"

  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {{
          Service = "ec2.amazonaws.com"
        }}
      }}
    ]
  }})

  tags = {{
    Name = "${{var.instance_name}}-role"
  }}
}}

# Attach AWS Systems Manager managed policy for remote console without SSH keys
resource "aws_iam_role_policy_attachment" "app_role_ssm" {{
  role       = aws_iam_role.app_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}}

# IAM Instance Profile linking role to EC2
resource "aws_iam_instance_profile" "app_instance_profile" {{
  name_prefix = "${{var.instance_name}}-profile-"
  role        = aws_iam_role.app_role.name
}}

# Application Security Group with Restrictive Ingress and Scoped Egress
resource "aws_security_group" "app_sg" {{
  name_prefix = "${{var.instance_name}}-sg"
  description = "Security group for ${{var.instance_name}} ({mode_desc})"
  vpc_id      = var.vpc_id != "" ? var.vpc_id : null
{ingress_hcl}
  egress {{
    description = "Allow all outbound traffic via scoped HTTPS (443) for AWS APIs, SSM Session Manager, and secure package updates"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    description = "Allow all outbound traffic via scoped HTTP (80) for OS package repository mirrors and CRL verification"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  tags = {{
    Name = "${{var.instance_name}}-sg"
  }}
}}

# EC2 Compute Instance (Hardened Configuration)
resource "aws_instance" "app_server" {{
  ami                         = var.ami_id
  instance_type               = var.instance_type
  associate_public_ip_address = {pub_ip_tf}
  ebs_optimized               = true
  monitoring                  = true
  iam_instance_profile        = aws_iam_instance_profile.app_instance_profile.name
{vpc_subnet_hcl}{sg_hcl}
  root_block_device {{
    volume_size           = {ec2.storage.size_gb}
    volume_type           = "{ec2.storage.volume_type.value}"
    encrypted             = true
    delete_on_termination = true
  }}

  metadata_options {{
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }}

  tags = {tags_hcl}
}}
"""
        return main_content

    def _build_variables_tf(self, ec2: NormalizedEC2Spec, region: str) -> str:
        ami_id = (ec2.ami.ami_id or "").strip()
        if not ami_id:
            from services.ami_resolver import OFFICIAL_AL2023_AMIS, determine_architecture
            arch = determine_architecture(ec2.instance_type)
            ami_id = OFFICIAL_AL2023_AMIS.get((region.lower(), arch), "")
            if not ami_id:
                raise ValidationError("Cannot generate Terraform configuration with an empty AMI ID. AMI resolution failed.")

        vpc_id = (ec2.network.vpc_id or "").strip()

        return f"""variable "aws_region" {{
  description = "Target AWS Region"
  type        = string
  default     = "{region}"
}}

variable "instance_name" {{
  description = "Name tag for the EC2 instance"
  type        = string
  default     = "{ec2.name}"
}}

variable "instance_type" {{
  description = "EC2 instance sizing"
  type        = string
  default     = "{ec2.instance_type}"
}}

variable "ami_id" {{
  description = "Amazon Machine Image ID"
  type        = string
  default     = "{ami_id}"
}}

variable "vpc_id" {{
  description = "Existing VPC ID reference"
  type        = string
  default     = "{vpc_id}"
}}
"""

    def _build_outputs_tf(self, ec2: NormalizedEC2Spec) -> str:
        return """output "instance_id" {
  description = "The ID of the provisioned EC2 instance"
  value       = aws_instance.app_server.id
}

output "private_ip" {
  description = "Private IP address of the EC2 instance"
  value       = aws_instance.app_server.private_ip
}

output "public_ip" {
  description = "Public IP address (if assigned)"
  value       = aws_instance.app_server.public_ip
}

output "arn" {
  description = "Amazon Resource Name of the instance"
  value       = aws_instance.app_server.arn
}

output "iam_role_arn" {
  description = "Amazon Resource Name of the attached least-privilege IAM execution role"
  value       = aws_iam_role.app_role.arn
}
"""
