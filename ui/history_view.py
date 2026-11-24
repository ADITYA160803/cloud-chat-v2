from typing import Any, Dict, List, Optional
import streamlit as st

from core.models import NormalizedInfrastructureSpec
from database.repository import DeploymentRepository
from services.aws_verification_service import AWSVerificationService
from ui.resources_view import extract_deployment_resources
from ui.theme import render_top_header


def _extract_summary_details(rec) -> Dict[str, Any]:
    """Helper to extract instance ID and IP details using canonical extraction function."""
    return extract_deployment_resources(rec)


def render_history_view():
    """Renders Page 3: Deployment History."""
    render_top_header("Deployment History")

    st.markdown("Search and inspect past infrastructure deployment records and verification statuses.")

    repo = DeploymentRepository()
    try:
        deployments = repo.list_recent(limit=100)
    except Exception as e:
        st.error(f"Failed to fetch deployment history: {e}")
        return

    if not deployments:
        st.markdown(
            """
            <div class="cloud-card" style="text-align: center; padding: 40px; margin-top: 20px;">
                <div style="font-size: 32px; margin-bottom: 12px;">📜</div>
                <h4 style="color: #f8fafc; margin-bottom: 8px;">No Deployments Found</h4>
                <p style="color: #94a3b8; font-size: 14px; max-width: 500px; margin: 0 auto 20px auto;">
                    You have not provisioned any infrastructure yet. New deployments will be recorded here.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        bcol1, bcol2, bcol3 = st.columns([1.5, 2, 1.5])
        with bcol2:
            if st.button("🚀 Start a New Deployment", type="primary", width="stretch"):
                st.session_state["current_page"] = "New Deployment"
                st.rerun()
        return

    # Search Box supporting Deployment ID, Plan ID, or original request text
    search_query = st.text_input(
        "🔍 Search by Deployment ID, Plan ID, or Request:",
        placeholder="Type deployment ID, plan hash, or request words to filter...",
    ).strip().lower()

    filtered = []
    for d in deployments:
        if search_query:
            matches_dep_id = search_query in d.deployment_id.lower()
            matches_request = search_query in d.user_request.lower()
            matches_plan = bool(d.plan_hash and search_query in d.plan_hash.lower())
            if not (matches_dep_id or matches_request or matches_plan):
                continue
        filtered.append(d)

    st.caption(f"Showing {len(filtered)} deployment record(s):")

    # Compact History Table with exactly: | Deployment ID | Request | Date | Status |
    table_data = []
    details_map = {}
    for d in filtered:
        info = _extract_summary_details(d)
        details_map[d.deployment_id] = info
        date_str = d.created_at.strftime("%Y-%m-%d %H:%M") if d.created_at else "N/A"
        req_summary = d.user_request[:60] + ("..." if len(d.user_request) > 60 else "")
        table_data.append({
            "Deployment ID": d.deployment_id[:8],
            "Request": req_summary,
            "Date": date_str,
            "Status": d.status.value.upper(),
        })

    st.dataframe(table_data, width="stretch")

    # Selected Record Details Section
    if filtered:
        st.markdown("<div style='margin-top: 16px;'></div>", unsafe_allow_html=True)
        dep_options = [f"{d.deployment_id[:8]} — {d.user_request[:50]}" for d in filtered]
        selected_idx = st.selectbox(
            "Select Record to Inspect:",
            range(len(dep_options)),
            format_func=lambda i: dep_options[i],
        )
        rec = filtered[selected_idx]
        info = details_map.get(rec.deployment_id, _extract_summary_details(rec))

        # Color badges for status
        apply_color = "#34d399" if rec.status.value.upper() == "COMPLETED" else ("#f87171" if rec.status.value.upper() == "FAILED" else "#818cf8")
        verif_color = "#34d399" if rec.verification_status.value == "VERIFIED" else ("#fbbf24" if "WARN" in rec.verification_status.value else "#f87171")
        inst_id_val = info.get("instance_id") or "Not Available / Not Provisioned"

        st.markdown(
            f"""
            <div class="cloud-card" style="margin-top: 14px; padding: 20px; border-left: 4px solid {apply_color};">
                <div style="font-size: 15px; font-weight: 700; color: #f8fafc; margin-bottom: 14px;">
                    📌 Selected Record Details
                </div>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px;">
                    <div>
                        <div style="font-size: 11px; color: #94a3b8;">Deployment ID</div>
                        <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 2px;">
                            <code>{rec.deployment_id}</code>
                        </div>
                    </div>
                    <div>
                        <div style="font-size: 11px; color: #94a3b8;">Terraform Apply Status</div>
                        <div style="font-size: 13px; font-weight: 700; color: {apply_color}; margin-top: 2px;">
                            {rec.status.value.upper()}
                        </div>
                    </div>
                    <div>
                        <div style="font-size: 11px; color: #94a3b8;">AWS Verification Status</div>
                        <div style="font-size: 13px; font-weight: 700; color: {verif_color}; margin-top: 2px;">
                            {rec.verification_status.value.upper()}
                        </div>
                    </div>
                    <div>
                        <div style="font-size: 11px; color: #94a3b8;">EC2 Instance ID</div>
                        <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 2px;">
                            <code>{inst_id_val}</code>
                        </div>
                    </div>
                </div>
                <div style="margin-top: 16px;">
                    <div style="font-size: 11px; color: #94a3b8;">Original User Request</div>
                    <div style="font-size: 13px; color: #cbd5e1; margin-top: 4px; background: rgba(15, 23, 42, 0.6); padding: 10px 14px; border-radius: 6px;">
                        {rec.user_request}
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
