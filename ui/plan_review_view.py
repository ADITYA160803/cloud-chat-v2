from pathlib import Path
import re
from typing import Dict, List, Optional
import streamlit as st
from config import config
from core.models import ApprovalStatus, PlannerReadinessStatus, PlannerResult, TerraformPlanResult
from database.repository import DeploymentRepository
from services.aws_auth_service import aws_auth_service
from services.terraform_service import TerraformService
from ui.theme import render_top_header


def render_plan_review_view():
    """Renders Page 6: Terraform Plan Review."""
    render_top_header("Terraform Plan Review")

    plan_result: Optional[PlannerResult] = st.session_state.get("active_plan_result")
    tf_files: Optional[Dict[str, str]] = st.session_state.get("active_tf_files")
    pipeline_result: Optional[TerraformPlanResult] = st.session_state.get("active_tf_plan_result")
    deployment_id = st.session_state.get("active_deployment_id")

    if not plan_result or not tf_files:
        st.info("No active Terraform plan found. Please generate configuration from **Terraform Configuration** first.")
        if st.button("➔ Go to Terraform Configuration"):
            st.session_state["current_page"] = "Terraform Configuration"
            st.rerun()
        return

    # Use pipeline execution result if available, otherwise calculate from service
    tf_service = TerraformService()
    if pipeline_result:
        to_add = pipeline_result.to_add
        to_change = pipeline_result.to_change
        to_destroy = pipeline_result.to_destroy
        actions = pipeline_result.actions
        plan_hash = pipeline_result.plan_hash
        security_scan = pipeline_result.security_scan
        is_destructive = pipeline_result.is_destructive
        summary = {
            "to_add": to_add,
            "to_change": to_change,
            "to_destroy": to_destroy,
            "actions": actions,
        }
    else:
        summary = tf_service.generate_plan_summary(plan_result.normalized_spec)
        to_add = summary["to_add"]
        to_change = summary["to_change"]
        to_destroy = summary["to_destroy"]
        actions = summary["actions"]
        plan_hash = tf_service.calculate_plan_hash(tf_files)
        security_scan = None
        is_destructive = False

    st.session_state["active_plan_hash"] = plan_hash

    # Resolve resource specifications early for consistent subnet and architecture display
    res_spec = plan_result.normalized_spec.resources[0] if (plan_result and plan_result.normalized_spec and plan_result.normalized_spec.resources) else None
    from core.models import DeploymentMode
    dep_mode_val = getattr(res_spec.network, "deployment_mode", DeploymentMode.PRIVATE).value if res_spec else "PRIVATE"
    pub_ip_val = "ENABLED (Auto-assigned Public IPv4)" if (res_spec and res_spec.network.public_ip) else "DISABLED (Private)"
    sub_val = (res_spec.network.subnet_id or "Unassigned") if res_spec else "N/A"

    st.markdown(
        "Carefully review the proposed infrastructure modifications and security findings before granting deployment approval. "
        "Any subsequent code alteration invalidates approval."
    )

    # Destructive Action Warning Banner
    if is_destructive or to_destroy > 0:
        st.error(
            f"🚨 **CRITICAL DESTRUCTIVE ACTION WARNING**: This plan contains **{to_destroy}** resource deletion action(s)! "
            "Existing cloud data or instances may be permanently destroyed upon apply."
        )

    # 1. Validation & Pipeline Health Badges (Dynamic)
    fmt_ok = pipeline_result.format_passed if pipeline_result else False
    init_ok = pipeline_result.init_passed if pipeline_result else False
    val_ok = pipeline_result.validation_passed if pipeline_result else False
    plan_ok = pipeline_result.plan_passed if pipeline_result else False

    vcol1, vcol2, vcol3, vcol4 = st.columns(4)
    with vcol1:
        fmt_text = "✓ Clean (fmt passed)" if fmt_ok else ("✗ Failed" if pipeline_result else "⚪ Not Run")
        fmt_color = "#34d399" if fmt_ok else ("#f87171" if pipeline_result else "#94a3b8")
        st.markdown(
            f"""
            <div class="cloud-card" style="padding: 14px 18px; margin-bottom: 12px;">
                <div style="font-size: 12px; color: #94a3b8;">Syntax Formatting</div>
                <div style="color: {fmt_color}; font-weight: 700; font-size: 15px;">{fmt_text}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with vcol2:
        init_text = "✓ Initialized (init passed)" if init_ok else ("✗ Failed" if pipeline_result else "⚪ Not Run")
        init_color = "#34d399" if init_ok else ("#f87171" if pipeline_result else "#94a3b8")
        st.markdown(
            f"""
            <div class="cloud-card" style="padding: 14px 18px; margin-bottom: 12px;">
                <div style="font-size: 12px; color: #94a3b8;">Provider Initialization</div>
                <div style="color: {init_color}; font-weight: 700; font-size: 15px;">{init_text}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with vcol3:
        val_text = "✓ Valid (validate passed)" if val_ok else ("✗ Failed" if pipeline_result else "⚪ Not Run")
        val_color = "#34d399" if val_ok else ("#f87171" if pipeline_result else "#94a3b8")
        st.markdown(
            f"""
            <div class="cloud-card" style="padding: 14px 18px; margin-bottom: 12px;">
                <div style="font-size: 12px; color: #94a3b8;">Syntax Validation</div>
                <div style="color: {val_color}; font-weight: 700; font-size: 15px;">{val_text}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with vcol4:
        plan_text = "✓ Plan Generated" if plan_ok else ("✗ Plan Failed" if pipeline_result else "⚪ Not Run")
        plan_color = "#34d399" if plan_ok else ("#f87171" if pipeline_result else "#94a3b8")
        st.markdown(
            f"""
            <div class="cloud-card" style="padding: 14px 18px; margin-bottom: 12px;">
                <div style="font-size: 12px; color: #94a3b8;">Terraform Plan</div>
                <div style="color: {plan_color}; font-weight: 700; font-size: 15px;">{plan_text}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Prominent Error Display if Planning Failed
    if pipeline_result and not pipeline_result.plan_passed:
        st.error(
            f"🚨 **Terraform Planning Failed**: {pipeline_result.error_message or 'Terraform encountered an error while generating this plan.'}\n\n"
            "Execution logs are captured below. **Deployment approval is disabled** until a valid Terraform plan artifact is successfully generated."
        )
        if pipeline_result.execution_logs:
            with st.expander("📋 View Terraform Planning Error Logs", expanded=True):
                st.code(pipeline_result.execution_logs, language="text")

    # 2. Plan Modification Summary Badges
    st.subheader("1. Planned Resource Modifications")
    b1, b2, b3 = st.columns(3)
    b1.markdown(
        f"""
        <div class="cloud-card" style="border-left: 4px solid #34d399; text-align: center;">
            <div style="font-size: 2rem; font-weight: 700; color: #34d399;">+{to_add}</div>
            <div style="color: #94a3b8; font-size: 14px;">Resources to Add</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    b2.markdown(
        f"""
        <div class="cloud-card" style="border-left: 4px solid #38bdf8; text-align: center;">
            <div style="font-size: 2rem; font-weight: 700; color: #38bdf8;">~{to_change}</div>
            <div style="color: #94a3b8; font-size: 14px;">Resources to Modify</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    b3.markdown(
        f"""
        <div class="cloud-card" style="border-left: 4px solid #f87171; text-align: center;">
            <div style="font-size: 2rem; font-weight: 700; color: #f87171;">-{to_destroy}</div>
            <div style="color: #94a3b8; font-size: 14px;">Resources to Destroy</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Detailed Planned Actions
    for act in actions:
        res_name = act.get("resource") or act.get("name") or "Resource"
        with st.expander(f"➕ {act.get('action', '').upper()}: {res_name}", expanded=True):
            st.write(f"- **Resource Type**: `{act.get('type', 'Unknown')}`")
            st.write(f"- **Action**: `{act.get('action', '').upper()}`")
            if act.get("details"):
                st.write(f"- **Details**: {act['details']}")

    # 3. Security Findings Section (Checkov / Policy Engine)
    st.subheader("2. Security & Compliance Findings")
    if security_scan:
        accepted = getattr(security_scan, "accepted_findings", []) or [f for f in security_scan.findings if f.disposition == "ACCEPTED_RISK"]
        unresolved = getattr(security_scan, "unresolved_findings", []) or [f for f in security_scan.findings if f.disposition == "UNRESOLVED"]
        remediated = getattr(security_scan, "remediated_findings", []) or [f for f in security_scan.findings if f.disposition == "REMEDIATED"]

        mcol1, mcol2, mcol3, mcol4 = st.columns(4)
        mcol1.metric("Passed Checks", security_scan.passed_checks)
        mcol2.metric("Remediated Controls", len(remediated))
        mcol3.metric("Accepted Risks", len(accepted))
        mcol4.metric("Unresolved Findings", len(unresolved))

        # 2A. Accepted Architecture Decisions (e.g. CKV_AWS_88 Public IP in PUBLIC mode)
        if accepted:
            st.markdown("##### 🟡 Accepted Architecture Exceptions")
            for f in accepted:
                with st.expander(f"⚠️ [{f.severity}] {f.title} ({f.rule_id}) — ACCEPTED RISK", expanded=True):
                    st.markdown(
                        f"<span style='background: #fbbf24; color: #000; padding: 2px 8px; border-radius: 4px; font-weight: 700; font-size: 12px;'>"
                        f"ACCEPTED RISK</span> <b>Resource</b>: <code>{f.resource}</code>",
                        unsafe_allow_html=True,
                    )
                    st.write(f"**Security Context**: {f.explanation}")
                    if f.rationale:
                        display_rationale = f.rationale
                        if sub_val and sub_val not in ("N/A", "Unassigned"):
                            display_rationale = re.sub(r"subnet-[a-zA-Z0-9]+", sub_val, display_rationale)
                        st.info(f"**Architecture Justification**: {display_rationale}")
                    else:
                        st.info(f"**Remediation Guidance**: {f.remediation}")

        # 2B. Remediated Controls (e.g. CKV_AWS_382 scoped egress, CKV2_AWS_41 IAM role, CKV_AWS_79 IMDSv2)
        if remediated:
            with st.expander(f"🟢 Verified Remediated Security Controls ({len(remediated)})", expanded=False):
                for r in remediated:
                    st.markdown(
                        f"- <span style='color: #34d399; font-weight: 600;'>✓ [{r.rule_id}]</span> "
                        f"<b>{r.title}</b> (<code>{r.resource}</code>): {r.rationale or r.explanation}",
                        unsafe_allow_html=True,
                    )

        # 2C. Unresolved Vulnerabilities (if any)
        if unresolved:
            st.markdown("##### 🔴 Unresolved Security Vulnerabilities")
            for f in unresolved:
                badge_color = "#f87171" if f.severity == "HIGH" else "#fbbf24"
                with st.expander(f"🚨 [{f.severity}] {f.title} ({f.rule_id}) — UNRESOLVED", expanded=True):
                    st.markdown(
                        f"<span style='background: {badge_color}; color: #000; padding: 2px 8px; border-radius: 4px; font-weight: 700; font-size: 12px;'>"
                        f"{f.severity}</span> <b>Resource</b>: <code>{f.resource}</code>",
                        unsafe_allow_html=True,
                    )
                    st.write(f"**Explanation**: {f.explanation}")
                    st.error(f"**Required Remediation**: {f.remediation}")
        else:
            st.success("✅ **Zero Unresolved Vulnerabilities**: All flagged checks have been remediated or contextually justified as accepted architectural decisions.")

    else:
        st.info("Security scan will execute during the validation pipeline.")

    # 4. Cryptographic Plan Integrity & Target Binding Card
    st.subheader("3. Plan Integrity & AWS Target Binding")
    active_identity = aws_auth_service.get_active_identity()
    target_account = active_identity.account_id if active_identity else ("123456789012 (Mock Account)" if config.enable_mock_mode else "Unauthenticated")
    from services.region_service import get_selected_region
    target_region = plan_result.normalized_spec.region if (plan_result and plan_result.normalized_spec) else get_selected_region()
    target_arn = active_identity.arn if active_identity else ("arn:aws:iam::123456789012:role/MockDeploymentRole" if config.enable_mock_mode else "None")

    if not config.enable_mock_mode and active_identity and active_identity.is_valid and active_identity.region != target_region:
        st.warning(
            f"⚠️ **Target Region Notice**: Active AWS session is authenticated in **{active_identity.region}**, "
            f"but this infrastructure plan is configured for **{target_region}**. "
            f"Approval will be strictly bound to **{target_region}**."
        )

    res_spec = plan_result.normalized_spec.resources[0] if (plan_result and plan_result.normalized_spec and plan_result.normalized_spec.resources) else None
    from core.models import DeploymentMode
    dep_mode_val = getattr(res_spec.network, "deployment_mode", DeploymentMode.PRIVATE).value if res_spec else "PRIVATE"
    pub_ip_val = "ENABLED (Auto-assigned Public IPv4)" if (res_spec and res_spec.network.public_ip) else "DISABLED (Private)"
    sub_val = (res_spec.network.subnet_id or "Unassigned") if res_spec else "N/A"

    if dep_mode_val == "PUBLIC" and res_spec and res_spec.network.public_ip:
        exposure_banner = "⚠️ **Internet Exposure Notice**: The EC2 instance will receive a public IPv4 address and will be directly reachable on authorized security group ingress ports."
        banner_type = "warning"
    else:
        exposure_banner = "🔒 **Internal Security Guarantee**: The EC2 instance has zero direct public internet exposure and is restricted to internal VPC routing."
        banner_type = "info"

    if banner_type == "warning":
        st.warning(exposure_banner)
    else:
        st.info(exposure_banner)

    st.markdown(
        f"""
        <div class="cloud-card" style="background: rgba(99, 102, 241, 0.08); border-color: rgba(99, 102, 241, 0.3);">
            <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                <div>
                    <span style="font-weight: 600; color: #818cf8;">🔒 Cryptographic Plan Integrity Hash</span><br/>
                    <code style="font-size: 13px;">SHA-256: {plan_hash or 'None (Plan Pending/Failed)'}</code><br/>
                    <span style="font-size: 12px; color: #cbd5e1;">Architecture: <b>{dep_mode_val}</b> | Subnet: <code>{sub_val}</code> | Public IP: <b>{pub_ip_val}</b></span>
                </div>
                <div style="text-align: right;">
                    <span style="font-weight: 600; color: #34d399;">🎯 Target AWS Account</span><br/>
                    <code style="font-size: 13px;">{target_account} ({target_region})</code><br/>
                    <span style="font-size: 11px; color: #94a3b8;">ARN: {target_arn}</span>
                </div>
            </div>
            <div style="font-size: 12px; color: #94a3b8; margin-top: 6px;">
                Deployment approval is cryptographically bound to this exact plan artifact and target AWS account.
                Any configuration change or account switch invalidates approval.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Validation of Approval Gate
    approval_blocked_reasons: List[str] = []

    if not pipeline_result:
        approval_blocked_reasons.append("Terraform validation pipeline has not been executed yet.")
    else:
        if not pipeline_result.plan_passed:
            approval_blocked_reasons.append(f"Terraform plan failed: {pipeline_result.error_message or 'Non-zero exit code'}.")
        if not plan_hash or not plan_hash.strip():
            approval_blocked_reasons.append("Cryptographic plan hash is missing or empty.")
        if not pipeline_result.plan_file_path or not Path(pipeline_result.plan_file_path).exists():
            approval_blocked_reasons.append("Terraform binary plan artifact (tfplan) does not exist on disk.")
        else:
            try:
                art_bytes = Path(pipeline_result.plan_file_path).read_bytes()
                calc_art_hash = tf_service.calculate_plan_artifact_hash(art_bytes)
                if calc_art_hash != pipeline_result.plan_artifact_hash:
                    approval_blocked_reasons.append("Plan artifact integrity check failed (file altered or corrupted).")
            except Exception as e:
                approval_blocked_reasons.append(f"Unable to verify plan artifact integrity: {e}")

    # Check for zero-resource plan when creation was expected
    has_spec_resources = bool(plan_result and plan_result.normalized_spec and plan_result.normalized_spec.resources)
    if has_spec_resources and to_add == 0 and to_change == 0 and to_destroy == 0:
        approval_blocked_reasons.append("Infrastructure creation was requested, but Terraform plan contains 0 resource changes.")

    # Check for unresolved infrastructure inputs
    if plan_result and plan_result.readiness in (PlannerReadinessStatus.NEEDS_USER_INPUT, PlannerReadinessStatus.VALIDATION_FAILED):
        reasons_str = ", ".join(plan_result.missing_information or plan_result.validation_errors)
        approval_blocked_reasons.append(f"Required infrastructure inputs remain unresolved: {reasons_str}.")

    # Check mock plan approved for real deployment
    if config.is_real_deployment_allowed() and pipeline_result and pipeline_result.is_mock_plan:
        approval_blocked_reasons.append("Mock or simulated plan cannot be approved for real AWS deployment.")

    can_approve = len(approval_blocked_reasons) == 0

    if not can_approve:
        st.warning("⚠️ **Approval Blocked**:\n" + "\n".join(f"- {r}" for r in approval_blocked_reasons))

    # Action Gateway
    st.divider()
    bcol1, bcol2, bcol3 = st.columns([1.2, 2.2, 2.6])
    with bcol1:
        if st.button("⬅ Back to Code"):
            st.session_state["current_page"] = "Terraform Configuration"
            st.rerun()

    with bcol2:
        if st.button("✅ Approve & Deploy ➔", type="primary", width="stretch", disabled=not can_approve):
            if deployment_id:
                repo = DeploymentRepository()
                rec = repo.get(deployment_id)
                if not rec:
                    st.error("Deployment record not found in database. Approval rejected.")
                    return
                bound_account = active_identity.account_id if active_identity else "123456789012"
                bound_region = target_region
                # Safely extract plan summary dictionary
                plan_summary_dict: Optional[Dict[str, Any]] = None
                if summary:
                    plan_summary_dict = (
                        summary.model_dump()
                        if hasattr(summary, "model_dump")
                        else (summary if isinstance(summary, dict) else None)
                    )
                if not plan_summary_dict and plan_result and plan_result.normalized_spec:
                    plan_summary_dict = tf_service.generate_plan_summary(plan_result.normalized_spec)

                repo.update_approval(
                    deployment_id=deployment_id,
                    approval_status=ApprovalStatus.APPROVED,
                    plan_hash=plan_hash,
                    aws_account_id=bound_account,
                    aws_region=bound_region,
                    normalized_spec=plan_result.normalized_spec.model_dump() if (plan_result and plan_result.normalized_spec) else None,
                    plan_summary=plan_summary_dict,
                )
            st.session_state["plan_approved"] = True
            st.session_state.pop("deployment_executed", None)
            st.session_state.pop("active_deployment_result", None)
            st.session_state.pop("deployment_error", None)
            st.session_state["current_page"] = "Deployment in Progress"
            st.rerun()
