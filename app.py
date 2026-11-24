import streamlit as st
from config import config
from database.db import init_db
from ui.chat_view import render_chat_view
from ui.cost_view import render_cost_view
from ui.deployment_progress_view import render_deployment_progress_view
from ui.deployment_view import render_deployment_view
from ui.history_view import render_history_view
from ui.home_view import render_home_view
from ui.intent_view import render_intent_view
from ui.plan_review_view import render_plan_review_view
from ui.resources_view import render_resources_view
from ui.saved_plans_view import render_saved_plans_view
from ui.settings_view import render_settings_view
from ui.terraform_code_view import render_terraform_code_view
from ui.theme import apply_theme, get_effective_region
from ui.verification_view import render_verification_view

# Streamlit Page Configuration
st.set_page_config(
    page_title="Cloud-Chat V2",
    page_icon="☁️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def ensure_initialized():
    """Initializes SQLite database and session state lazily on startup."""
    if "db_initialized" not in st.session_state:
        init_db()
        st.session_state["db_initialized"] = True

    if "current_page" not in st.session_state:
        st.session_state["current_page"] = "New Deployment"

    if "last_rendered_page" not in st.session_state:
        st.session_state["last_rendered_page"] = "New Deployment"

    if "theme" not in st.session_state:
        st.session_state["theme"] = "dark"


def _resolve_plans_page() -> str:
    """Legacy helper maintained for backward compatibility. Direct routing now uses 'Deployments'."""
    return "Deployments"


def main():
    ensure_initialized()
    apply_theme()

    # Fixed Left Sidebar Branding
    st.sidebar.markdown(
        """
        <div style="padding: 10px 0 16px 0;">
            <div style="display: flex; align-items: center; gap: 8px;">
                <span style="font-size: 24px;">☁️</span>
                <span style="font-size: 20px; font-weight: 700; color: #818cf8;">Cloud-Chat</span>
            </div>
            <div style="font-size: 11px; color: #94a3b8; margin-top: 2px;">
                AI to AWS with Terraform
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Sidebar Navigation Items — Exactly Four Pages
    sidebar_items = [
        "💬 New Deployment",
        "🚀 Deployments",
        "📜 Deployment History",
        "⚙️ Settings",
    ]

    # Map wizard sub-steps to sidebar items
    page_to_sidebar = {
        "New Deployment": "💬 New Deployment",
        "Infrastructure Intent": "💬 New Deployment",
        "Cost Estimation": "💬 New Deployment",
        "Terraform Configuration": "💬 New Deployment",
        "Terraform Plan Review": "💬 New Deployment",
        "Deployments": "🚀 Deployments",
        "Deployment in Progress": "🚀 Deployments",
        "Verification": "🚀 Deployments",
        "Deployment History": "📜 Deployment History",
        "Settings": "⚙️ Settings",
        # Legacy mappings for backward compatibility
        "Home": "💬 New Deployment",
        "Deployment Plans": "🚀 Deployments",
        "My Resources": "🚀 Deployments",
    }

    # When user clicks a sidebar item directly, route to that view
    sidebar_to_page = {
        "💬 New Deployment": "New Deployment",
        "🚀 Deployments": "Deployments",
        "📜 Deployment History": "Deployment History",
        "⚙️ Settings": "Settings",
    }

    current_page = st.session_state.get("current_page", "New Deployment")
    last_rendered = st.session_state.get("last_rendered_page", "New Deployment")

    # If current_page changed programmatically (via an in-page button or workflow step),
    # synchronize the sidebar widget value BEFORE creating the widget.
    if current_page != last_rendered:
        st.session_state["nav_radio_sidebar"] = page_to_sidebar.get(current_page, "💬 New Deployment")
        st.session_state["last_rendered_page"] = current_page

    # Initialize nav_radio_sidebar if not present yet
    if "nav_radio_sidebar" not in st.session_state:
        st.session_state["nav_radio_sidebar"] = page_to_sidebar.get(current_page, "💬 New Deployment")

    def on_sidebar_nav_change():
        selected = st.session_state.get("nav_radio_sidebar")
        if selected:
            routed = sidebar_to_page.get(selected, "New Deployment")
            st.session_state["current_page"] = routed
            st.session_state["last_rendered_page"] = routed

    st.sidebar.radio(
        "Navigation",
        sidebar_items,
        key="nav_radio_sidebar",
        on_change=on_sidebar_nav_change,
        label_visibility="collapsed",
    )

    # Effective page to render
    current_page = st.session_state.get("current_page", "New Deployment")

    # Sidebar Safety Guardrail Badges
    st.sidebar.divider()
    st.sidebar.markdown(
        """
        <div style="font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8; margin-bottom: 8px;">
            Safety Guardrails
        </div>
        """,
        unsafe_allow_html=True,
    )
    if config.enable_mock_mode:
        st.sidebar.markdown(
            "<div style='background: #1e1b4b; border: 1px solid #4338ca; border-radius: 8px; padding: 6px 12px; margin-bottom: 6px; font-size: 13px; color: #c7d2fe;'>"
            "🛡️ <b>Mock Mode: Active</b></div>",
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.warning("⚠️ Mock Mode: Disabled")

    if not config.enable_real_terraform_apply:
        st.sidebar.markdown(
            "<div style='background: #064e3b; border: 1px solid #047857; border-radius: 8px; padding: 6px 12px; font-size: 13px; color: #a7f3d0;'>"
            "🔒 <b>Real Apply: Disabled</b></div>",
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.error("🚨 Real Apply: ENABLED")

    st.sidebar.caption(f"Region: {get_effective_region()}")

    # Render Active Page (4 core sidebar pages + seamless workflow steps)
    if current_page == "New Deployment":
        render_chat_view()
    elif current_page == "Deployments":
        render_deployment_view()
    elif current_page == "Deployment History":
        render_history_view()
    elif current_page == "Settings":
        render_settings_view()
    elif current_page == "Infrastructure Intent":
        render_intent_view()
    elif current_page == "Cost Estimation":
        render_cost_view()
    elif current_page == "Terraform Configuration":
        render_terraform_code_view()
    elif current_page == "Terraform Plan Review":
        render_plan_review_view()
    elif current_page == "Deployment in Progress":
        render_deployment_progress_view()
    elif current_page == "Verification":
        render_verification_view()
    elif current_page == "Home":
        render_chat_view()
    elif current_page in ("Deployment Plans", "My Resources"):
        render_deployment_view()


if __name__ == "__main__":
    main()
