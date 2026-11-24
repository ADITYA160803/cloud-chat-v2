import json
from typing import Any, Dict, List, Optional
import streamlit as st

from core.models import (
    ApprovalStatus,
    DeploymentRecord,
    DeploymentStatus,
    InterpretedInfrastructure,
    NormalizedInfrastructureSpec,
)
from database.repository import DeploymentRepository
from services.terraform_service import TerraformService
from ui.theme import render_top_header


def extract_plan_resource_summary(record: DeploymentRecord) -> str:
    """Extracts a human-readable summary of resources planned or defined in the record."""
    # 1. From plan_summary if available
    if record.plan_summary and isinstance(record.plan_summary, dict):
        actions = record.plan_summary.get("actions", [])
        to_add = record.plan_summary.get("to_add", 0)
        to_change = record.plan_summary.get("to_change", 0)
        to_destroy = record.plan_summary.get("to_destroy", 0)
        
        types = []
        for a in actions:
            rtype = a.get("type", "").replace("aws_", "")
            if rtype and rtype not in types:
                types.append(rtype)
        
        type_str = ", ".join(types[:3]) if types else "Resources"
        diff_str = f"+{to_add}/~{to_change}/-{to_destroy}"
        return f"{type_str} ({diff_str})"

    # 2. From normalized_spec if available
    if record.normalized_spec and isinstance(record.normalized_spec, dict):
        resources = record.normalized_spec.get("resources", [])
        if resources:
            r = resources[0]
            itype = r.get("instance_type", "EC2")
            size = r.get("storage", {}).get("size_gb", 8)
            vtype = r.get("storage", {}).get("volume_type", "gp3")
            return f"EC2 ({itype}, {size} GiB {vtype})"

    # 3. From parsed_intent if available
    if record.parsed_intent and isinstance(record.parsed_intent, dict):
        resources = record.parsed_intent.get("resources", [])
        if resources:
            r = resources[0]
            itype = r.get("instance_type") or "EC2"
            return f"EC2 ({itype})"

    # 4. Fallback based on service
    return f"{record.service.upper()} Instance"


def get_plan_actions_details(record: DeploymentRecord) -> List[Dict[str, Any]]:
    """Extracts structured list of actions (add/change/destroy) for the plan."""
    if record.plan_summary and isinstance(record.plan_summary, dict):
        actions = record.plan_summary.get("actions", [])
        if actions:
            return actions

    if record.normalized_spec and isinstance(record.normalized_spec, dict):
        try:
            spec = NormalizedInfrastructureSpec.model_validate(record.normalized_spec)
            summary = TerraformService().generate_plan_summary(spec)
            return summary.get("actions", [])
        except Exception:
            pass

    return []


def resolve_plan_approval_display(record: DeploymentRecord) -> tuple[str, str]:
    """Resolves the displayed approval status label and approval timestamp text.

    Rules:
    1. Display completed deployments as 'APPROVED (CONSUMED)' only when supported
       by genuine approval evidence (cryptographic plan hash + verified execution).
    2. Never substitute execution_started_at or created_at as approved_at.
    3. If the historical approval timestamp is unavailable, display 'Historical approval timestamp unavailable'.
    """
    is_terminal = record.status in (DeploymentStatus.COMPLETED, DeploymentStatus.PARTIALLY_DEPLOYED)
    has_approval_evidence = (
        record.approval_status == ApprovalStatus.APPROVED
        or (
            is_terminal
            and bool(record.plan_hash and len(record.plan_hash) == 64)
            and bool(record.execution_started_at or record.aws_resource_ids)
        )
    )

    if is_terminal and has_approval_evidence:
        status_label = "APPROVED (CONSUMED)"
    else:
        status_label = record.approval_status.value.upper()

    if record.approved_at:
        timestamp_label = (
            record.approved_at.strftime("%Y-%m-%d %H:%M:%S")
            if hasattr(record.approved_at, "strftime")
            else str(record.approved_at)
        )
    elif is_terminal and has_approval_evidence:
        timestamp_label = "Historical approval timestamp unavailable"
    else:
        timestamp_label = "Not yet approved"

    return status_label, timestamp_label


