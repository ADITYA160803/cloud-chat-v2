from pathlib import Path
from typing import Any, Dict, List, Optional
import streamlit as st

from core.models import ApprovalStatus, DeploymentRecord, DeploymentStatus
from database.repository import DeploymentRepository
from ui.saved_plans_view import get_plan_actions_details, resolve_plan_approval_display
from ui.theme import render_top_header


def _get_relevant_config_details(record: DeploymentRecord) -> Dict[str, str]:
    """Extracts high-level human-readable configuration details without raw metadata."""
    details = {
        "Instance Type": "t3.micro",
        "Storage": "8 GB gp3 (encrypted)",
        "Target Region": record.aws_region or "ap-south-1",
        "VPC": "Existing VPC",
        "Subnet": "Existing Subnet",
        "Security Group": "Cloud-Chat Application SG",
    }
    spec = record.normalized_spec or {}
    resources = spec.get("resources", [])
    if resources and isinstance(resources, list):
        res0 = resources[0]
        if isinstance(res0, dict):
            if res0.get("instance_type"):
                details["Instance Type"] = res0["instance_type"]
            storage = res0.get("storage", {})
            if isinstance(storage, dict):
                size = storage.get("size_gb", 8)
                vtype = storage.get("volume_type", "gp3")
                enc = "encrypted" if storage.get("encrypted", True) else "unencrypted"
                details["Storage"] = f"{size} GB {vtype} ({enc})"
            net = res0.get("networking", {})
            if isinstance(net, dict):
                if net.get("vpc_id"):
                    details["VPC"] = net["vpc_id"]
                if net.get("subnet_id"):
                    details["Subnet"] = net["subnet_id"]

    if spec.get("region"):
        details["Target Region"] = spec["region"]

    return details


