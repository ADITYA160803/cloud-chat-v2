import streamlit as st
from config import config
from ui.theme import render_top_header


def render_home_view():
    """Renders Page 1: Home / Landing Page."""
    render_top_header("Home")

    # Hero Section
    st.markdown(
        """
        <div class="cloud-card" style="text-align: center; padding: 40px 20px;">
            <h1 style="font-size: 2.4rem; margin-bottom: 8px;">Welcome to Cloud-Chat</h1>
            <p style="font-size: 1.15rem; max-width: 680px; margin: 0 auto 24px auto;">
                Describe your AWS infrastructure in plain English. Cloud-Chat interprets requirements with AI,
                generates secure Terraform configurations, and safely provisions cloud resources.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Primary Call to Action
    bcol1, bcol2, bcol3 = st.columns([1, 1.5, 1])
    with bcol2:
        if st.button("🚀 Start a New Deployment", type="primary", width="stretch"):
            st.session_state["current_page"] = "New Deployment"
            st.rerun()

    st.markdown("<br/>", unsafe_allow_html=True)

    # 3 Core Pillars / Feature Cards
    fcol1, fcol2, fcol3 = st.columns(3)

    with fcol1:
        st.markdown(
            """
            <div class="feature-card">
                <div style="font-size: 2.2rem; margin-bottom: 12px;">💬</div>
                <h3 style="margin-bottom: 8px;">1. Describe</h3>
                <p>Tell Cloud-Chat what AWS infrastructure you need in conversational, ordinary English.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with fcol2:
        st.markdown(
            """
            <div class="feature-card">
                <div style="font-size: 2.2rem; margin-bottom: 12px;">🔍</div>
                <h3 style="margin-bottom: 8px;">2. Review</h3>
                <p>Review the interpreted requirements, transparent cost estimate, and planned Terraform code.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with fcol3:
        st.markdown(
            """
            <div class="feature-card">
                <div style="font-size: 2.2rem; margin-bottom: 12px;">🛡️</div>
                <h3 style="margin-bottom: 8px;">3. Deploy</h3>
                <p>Explicitly approve the exact plan and independently verify actual AWS resources via Boto3.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