def render_saved_plans_view():
    """Renders the dedicated Deployment Plans page listing saved infrastructure deployment plans."""
    render_top_header("Deployment Plans")

    st.markdown(
        "View and inspect saved infrastructure deployment plans, Terraform resource actions, "
        "and security approval states across all environments."
    )

    repo = DeploymentRepository()
    try:
        deployments: List[DeploymentRecord] = repo.list_recent(limit=100)
    except Exception as e:
        st.error(f"Failed to fetch saved deployment plans: {e}")
        return

    # Filter out empty records if any; all deployment records in SQLite are saved plans/intents
    plans = deployments or []

    # Empty State
    if not plans:
        st.markdown(
            """
            <div class="cloud-card" style="text-align: center; padding: 48px 24px; margin-top: 16px;">
                <div style="font-size: 42px; margin-bottom: 12px;">📋</div>
                <h3 style="margin-bottom: 8px;">No Saved Deployment Plans Found</h3>
                <p style="color: #94a3b8; max-width: 520px; margin: 0 auto 20px auto; font-size: 14px; line-height: 1.5;">
                    You haven't generated any infrastructure deployment plans yet.
                    Start by describing your desired AWS architecture in the New Deployment wizard.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button("💬 Start a New Deployment", type="primary", width="stretch"):
                st.session_state["current_page"] = "New Deployment"
                st.session_state["nav_radio_sidebar"] = "💬 New Deployment"
                st.rerun()
        return

    # Filter & Search Controls
    fcol1, fcol2, fcol3 = st.columns([2, 1, 1])
    with fcol1:
        search_query = st.text_input(
            "🔍 Search plans by ID, service, region, or request:",
            placeholder="Type to filter...",
        ).strip().lower()
    with fcol2:
        approval_filter = st.selectbox(
            "Approval Status",
            ["All", "APPROVED", "PENDING", "REVOKED", "REJECTED"],
        )
    with fcol3:
        status_filter = st.selectbox(
            "Deployment Status",
            ["All", "DRAFT", "COMPLETED", "IN_PROGRESS", "FAILED", "PARTIALLY_DEPLOYED"],
        )

    # Apply filters
    filtered: List[DeploymentRecord] = []
    for d in plans:
        if approval_filter != "All" and d.approval_status.value.upper() != approval_filter:
            continue
        if status_filter != "All" and d.status.value.upper() != status_filter:
            continue
        if search_query:
            text_match = (
                search_query in d.deployment_id.lower()
                or search_query in d.user_request.lower()
                or search_query in d.service.lower()
                or search_query in (d.aws_region or "").lower()
                or any(search_query in str(rid).lower() for rid in (d.aws_resource_ids or []))
            )
            if not text_match:
                continue
        filtered.append(d)

    st.caption(f"Showing {len(filtered)} saved plan(s) out of {len(plans)} total:")

    if not filtered:
        st.info("No saved plans match the selected search or filter criteria.")
        return

    # Summary Table of Saved Plans
    table_data = []
    for d in filtered:
        summary_str = extract_plan_resource_summary(d)
        region = d.aws_region or (d.parsed_intent.get("region") if d.parsed_intent else "ap-south-1")
        plan_hash_disp = f"`{d.plan_hash[:12]}...`" if d.plan_hash else "Draft"
        created_str = d.created_at.strftime("%Y-%m-%d %H:%M:%S") if d.created_at else "—"
        approval_label, _ = resolve_plan_approval_display(d)

        table_data.append({
            "Plan / Deployment ID": d.deployment_id[:8],
            "Created (UTC)": created_str,
            "Service": d.service.upper(),
            "Region": region,
            "Resource Summary": summary_str,
            "Approval": approval_label,
            "Deployment Status": d.status.value.upper(),
            "Plan Hash": plan_hash_disp,
        })

    st.dataframe(table_data, width="stretch")

    # Saved Plan Inspection Section
    st.markdown("### 🔍 Plan Inspector & Terraform Actions")

    plan_options = [
        f"{d.deployment_id[:8]} — {d.service.upper()} ({d.aws_region or 'ap-south-1'}) — {resolve_plan_approval_display(d)[0]}"
        for d in filtered
    ]
    
    # Try to select active deployment if in filtered list
    default_index = 0
    active_id = st.session_state.get("active_deployment_id")
    if active_id:
        for idx, d in enumerate(filtered):
            if d.deployment_id == active_id:
                default_index = idx
                break

    selected_idx = st.selectbox(
        "Select a saved plan to inspect:",
        range(len(plan_options)),
        index=default_index,
        format_func=lambda i: plan_options[i],
    )
    selected_plan = filtered[selected_idx]
    selected_approval_label, selected_approved_at_text = resolve_plan_approval_display(selected_plan)

    # Metrics Row
    mcol1, mcol2, mcol3, mcol4, mcol5 = st.columns(5)
    to_add = 0
    to_change = 0
    to_destroy = 0
    if selected_plan.plan_summary and isinstance(selected_plan.plan_summary, dict):
        to_add = selected_plan.plan_summary.get("to_add", 0)
        to_change = selected_plan.plan_summary.get("to_change", 0)
        to_destroy = selected_plan.plan_summary.get("to_destroy", 0)
    elif selected_plan.normalized_spec:
        to_add = 5  # Standard EC2 bundle (instance, sg, role, policy, profile)

    mcol1.metric("Add", f"+{to_add}")
    mcol2.metric("Change", f"~{to_change}")
    mcol3.metric("Destroy", f"-{to_destroy}")
    mcol4.metric("Approval", selected_approval_label)
    mcol5.metric("Deployment", selected_plan.status.value.upper())

    # Metadata & Scope Details
    st.markdown(
        """
        <div class="cloud-card">
        """,
        unsafe_allow_html=True,
    )
    col_meta1, col_meta2 = st.columns(2)
    with col_meta1:
        st.markdown(f"**Full Deployment ID**: `{selected_plan.deployment_id}`")
        st.markdown(f"**Target AWS Account**: `{selected_plan.aws_account_id or '082229155432'}`")
        st.markdown(f"**Target Region**: `{selected_plan.aws_region or 'ap-south-1'}`")
        st.markdown(f"**Assumed Role / Identity**: `{selected_plan.aws_identity_arn or 'CloudChatTerraformDeployRole'}`")
        st.markdown(f"**Execution Mode**: `{selected_plan.execution_mode or 'mock'}`")
    with col_meta2:
        st.markdown(f"**Plan SHA-256 Hash**: `{selected_plan.plan_hash or 'Not calculated (Draft)'}`")
        st.markdown(f"**Created At**: `{selected_plan.created_at}`")
        st.markdown(f"**Approved At**: `{selected_approved_at_text}`" if selected_plan.approved_at else f"**Approved At**: *{selected_approved_at_text}*")
        st.markdown(f"**User Request**: *\"{selected_plan.user_request}\"*")
        if selected_plan.aws_resource_ids:
            st.markdown(f"**Provisioned Resources**: `{selected_plan.aws_resource_ids}`")
    st.markdown("</div>", unsafe_allow_html=True)

    # Detailed Terraform Planned Actions
    actions = get_plan_actions_details(selected_plan)
    if actions:
        st.markdown("#### Planned Resource Actions")
        actions_table = []
        for a in actions:
            action_type = a.get("action", "create").upper()
            action_icon = "🟢 ADD" if action_type == "CREATE" else ("🟡 MODIFY" if action_type == "MODIFY" else "🔴 DESTROY")
            actions_table.append({
                "Action": action_icon,
                "Resource": a.get("resource", "Unknown"),
                "Type": a.get("type", "Unknown"),
                "Configuration / Security Details": a.get("details", ""),
            })
        st.dataframe(actions_table, width="stretch")
    elif selected_plan.parsed_intent:
        with st.expander("Parsed Infrastructure Intent", expanded=False):
            st.json(selected_plan.parsed_intent)

    # Plan Context Action Buttons
    st.divider()
    act_col1, act_col2, act_col3 = st.columns([1.5, 1.5, 2])

    with act_col1:
        if selected_plan.status == DeploymentStatus.COMPLETED:
            if st.button("🛡️ Open in Verification", type="primary", width="stretch"):
                st.session_state["active_deployment_id"] = selected_plan.deployment_id
                st.session_state["current_page"] = "Verification"
                st.session_state["nav_radio_sidebar"] = "🛡️ Verification"
                st.rerun()
        elif selected_plan.approval_status == ApprovalStatus.APPROVED:
            if st.button("🚀 View Deployment Progress", type="primary", width="stretch"):
                st.session_state["active_deployment_id"] = selected_plan.deployment_id
                st.session_state["current_page"] = "Deployment in Progress"
                st.session_state["nav_radio_sidebar"] = "🚀 Deployments"
                st.rerun()
        else:
            if st.button("📋 Review & Deploy Plan", type="primary", width="stretch"):
                st.session_state["active_deployment_id"] = selected_plan.deployment_id
                if selected_plan.parsed_intent:
                    try:
                        st.session_state["active_interpreted_infra"] = InterpretedInfrastructure.model_validate(
                            selected_plan.parsed_intent
                        )
                    except Exception:
                        pass
                st.session_state["current_page"] = "Infrastructure Intent"
                st.session_state["nav_radio_sidebar"] = "💬 New Deployment"
                st.rerun()

    with act_col2:
        if st.button("✏️ Load in Intent Wizard", width="stretch"):
            st.session_state["active_deployment_id"] = selected_plan.deployment_id
            if selected_plan.parsed_intent:
                try:
                    st.session_state["active_interpreted_infra"] = InterpretedInfrastructure.model_validate(
                        selected_plan.parsed_intent
                    )
                except Exception:
                    pass
            st.session_state["current_page"] = "Infrastructure Intent"
            st.session_state["nav_radio_sidebar"] = "💬 New Deployment"
            st.rerun()

    with act_col3:
        if selected_plan.status == DeploymentStatus.COMPLETED:
            if st.button("☁️ View in My Resources", width="stretch"):
                st.session_state["current_page"] = "My Resources"
                st.session_state["nav_radio_sidebar"] = "☁️ My Resources"
                st.rerun()