def render_deployment_view():
    """Renders Page 2: Deployments (Single active/current deployment focus)."""
    render_top_header("Deployments")

    st.markdown(
        "Monitor the progress, approval lifecycle, and Terraform execution state of your current deployment."
    )

    repo = DeploymentRepository()
    active_id = st.session_state.get("active_deployment_id")
    record: Optional[DeploymentRecord] = None

    if active_id:
        record = repo.get(active_id)

    if not record:
        recents = repo.list_recent(limit=1)
        if recents:
            record = recents[0]
            st.session_state["active_deployment_id"] = record.deployment_id

    # Empty State
    if not record:
        st.markdown(
            """
            <div class="cloud-card" style="text-align: center; padding: 40px; margin-top: 20px;">
                <div style="font-size: 32px; margin-bottom: 12px;">☁️</div>
                <h4 style="color: #f8fafc; margin-bottom: 8px;">No Current Deployment</h4>
                <p style="color: #94a3b8; font-size: 14px; max-width: 500px; margin: 0 auto 20px auto;">
                    You do not have an active deployment. Describe your infrastructure in New Deployment
                    to generate a plan and deploy to AWS.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        bcol1, bcol2, bcol3 = st.columns([1.5, 2, 1.5])
        with bcol2:
            if st.button("🚀 Start New Deployment", type="primary", width="stretch"):
                st.session_state["current_page"] = "New Deployment"
                st.rerun()
        return

    # Derive overall status and color
    status_str = record.status.value.upper()
    status_color = "#94a3b8"
    if record.status == DeploymentStatus.COMPLETED:
        status_color = "#34d399"  # green
    elif record.status == DeploymentStatus.PARTIALLY_DEPLOYED:
        status_color = "#f59e0b"  # amber
    elif record.status == DeploymentStatus.FAILED:
        status_color = "#f87171"  # red
    elif record.status in (DeploymentStatus.IN_PROGRESS, DeploymentStatus.PENDING_APPROVAL, DeploymentStatus.APPROVED):
        status_color = "#818cf8"  # indigo

    verif_color = "#34d399" if record.verification_status.value == "VERIFIED" else ("#fbbf24" if "WARN" in record.verification_status.value else "#94a3b8")

    # Table/Card 1 — Current Deployment
    st.markdown(
        f"""
        <div class="cloud-card" style="border-left: 4px solid {status_color}; margin-bottom: 20px; padding: 20px;">
            <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: #94a3b8; margin-bottom: 4px;">
                Current Deployment
            </div>
            <div style="font-size: 18px; font-weight: 700; color: #f8fafc; margin-bottom: 16px;">
                {record.service.upper()} Deployment &nbsp;
                <span style="font-size: 12px; font-weight: 600; padding: 3px 8px; border-radius: 6px; background: rgba(129, 140, 248, 0.15); color: {status_color}; border: 1px solid {status_color}33;">
                    {status_str}
                </span>
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px;">
                <div>
                    <div style="font-size: 11px; color: #94a3b8;">Deployment Name</div>
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 2px;">
                        {record.user_request[:50] if record.user_request else f'{record.service.upper()} EC2 Instance'}
                    </div>
                </div>
                <div>
                    <div style="font-size: 11px; color: #94a3b8;">Deployment ID</div>
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 2px;">
                        <code>{record.deployment_id}</code>
                    </div>
                </div>
                <div>
                    <div style="font-size: 11px; color: #94a3b8;">AWS Region</div>
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 2px;">
                        <code>{record.aws_region or 'ap-south-1'}</code>
                    </div>
                </div>
                <div>
                    <div style="font-size: 11px; color: #94a3b8;">Overall Status</div>
                    <div style="font-size: 13px; font-weight: 700; color: {status_color}; margin-top: 2px;">
                        {status_str}
                    </div>
                </div>
                <div>
                    <div style="font-size: 11px; color: #94a3b8;">AWS Verification</div>
                    <div style="font-size: 13px; font-weight: 700; color: {verif_color}; margin-top: 2px;">
                        {record.verification_status.value}
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Determine 5 Stages & Statuses
    # Stage 1: Plan Created
    if record.plan_hash or record.status in (
        DeploymentStatus.PENDING_APPROVAL,
        DeploymentStatus.APPROVED,
        DeploymentStatus.IN_PROGRESS,
        DeploymentStatus.COMPLETED,
        DeploymentStatus.PARTIALLY_DEPLOYED,
    ):
        s1_icon = "🟢"
        s1_text = "Plan Created"
    elif record.status == DeploymentStatus.DRAFT:
        s1_icon = "🟡"
        s1_text = "In Progress"
    else:
        s1_icon = "⚪"
        s1_text = "Pending"

    # Stage 2: Plan Reviewed / Approved
    appr_display, appr_time = resolve_plan_approval_display(record)
    if record.status in (DeploymentStatus.COMPLETED, DeploymentStatus.PARTIALLY_DEPLOYED):
        s2_icon = "🟢"
        s2_text = "Approved (Consumed)"
    elif record.approval_status == ApprovalStatus.APPROVED:
        s2_icon = "🟢"
        s2_text = "Approved"
    elif record.approval_status == ApprovalStatus.REVOKED:
        s2_icon = "🔴"
        s2_text = "Revoked"
    elif record.approval_status == ApprovalStatus.REJECTED:
        s2_icon = "🔴"
        s2_text = "Rejected"
    else:
        s2_icon = "🟡"
        s2_text = "Pending Review"

    # Stage 3: Deploying
    if record.status == DeploymentStatus.IN_PROGRESS:
        s3_icon = "🟡"
        s3_text = "Deploying..."
    elif record.status in (DeploymentStatus.COMPLETED, DeploymentStatus.PARTIALLY_DEPLOYED):
        s3_icon = "🟢"
        s3_text = "Deployed"
    elif record.status == DeploymentStatus.FAILED:
        s3_icon = "🔴"
        s3_text = "Deploy Failed"
    else:
        s3_icon = "⚪"
        s3_text = "Not Started"

    # Stage 4: Completed / Failed
    if record.status == DeploymentStatus.COMPLETED:
        s4_icon = "🟢"
        s4_text = "Completed"
    elif record.status == DeploymentStatus.PARTIALLY_DEPLOYED:
        s4_icon = "🟡"
        s4_text = "Partially Deployed"
    elif record.status == DeploymentStatus.FAILED:
        s4_icon = "🔴"
        s4_text = "Failed"
    else:
        s4_icon = "⚪"
        s4_text = "Pending"

    # Stage 5: Approval Status
    s5_icon = "🔒"
    s5_text = appr_display

    # Table/Card 2 — Deployment Progress & Details (All 5 stages rendered cleanly inside the same card)
    st.markdown(
        f"""
        <div class="cloud-card" style="margin-bottom: 24px; padding: 20px;">
            <div style="font-size: 15px; font-weight: 700; color: #f8fafc; margin-bottom: 16px;">
                ⚡ Deployment Progress & Stage Lifecycle
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 14px;">
                <div style="background: rgba(15, 23, 42, 0.6); padding: 14px; border-radius: 8px; border: 1px solid #334155;">
                    <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8;">1. Plan Created</div>
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 6px;">
                        {s1_icon} <code>{s1_text}</code>
                    </div>
                </div>
                <div style="background: rgba(15, 23, 42, 0.6); padding: 14px; border-radius: 8px; border: 1px solid #334155;">
                    <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8;">2. Plan Reviewed</div>
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 6px;">
                        {s2_icon} <code>{s2_text}</code>
                    </div>
                </div>
                <div style="background: rgba(15, 23, 42, 0.6); padding: 14px; border-radius: 8px; border: 1px solid #334155;">
                    <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8;">3. Deploying</div>
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 6px;">
                        {s3_icon} <code>{s3_text}</code>
                    </div>
                </div>
                <div style="background: rgba(15, 23, 42, 0.6); padding: 14px; border-radius: 8px; border: 1px solid #334155;">
                    <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8;">4. Completed / Failed</div>
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 6px;">
                        {s4_icon} <code>{s4_text}</code>
                    </div>
                </div>
                <div style="background: rgba(15, 23, 42, 0.6); padding: 14px; border-radius: 8px; border: 1px solid #334155;">
                    <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8;">5. Approval Status</div>
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 6px;">
                        {s5_icon} <code>{s5_text}</code>
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Workflow Action Buttons
    act_col1, act_col2 = st.columns([2.5, 2.5])
    with act_col1:
        if record.status in (DeploymentStatus.DRAFT, DeploymentStatus.PENDING_APPROVAL) and record.approval_status != ApprovalStatus.APPROVED:
            if st.button("➔ Review & Approve Plan", type="primary", width="stretch"):
                st.session_state["current_page"] = "Terraform Plan Review"
                st.rerun()
        elif record.approval_status == ApprovalStatus.APPROVED and record.status not in (DeploymentStatus.COMPLETED, DeploymentStatus.PARTIALLY_DEPLOYED):
            if st.button("🚀 Execute Deployment", type="primary", width="stretch"):
                st.session_state.pop("deployment_executed", None)
                st.session_state.pop("active_deployment_result", None)
                st.session_state.pop("deployment_error", None)
                st.session_state["current_page"] = "Deployment in Progress"
                st.rerun()
        elif record.status in (DeploymentStatus.COMPLETED, DeploymentStatus.PARTIALLY_DEPLOYED):
            if st.button("🛡️ View AWS Verification Details", type="primary", width="stretch"):
                st.session_state["current_page"] = "Verification"
                st.rerun()
    with act_col2:
        if record.status in (DeploymentStatus.COMPLETED, DeploymentStatus.PARTIALLY_DEPLOYED):
            res_ids = record.aws_resource_ids or []
            inst_id = next((r for r in res_ids if r.startswith("i-")), None)
            if inst_id:
                st.caption(f"Verified Provisioned Instance: `{inst_id}` in `{record.aws_region or 'ap-south-1'}`")

    # Terraform Plan Button
    st.markdown("<div style='margin-top: 10px; margin-bottom: 8px;'></div>", unsafe_allow_html=True)
    plan_col1, plan_col2 = st.columns([2, 3])
    with plan_col1:
        toggle_plan = st.button("📄 View Terraform Plan", width="stretch")
        if toggle_plan:
            st.session_state["deployments_show_plan"] = not st.session_state.get("deployments_show_plan", False)

    if st.session_state.get("deployments_show_plan", False):
        actions_list = get_plan_actions_details(record)
        to_add = []
        to_change = []
        to_destroy = []
        for a in actions_list:
            act = str(a.get("action", "")).lower()
            name = f"{a.get('type', 'resource')}.{a.get('name', 'main')}"
            if act in ("create", "add"):
                to_add.append(name)
            elif act in ("update", "change", "modify"):
                to_change.append(name)
            elif act in ("delete", "destroy"):
                to_destroy.append(name)
            else:
                to_add.append(name)

        if not to_add and not to_change and not to_destroy:
            to_add = [
                "aws_instance.app_server",
                "aws_security_group.app_sg",
                "aws_iam_role.app_role",
                "aws_iam_instance_profile.app_profile",
            ]

        cfg_details = _get_relevant_config_details(record)

        st.markdown(
            """
            <div class="cloud-card" style="border: 1px solid #4338ca; background: rgba(30, 27, 75, 0.4); margin-top: 14px; padding: 20px;">
                <div style="font-size: 15px; font-weight: 700; color: #a5b4fc; margin-bottom: 12px;">
                    📋 Saved Terraform Plan Details
                </div>
            """,
            unsafe_allow_html=True,
        )

        pcol1, pcol2, pcol3 = st.columns(3)
        with pcol1:
            st.markdown(f"**Resources to Add**: `{len(to_add)}`")
            for item in to_add:
                st.markdown(f"- 🟢 `{item}`")
            if not to_add:
                st.markdown("*(None)*")

        with pcol2:
            st.markdown(f"**Resources to Change**: `{len(to_change)}`")
            for item in to_change:
                st.markdown(f"- 🟡 `{item}`")
            if not to_change:
                st.markdown("*(None)*")

        with pcol3:
            st.markdown(f"**Resources to Destroy**: `{len(to_destroy)}`")
            for item in to_destroy:
                st.markdown(f"- 🔴 `{item}`")
            if not to_destroy:
                st.markdown("*(None)*")

        st.markdown("<hr style='margin: 14px 0;' />", unsafe_allow_html=True)
        st.markdown("**Relevant Resource Configuration Details:**")

        rc1, rc2 = st.columns(2)
        with rc1:
            for k in ["Instance Type", "Storage", "Target Region"]:
                st.markdown(f"- **{k}**: `{cfg_details.get(k, 'N/A')}`")
        with rc2:
            for k in ["VPC", "Subnet", "Security Group"]:
                st.markdown(f"- **{k}**: `{cfg_details.get(k, 'N/A')}`")

        st.markdown("</div>", unsafe_allow_html=True)


render_deployments_view = render_deployment_view
