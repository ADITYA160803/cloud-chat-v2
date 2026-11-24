import streamlit as st
from config import config
from database.repository import DeploymentRepository


def render_dashboard():
    """Renders the executive overview and system health dashboard."""
    st.title("Cloud-Chat V2 Dashboard")
    st.markdown("Effortless natural-language AWS infrastructure deployment.")

    repo = DeploymentRepository()
    try:
        summary = repo.count_summary()
    except Exception as e:
        summary = {"total": 0, "successful": 0, "failed": 0, "in_progress": 0}
        st.warning(f"Could not load deployment summary: {e}")

    # Top metrics row
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Deployments", summary.get("total", 0))
    with col2:
        st.metric("Successful", summary.get("successful", 0))
    with col3:
        st.metric("Failed", summary.get("failed", 0))
    with col4:
        st.metric("In Progress", summary.get("in_progress", 0))

    st.divider()

    # System Safety & Environment Status
    st.subheader("System Safety & Guardrails")
    gcol1, gcol2, gcol3 = st.columns(3)
    with gcol1:
        mock_color = "green" if config.enable_mock_mode else "red"
        st.markdown(
            f"**Mock Mode**: <span style='color:{mock_color}; font-weight:bold;'>"
            f"{'ENABLED (Safe)' if config.enable_mock_mode else 'DISABLED'}</span>",
            unsafe_allow_html=True,
        )
        st.caption("Guarantees no accidental AWS provisioning during development.")

    with gcol2:
        apply_color = "red" if config.enable_real_terraform_apply else "green"
        st.markdown(
            f"**Real Terraform Apply**: <span style='color:{apply_color}; font-weight:bold;'>"
            f"{'ENABLED' if config.enable_real_terraform_apply else 'DISABLED (Protected)'}</span>",
            unsafe_allow_html=True,
        )
        st.caption("Must be deliberately enabled by operator for real cloud changes.")

    with gcol3:
        from services.region_service import get_selected_region
        st.markdown(f"**Target Region**: `{get_selected_region()}`")
        st.markdown(f"**Primary AI Provider**: `{config.default_ai_provider.upper()}`")

    st.divider()

    # Quick start & recent activity
    st.subheader("Quick Start")
    qcol1, qcol2 = st.columns([2, 1])
    with qcol1:
        st.info(
            "Ready to provision infrastructure? Go to **Infrastructure Builder** and describe "
            "what you need in plain English (e.g. *'Create a private EC2 instance in Mumbai'*)."
        )
        if st.button("Launch Infrastructure Builder", type="primary"):
            st.session_state["current_page"] = "Infrastructure Builder"
            st.rerun()

    with qcol2:
        st.markdown("##### Recent Activity")
        recent = repo.list_recent(limit=5)
        if not recent:
            st.caption("No deployments yet. Create your first one!")
        else:
            for item in recent:
                st.text(f"• [{item.status.value.upper()}] {item.deployment_id[:8]} - {item.service}")
