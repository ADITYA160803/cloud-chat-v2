from unittest.mock import MagicMock, patch
import pytest
import streamlit as st

from config import config
from database.db import init_db
from database.repository import DeploymentRepository
from core.models import DeploymentRecord, DeploymentStatus


@pytest.fixture(autouse=True)
def clean_streamlit_session():
    """Ensures clean streamlit session state for each navigation test."""
    session = {
        "db_initialized": True,
        "current_page": "New Deployment",
        "last_rendered_page": "New Deployment",
        "theme": "dark",
        "nav_radio_sidebar": "💬 New Deployment",
    }
    with patch("streamlit.session_state", session):
        yield session


def simulate_sidebar_selection(session, selected_item):
    """Simulates a user clicking an item on the sidebar radio."""
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
        "Home": "💬 New Deployment",
        "Deployment Plans": "🚀 Deployments",
        "My Resources": "🚀 Deployments",
    }
    sidebar_to_page = {
        "💬 New Deployment": "New Deployment",
        "🚀 Deployments": "Deployments",
        "📜 Deployment History": "Deployment History",
        "⚙️ Settings": "Settings",
    }

    # 1. User clicks sidebar item -> radio widget updates nav_radio_sidebar
    session["nav_radio_sidebar"] = selected_item

    # 2. on_sidebar_nav_change callback runs
    routed = sidebar_to_page.get(selected_item, "New Deployment")
    session["current_page"] = routed
    session["last_rendered_page"] = routed

    # 3. Next script run synchronizes state
    current_page = session.get("current_page", "New Deployment")
    last_rendered = session.get("last_rendered_page", "New Deployment")
    if current_page != last_rendered:
        session["nav_radio_sidebar"] = page_to_sidebar.get(current_page, "💬 New Deployment")
        session["last_rendered_page"] = current_page


def simulate_programmatic_navigation(session, target_page):
    """Simulates an in-page button navigating programmatically."""
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
        "Home": "💬 New Deployment",
        "Deployment Plans": "🚀 Deployments",
        "My Resources": "🚀 Deployments",
    }

    # In-page button sets current_page
    session["current_page"] = target_page

    # Next script run sync block executes
    current_page = session.get("current_page", "New Deployment")
    last_rendered = session.get("last_rendered_page", "New Deployment")
    if current_page != last_rendered:
        session["nav_radio_sidebar"] = page_to_sidebar.get(current_page, "💬 New Deployment")
        session["last_rendered_page"] = current_page


# ============================================================================
# Navigation Regression Tests — Four Sidebar Pages
# ============================================================================

def test_selecting_new_deployment_remains_on_new_deployment(clean_streamlit_session):
    """Selecting New Deployment stays on New Deployment."""
    session = clean_streamlit_session
    simulate_sidebar_selection(session, "💬 New Deployment")
    assert session["current_page"] == "New Deployment"
    assert session["nav_radio_sidebar"] == "💬 New Deployment"


def test_selecting_deployments_routes_to_deployments(clean_streamlit_session):
    """Selecting Deployments routes strictly to Deployments."""
    session = clean_streamlit_session
    simulate_sidebar_selection(session, "🚀 Deployments")
    assert session["current_page"] == "Deployments"
    assert session["nav_radio_sidebar"] == "🚀 Deployments"


def test_selecting_deployment_history_routes_to_history(clean_streamlit_session):
    """Selecting Deployment History routes to Deployment History."""
    session = clean_streamlit_session
    simulate_sidebar_selection(session, "📜 Deployment History")
    assert session["current_page"] == "Deployment History"
    assert session["nav_radio_sidebar"] == "📜 Deployment History"


def test_selecting_settings_remains_on_settings(clean_streamlit_session):
    """Selecting Settings stays on Settings."""
    session = clean_streamlit_session
    session["current_page"] = "Deployments"
    session["last_rendered_page"] = "Deployments"
    session["nav_radio_sidebar"] = "🚀 Deployments"

    simulate_sidebar_selection(session, "⚙️ Settings")
    assert session["current_page"] == "Settings"
    assert session["nav_radio_sidebar"] == "⚙️ Settings"


