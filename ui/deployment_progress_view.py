from pathlib import Path
import time
from typing import Optional
import streamlit as st
from config import config
from core.exceptions import ApprovalError, ConfigurationError, TerraformExecutionError
from core.models import ApprovalStatus, DeploymentStatus, NormalizedInfrastructureSpec, PlannerReadinessStatus, PlannerResult
from database.repository import DeploymentRepository
from services.aws_auth_service import aws_auth_service
from services.deployment_service import DeploymentService
from services.terraform_service import TerraformService
from ui.theme import render_top_header


def render_deployment_progress_view():
    """Renders Page 7: Deployment in Progress."""
    render_top_header("Deployment in Progress")

    repo = DeploymentRepository()
    plan_result: Optional[PlannerResult] = st.session_state.get("active_plan_result")
    plan_hash: Optional[str] = st.session_state.get("active_plan_hash")
    deployment_id: Optional[str] = st.session_state.get("active_deployment_id")

    # Database Recovery Fallback if session state is missing
    if not plan_result or not plan_hash:
        record = None
        if deployment_id:
            record = repo.get(deployment_id)
        if not record:
            recents = repo.list_recent(limit=1)
            if recents:
                record = recents[0]
                deployment_id = record.deployment_id
                st.session_state["active_deployment_id"] = deployment_id

        if not record:
            st.info("No approved deployment plan queued. Please review and approve a plan in **Terraform Plan Review**.")
            if st.button("➔ Go to Plan Review"):
                st.session_state["current_page"] = "Terraform Plan Review"
                st.rerun()
            return

        # Record was recovered from database — inspect its state
        if record.approval_status != ApprovalStatus.APPROVED:
            st.warning(
                f"⚠️ **Deployment Not Approved**: Deployment `{record.deployment_id}` has status "
                f"`{record.approval_status.value}`. It cannot be executed without explicit review and approval."
            )
            if st.button("➔ Go to Plan Review", type="primary"):
                st.session_state["current_page"] = "Terraform Plan Review"
                st.rerun()
            return

        if not record.plan_hash or not record.plan_hash.strip():
            st.error(
                f"🛑 **Plan Hash Missing**: Deployment `{record.deployment_id}` was marked approved, but lacks a valid cryptographic plan hash.\n\n"
                "A fresh Terraform plan must be generated and approved before deployment can proceed."
            )
            if st.button("➔ Go to Plan Review", type="primary"):
                st.session_state["current_page"] = "Terraform Plan Review"
                st.rerun()
            return

        # Verify plan artifact exists in workspace
        ws_plan = Path("workspaces") / record.deployment_id / "tfplan"
        if not ws_plan.exists():
            st.error(
                f"🚨 **Plan Artifact Missing**: The binary plan artifact (`tfplan`) for deployment `{record.deployment_id}` "
                "was not found on disk. Execution is blocked for safety."
            )
            if st.button("➔ Go to Plan Review", type="primary"):
                st.session_state["current_page"] = "Terraform Plan Review"
                st.rerun()
            return

        # Verify plan artifact integrity
        try:
            art_bytes = ws_plan.read_bytes()
            calc_art_hash = TerraformService().calculate_plan_artifact_hash(art_bytes)
            # Recover tf_files if present
            ws_dir = Path("workspaces") / record.deployment_id
            tf_files = {p.name: p.read_text(encoding="utf-8") for p in ws_dir.glob("*.tf")}
            calc_full_hash = TerraformService().calculate_plan_hash(tf_files, art_bytes)
            if calc_full_hash != record.plan_hash:
                st.error(
                    f"🚨 **Plan Integrity Mismatch**: Workspace plan files or artifact for deployment `{record.deployment_id}` "
                    "do not match the approved cryptographic hash. Approval has been invalidated."
                )
                repo.update_approval(record.deployment_id, ApprovalStatus.PENDING, plan_hash=None)
                if st.button("➔ Go to Plan Review", type="primary"):
                    st.session_state["current_page"] = "Terraform Plan Review"
                    st.rerun()
                return
        except Exception as e:
            st.error(f"🚨 **Plan Verification Error**: Failed to verify plan integrity: {e}")
            return

        # Reconstruct plan_result from database record normalized_spec or parsed_intent
        if record.normalized_spec:
            try:
                norm_spec = NormalizedInfrastructureSpec(**record.normalized_spec)
                plan_result = PlannerResult(
                    readiness=PlannerReadinessStatus.READY,
                    normalized_spec=norm_spec,
                )
                plan_hash = record.plan_hash
                st.session_state["active_plan_result"] = plan_result
                st.session_state["active_plan_hash"] = plan_hash
            except Exception as e:
                st.error(f"Failed to recover infrastructure specification: {e}")
                return
        elif record.parsed_intent:
            try:
                from core.models import InterpretedInfrastructure
                from services.infrastructure_planner import InfrastructurePlanner
                interp = InterpretedInfrastructure.model_validate(record.parsed_intent)
                planner_res = InfrastructurePlanner().plan(interp)
                norm_spec = planner_res.normalized_spec
                ws_vars = Path("workspaces") / record.deployment_id / "variables.tf"
                if ws_vars.exists() and norm_spec.resources:
                    import re
                    m = re.search(r'variable\s+"ami_id"\s*\{[^}]*default\s*=\s*"([^"]+)"', ws_vars.read_text(encoding="utf-8"))
                    if m and m.group(1).strip():
                        norm_spec.resources[0].ami.ami_id = m.group(1).strip()
                plan_result = PlannerResult(
                    readiness=PlannerReadinessStatus.READY,
                    normalized_spec=norm_spec,
                )
                plan_hash = record.plan_hash
                st.session_state["active_plan_result"] = plan_result
                st.session_state["active_plan_hash"] = plan_hash
                repo.update_normalized_spec(record.deployment_id, norm_spec.model_dump())
            except Exception as e:
                st.error(f"Failed to recover infrastructure specification from intent: {e}")
                return
        else:
            st.error("No normalized infrastructure specification found in deployment record.")
            return

        # If already executed in database, mark as executed and reconstruct deployment result
        if record.status in (DeploymentStatus.COMPLETED, DeploymentStatus.PARTIALLY_DEPLOYED) or record.execution_completed_at:
            st.session_state["deployment_executed"] = True
            if "active_deployment_result" not in st.session_state:
                from services.region_service import get_selected_region
                res_ids = record.aws_resource_ids or []
                inst_id = next((r for r in res_ids if r.startswith("i-")), "N/A")
                sg_id = next((r for r in res_ids if r.startswith("sg-")), "N/A")
                pub_ip = None
                priv_ip = None
                ws_state = Path("workspaces") / record.deployment_id / "terraform.tfstate"
                if ws_state.exists():
                    try:
                        import json
                        st_data = json.loads(ws_state.read_text(encoding="utf-8"))
                        for r in st_data.get("resources", []):
                            rtype = r.get("type")
                            for inst in r.get("instances", []):
                                attrs = inst.get("attributes", {})
                                rid = attrs.get("id")
                                if rid and rid not in res_ids:
                                    res_ids.append(rid)
                                if rtype == "aws_instance":
                                    inst_id = rid or inst_id
                                    pub_ip = attrs.get("public_ip")
                                    priv_ip = attrs.get("private_ip")
                                elif rtype == "aws_security_group":
                                    sg_id = rid or sg_id
                    except Exception:
                        pass
                status_str = "SUCCEEDED" if record.status == DeploymentStatus.COMPLETED else record.status.value.upper()
                st.session_state["active_deployment_result"] = {
                    "deployment_id": record.deployment_id,
                    "status": status_str,
                    "execution_mode": record.execution_mode or "real",
                    "instance_id": inst_id,
                    "security_group_id": sg_id,
                    "public_ip": pub_ip,
                    "private_ip": priv_ip,
                    "resource_ids": res_ids,
                    "region": record.aws_region or get_selected_region(),
                    "account_id": record.aws_account_id or "082229155432",
                    "verification": record.verification_details or {},
                    "logs": record.execution_logs or "",
                    "partial_resource_ids": res_ids if record.status == DeploymentStatus.PARTIALLY_DEPLOYED else [],
                }

    # Secondary safety check: ensure plan_hash is non-empty and valid
    if not plan_hash or not plan_hash.strip():
        st.error(
            "🛑 **Invalid Plan Hash**: Cannot execute deployment with an empty or missing plan hash. "
            "Please return to **Terraform Plan Review** and generate a valid plan."
        )
        if st.button("➔ Go to Plan Review"):
            st.session_state["current_page"] = "Terraform Plan Review"
            st.rerun()
        return

    # Wizard 5-step Progress Bar
    st.markdown(
        """
        <div style="display: flex; justify-content: space-between; margin-bottom: 24px; padding: 12px 20px; background: rgba(30, 41, 59, 0.4); border-radius: 10px;">
            <div style="color: #34d399; font-weight: 600;">✓ 1. Intent</div>
            <div style="color: #34d399; font-weight: 600;">✓ 2. Cost</div>
            <div style="color: #34d399; font-weight: 600;">✓ 3. Plan</div>
            <div style="color: #818cf8; font-weight: 700; text-decoration: underline;">➔ 4. Deploy</div>
            <div style="color: #94a3b8;">5. Verify</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    is_real = config.is_real_deployment_allowed()
    active_identity = aws_auth_service.get_active_identity()

    # Safety Mode Banner
    if not is_real:
        st.info(
            "🛡️ **Mock Mode Active**: Simulating isolated Terraform execution with complete audit persistence. "
            "No real AWS resources will be created or billed."
        )
    else:
        st.warning(
            f"⚡ **REAL AWS DEPLOYMENT ACTIVE**: Executing genuine Terraform apply against AWS Account "
            f"**{active_identity.account_id if active_identity else 'Unknown'}** ({active_identity.region if active_identity else 'Unknown'}). "
            f"Real cloud resources will be provisioned."
        )

    st.subheader("Terraform Deployment Execution")

    # Execution Container
    status_placeholder = st.empty()
    log_container = st.empty()

    if "deployment_executed" not in st.session_state:
        with status_placeholder.container():
            st.info("Initiating deployment orchestrator and verifying cryptographic approval...")

        try:
            with st.spinner("Executing Terraform Apply in workspace... Provisioning cloud resources..."):
                service = DeploymentService()
                dep_result = service.execute_deployment(
                    deployment_id=deployment_id or "dep-auto",
                    spec=plan_result.normalized_spec,
                    approved_plan_hash=plan_hash,
                )
            st.session_state["active_deployment_result"] = dep_result
            st.session_state["deployment_executed"] = True

            status = dep_result.get("status", "")
            if dep_result.get("iam_error"):
                iam_err = dep_result["iam_error"]
                status_placeholder.error(
                    f"🔒 **AWS IAM Permission Required**: `{iam_err.get('missing_permission', 'AccessDenied')}`\n\n"
                    f"{iam_err.get('message')}"
                )
            elif status in ("COMPLETED (REAL)", "COMPLETED (MOCK)", "SUCCEEDED"):
                status_placeholder.success("🎉 Deployment and independent verification completed successfully!")
            elif status == "PARTIALLY_DEPLOYED":
                partial_ids = dep_result.get("partial_resource_ids", [])
                status_placeholder.warning(
                    f"⚠️ **PARTIALLY DEPLOYED**: Apply encountered an error, but {len(partial_ids)} resource(s) were successfully "
                    f"created in AWS ({', '.join(partial_ids)}). State is preserved; no automatic destroy or rollback was performed."
                )
            elif status == "EXECUTION_STATUS_UNKNOWN":
                status_placeholder.info(
                    "❓ **EXECUTION STATUS UNKNOWN**: Process timed out or ended unexpectedly. "
                    "Cloud resources will not be destroyed automatically. Please inspect AWS or verification tab."
                )
            else:
                status_placeholder.error(f"❌ Deployment failed: {dep_result.get('error', 'Execution failure')}")

            if "logs" in dep_result:
                log_container.code(dep_result["logs"], language="text")

        except (ApprovalError, ConfigurationError, TerraformExecutionError) as e:
            status_placeholder.error(f"Execution Error: {str(e)}")
            st.session_state["deployment_error"] = str(e)
            return

    else:
        # Already executed, show stored logs & result
        dep_result = st.session_state.get("active_deployment_result", {})
        status = dep_result.get("status", "")
        if dep_result.get("iam_error"):
            iam_err = dep_result["iam_error"]
            status_placeholder.error(
                f"🔒 **AWS IAM Permission Required**: `{iam_err.get('missing_permission', 'AccessDenied')}`\n\n"
                f"{iam_err.get('message')}"
            )
        elif status in ("COMPLETED (REAL)", "COMPLETED (MOCK)", "SUCCEEDED"):
            status_placeholder.success("🎉 Deployment and independent verification completed successfully!")
        elif status == "PARTIALLY_DEPLOYED":
            partial_ids = dep_result.get("partial_resource_ids", [])
            status_placeholder.warning(
                f"⚠️ **PARTIALLY DEPLOYED**: Apply encountered an error, but {len(partial_ids)} resource(s) were successfully "
                f"created in AWS ({', '.join(partial_ids)}). State is preserved; no automatic destroy or rollback was performed."
            )
        elif status == "EXECUTION_STATUS_UNKNOWN":
            status_placeholder.info(
                "❓ **EXECUTION STATUS UNKNOWN**: Process timed out or ended unexpectedly. "
                "Cloud resources will not be destroyed automatically. Please inspect AWS or verification tab."
            )
        else:
            status_placeholder.error(f"❌ Deployment failed: {dep_result.get('error', 'Execution failure')}")

        if "logs" in dep_result:
            log_container.code(dep_result["logs"], language="text")

    # Action to proceed to Verification
    st.divider()
    bcol1, bcol2 = st.columns([1.5, 3.5])
    with bcol1:
        if st.button("View AWS Verification ➔", type="primary", width="stretch"):
            st.session_state["current_page"] = "Verification"
            st.rerun()
