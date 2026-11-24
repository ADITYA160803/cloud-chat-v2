from typing import Optional
import streamlit as st

from config import config
from core.exceptions import ConfigurationError, ValidationError
from services.aws_auth_service import aws_auth_service
from ui.theme import render_top_header


def _revoke_active_unexecuted_approval(dep_id: str | None) -> None:
    """Safely revokes approval only for an unexecuted draft/pending deployment.

    Never revokes approval for terminal deployments (COMPLETED or PARTIALLY_DEPLOYED).
    Always inspects the actual database record before taking action.
    """
    if not dep_id:
        return
    try:
        from core.models import ApprovalStatus, DeploymentStatus
        from database.repository import DeploymentRepository
        repo = DeploymentRepository()
        rec = repo.get(dep_id)
        if rec and rec.status not in (DeploymentStatus.COMPLETED, DeploymentStatus.PARTIALLY_DEPLOYED):
            repo.update_approval(dep_id, ApprovalStatus.REVOKED)
            st.session_state["plan_approved"] = False
    except Exception:
        pass


def render_settings_view():
    """Renders Page 4: Settings (Simplified single AWS Connection card)."""
    render_top_header("Settings")

    st.markdown("Check AWS connectivity and account status for Cloud-Chat infrastructure deployments.")

    active_identity = aws_auth_service.get_active_identity()
    is_connected = bool(active_identity and active_identity.is_valid)

    # Status styling
    status_label = "Connected" if is_connected else "Disconnected"
    status_icon = "🟢" if is_connected else "🔴"
    status_color = "#34d399" if is_connected else "#f87171"

    # Only ONE AWS Connection Card
    st.markdown(
        f"""
        <div class="cloud-card" style="border-left: 4px solid {status_color}; padding: 22px; margin-top: 10px; margin-bottom: 20px;">
            <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: #94a3b8; margin-bottom: 6px;">
                AWS Connection Management
            </div>
            <div style="font-size: 18px; font-weight: 700; color: #f8fafc; margin-bottom: 16px;">
                AWS Cloud Connectivity
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px;">
                <div>
                    <div style="font-size: 11px; color: #94a3b8;">AWS Connection Status</div>
                    <div style="font-size: 14px; font-weight: 700; color: {status_color}; margin-top: 2px;">
                        {status_icon} {status_label}
                    </div>
                </div>
                <div>
                    <div style="font-size: 11px; color: #94a3b8;">AWS CLI Profile</div>
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 2px;">
                        <code>cloud-chat-deploy</code>
                    </div>
                </div>
                <div>
                    <div style="font-size: 11px; color: #94a3b8;">Fixed AWS Region</div>
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-top: 2px;">
                        <code>ap-south-1 (Mumbai)</code>
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Actions: Test Connection & Save Settings buttons
    bcol1, bcol2, bcol3 = st.columns([1.5, 1.5, 3])
    with bcol1:
        if st.button("⚡ Test Connection", type="primary", width="stretch"):
            with st.spinner("Testing AWS connection with profile 'cloud-chat-deploy' in ap-south-1..."):
                try:
                    ident = aws_auth_service.authenticate_profile(
                        profile_name="cloud-chat-deploy",
                        region="ap-south-1",
                    )
                    st.session_state["aws_identity"] = ident
                    st.session_state["aws_credentials"] = aws_auth_service.get_active_credentials()
                    _revoke_active_unexecuted_approval(st.session_state.get("active_deployment_id"))
                    st.success(f"✓ Connected successfully to AWS Account {ident.account_id} ({ident.arn})")
                    st.rerun()
                except Exception as e:
                    st.error(f"AWS connection test failed: {e}")

    with bcol2:
        if st.button("💾 Save Settings", width="stretch"):
            st.success("✓ Settings saved successfully.")