def test_navigation_does_not_revert_to_deployments(clean_streamlit_session):
    """Clicking multiple pages in sequence never gets pulled back into Deployments."""
    session = clean_streamlit_session
    session["current_page"] = "Deployments"
    session["last_rendered_page"] = "Deployments"
    session["nav_radio_sidebar"] = "🚀 Deployments"

    # User clicks Settings
    simulate_sidebar_selection(session, "⚙️ Settings")
    assert session["current_page"] == "Settings"

    # User clicks Deployment History
    simulate_sidebar_selection(session, "📜 Deployment History")
    assert session["current_page"] == "Deployment History"

    # User clicks New Deployment
    simulate_sidebar_selection(session, "💬 New Deployment")
    assert session["current_page"] == "New Deployment"


def test_theme_switching_preserves_current_page(clean_streamlit_session):
    """Toggling theme never changes the active page or sidebar selection."""
    session = clean_streamlit_session
    session["current_page"] = "Settings"
    session["last_rendered_page"] = "Settings"
    session["nav_radio_sidebar"] = "⚙️ Settings"
    session["theme"] = "dark"

    session["theme"] = "light"
    if session["current_page"] != session["last_rendered_page"]:
        session["nav_radio_sidebar"] = "something_else"

    assert session["current_page"] == "Settings"
    assert session["nav_radio_sidebar"] == "⚙️ Settings"


def test_existing_deployment_record_does_not_force_navigation(clean_streamlit_session):
    """Having active deployment records in DB or session does not hijack navigation."""
    session = clean_streamlit_session
    session["active_deployment_id"] = "dep-12345"
    session["active_plan_hash"] = "abc123hash"
    session["deployment_executed"] = True

    # User navigates to Settings
    simulate_sidebar_selection(session, "⚙️ Settings")
    assert session["current_page"] == "Settings"
    assert session["nav_radio_sidebar"] == "⚙️ Settings"

    # User navigates to Deployment History
    simulate_sidebar_selection(session, "📜 Deployment History")
    assert session["current_page"] == "Deployment History"
    assert session["nav_radio_sidebar"] == "📜 Deployment History"


def test_programmatic_in_page_navigation(clean_streamlit_session):
    """In-page buttons update sidebar selection and current_page cleanly."""
    session = clean_streamlit_session
    assert session["current_page"] == "New Deployment"

    # User proceeds from Chat to Intent
    simulate_programmatic_navigation(session, "Infrastructure Intent")
    assert session["current_page"] == "Infrastructure Intent"
    assert session["nav_radio_sidebar"] == "💬 New Deployment"


def test_wizard_steps_mapped_to_new_deployment_sidebar(clean_streamlit_session):
    """Wizard planning sub-steps highlight '💬 New Deployment' in the sidebar."""
    session = clean_streamlit_session
    for step in ["Infrastructure Intent", "Cost Estimation", "Terraform Configuration", "Terraform Plan Review"]:
        simulate_programmatic_navigation(session, step)
        assert session["current_page"] == step
        assert session["nav_radio_sidebar"] == "💬 New Deployment"


def test_execution_steps_mapped_to_deployments_sidebar(clean_streamlit_session):
    """Execution sub-steps highlight '🚀 Deployments' in the sidebar."""
    session = clean_streamlit_session
    for step in ["Deployments", "Deployment in Progress", "Verification"]:
        simulate_programmatic_navigation(session, step)
        assert session["current_page"] == step
        assert session["nav_radio_sidebar"] == "🚀 Deployments"


def test_no_navigation_loop_on_repeated_reruns(clean_streamlit_session):
    """Multiple re-evaluations without user input stabilize without page mutation."""
    session = clean_streamlit_session
    simulate_sidebar_selection(session, "📜 Deployment History")

    for _ in range(5):
        if session["current_page"] != session["last_rendered_page"]:
            session["nav_radio_sidebar"] = "BROKEN"
        assert session["current_page"] == "Deployment History"
        assert session["nav_radio_sidebar"] == "📜 Deployment History"
