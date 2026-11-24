import streamlit as st
from config import config
from services.aws_auth_service import aws_auth_service

DARK_THEME_CSS = """
<style>
/* Global Dark Theme Variables */
:root {
    --bg-primary: #0b1120;
    --bg-secondary: #131d31;
    --bg-card: #17223b;
    --border-color: #263554;
    --text-primary: #f8fafc;
    --text-secondary: #94a3b8;
    --input-bg: #0f172a;
    --input-text: #f8fafc;
    --input-border: #334155;
    --code-bg: #0f172a;
    --code-text: #e2e8f0;
    --accent-purple: #818cf8;
    --accent-blue: #38bdf8;
    --accent-gradient: linear-gradient(135deg, #6366f1 0%, #3b82f6 100%);
    --success: #34d399;
    --warning: #fbbf24;
    --danger: #f87171;
}

/* App Background */
.stApp {
    background-color: var(--bg-primary);
    color: var(--text-primary);
}

/* Sidebar */
section[data-testid="stSidebar"] {
    background-color: var(--bg-secondary) !important;
    border-right: 1px solid var(--border-color);
}
section[data-testid="stSidebar"] .stRadio label {
    color: var(--text-primary) !important;
    font-size: 14px;
}
section[data-testid="stSidebar"] p, section[data-testid="stSidebar"] span {
    color: var(--text-secondary);
}

/* Cards & Containers */
div.cloud-card {
    background-color: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 16px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
    color: var(--text-primary);
}

div.feature-card {
    background-color: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 24px;
    text-align: center;
    transition: transform 0.2s ease, border-color 0.2s ease;
    color: var(--text-primary);
}
div.feature-card:hover {
    border-color: var(--accent-purple);
    transform: translateY(-2px);
}

/* Chat Bubbles */
div.chat-user {
    background: #1e293b;
    border: 1px solid var(--border-color);
    border-radius: 14px 14px 2px 14px;
    padding: 12px 18px;
    margin-bottom: 12px;
    color: #f1f5f9;
}
div.chat-ai {
    background: #1e1b4b;
    border: 1px solid #4338ca;
    border-radius: 14px 14px 14px 2px;
    padding: 14px 18px;
    margin-bottom: 12px;
    color: #f8fafc;
}

/* Headers & Text */
h1, h2, h3, h4, h5, h6 {
    color: var(--text-primary) !important;
    font-weight: 600;
}
p, span, label {
    color: var(--text-secondary);
}

/* Inputs & Text Areas */
div[data-baseweb="textarea"] textarea,
div[data-baseweb="input"] input,
div[data-testid="stTextInput"] input,
div[data-testid="stTextArea"] textarea {
    background-color: var(--input-bg) !important;
    color: var(--input-text) !important;
    border: 1px solid var(--input-border) !important;
    border-radius: 8px !important;
}

/* Selectboxes & Dropdowns */
div[data-baseweb="select"] > div {
    background-color: var(--input-bg) !important;
    color: var(--input-text) !important;
    border: 1px solid var(--input-border) !important;
    border-radius: 8px !important;
}
div[data-baseweb="select"] span {
    color: var(--input-text) !important;
}
div[data-baseweb="popover"], div[data-baseweb="menu"], ul[role="listbox"] {
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
    border: 1px solid var(--border-color) !important;
}
li[role="option"] {
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
}
li[role="option"]:hover {
    background-color: #1e293b !important;
}

/* Expanders */
details[data-testid="stExpander"] {
    background-color: var(--bg-card) !important;
    border: 1px solid var(--border-color) !important;
    border-radius: 8px !important;
    color: var(--text-primary) !important;
}
details[data-testid="stExpander"] summary {
    color: var(--text-primary) !important;
    font-weight: 600;
}
details[data-testid="stExpander"] div[data-testid="stExpanderDetails"] {
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
}

/* Code Blocks */
pre, code, div[data-testid="stCodeBlock"] {
    background-color: var(--code-bg) !important;
    color: var(--code-text) !important;
    border-color: var(--border-color) !important;
}

/* Tables / DataFrames */
div[data-testid="stDataFrame"], div[data-testid="stTable"] {
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
    border-radius: 8px;
}

/* Metric Display */
div[data-testid="stMetricValue"] {
    color: #ffffff !important;
    font-weight: 700;
}
div[data-testid="stMetricLabel"] {
    color: var(--text-secondary) !important;
}

/* Buttons */
button[kind="primary"] {
    background: var(--accent-gradient) !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
    padding: 8px 20px !important;
    box-shadow: 0 2px 8px rgba(99, 102, 241, 0.35);
}
button[kind="secondary"], button[data-testid="baseButton-secondary"] {
    background-color: #1e293b !important;
    color: #f8fafc !important;
    border: 1px solid var(--border-color) !important;
    border-radius: 8px !important;
}
</style>
"""

LIGHT_THEME_CSS = """
<style>
/* Global Light Theme Variables */
:root {
    --bg-primary: #f8fafc;
    --bg-secondary: #f1f5f9;
    --bg-card: #ffffff;
    --border-color: #cbd5e1;
    --text-primary: #0f172a;
    --text-secondary: #475569;
    --input-bg: #ffffff;
    --input-text: #0f172a;
    --input-border: #cbd5e1;
    --code-bg: #f1f5f9;
    --code-text: #0f172a;
    --accent-purple: #6366f1;
    --accent-blue: #2563eb;
    --accent-gradient: linear-gradient(135deg, #6366f1 0%, #3b82f6 100%);
    --success: #10b981;
    --warning: #f59e0b;
    --danger: #ef4444;
}

/* App Background */
.stApp {
    background-color: var(--bg-primary);
    color: var(--text-primary);
}

/* Sidebar */
section[data-testid="stSidebar"] {
    background-color: var(--bg-secondary) !important;
    border-right: 1px solid var(--border-color);
}
section[data-testid="stSidebar"] .stRadio label {
    color: var(--text-primary) !important;
    font-size: 14px;
}
section[data-testid="stSidebar"] p, section[data-testid="stSidebar"] span {
    color: var(--text-secondary);
}

/* Cards & Containers */
div.cloud-card {
    background-color: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 16px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
    color: var(--text-primary);
}

div.feature-card {
    background-color: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 24px;
    text-align: center;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.04);
    transition: transform 0.2s ease, border-color 0.2s ease;
    color: var(--text-primary);
}
div.feature-card:hover {
    border-color: var(--accent-purple);
    transform: translateY(-2px);
}

/* Chat Bubbles */
div.chat-user {
    background: #f1f5f9;
    border: 1px solid #cbd5e1;
    border-radius: 14px 14px 2px 14px;
    padding: 12px 18px;
    margin-bottom: 12px;
    color: #0f172a;
}
div.chat-ai {
    background: #eff6ff;
    border: 1px solid #93c5fd;
    border-radius: 14px 14px 14px 2px;
    padding: 14px 18px;
    margin-bottom: 12px;
    color: #1e3a8a;
}

/* Headers & Text */
h1, h2, h3, h4, h5, h6 {
    color: var(--text-primary) !important;
    font-weight: 600;
}
p, span, label {
    color: var(--text-secondary);
}

/* Inputs & Text Areas */
div[data-baseweb="textarea"] textarea,
div[data-baseweb="input"] input,
div[data-testid="stTextInput"] input,
div[data-testid="stTextArea"] textarea {
    background-color: var(--input-bg) !important;
    color: var(--input-text) !important;
    border: 1px solid var(--input-border) !important;
    border-radius: 8px !important;
}

/* Selectboxes & Dropdowns */
div[data-baseweb="select"] > div {
    background-color: var(--input-bg) !important;
    color: var(--input-text) !important;
    border: 1px solid var(--input-border) !important;
    border-radius: 8px !important;
}
div[data-baseweb="select"] span {
    color: var(--input-text) !important;
}
div[data-baseweb="popover"], div[data-baseweb="menu"], ul[role="listbox"] {
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
    border: 1px solid var(--border-color) !important;
}
li[role="option"] {
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
}
li[role="option"]:hover {
    background-color: #f1f5f9 !important;
}

/* Expanders */
details[data-testid="stExpander"] {
    background-color: var(--bg-card) !important;
    border: 1px solid var(--border-color) !important;
    border-radius: 8px !important;
    color: var(--text-primary) !important;
}
details[data-testid="stExpander"] summary {
    color: var(--text-primary) !important;
    font-weight: 600;
}
details[data-testid="stExpander"] div[data-testid="stExpanderDetails"] {
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
}

/* Code Blocks */
pre, code, div[data-testid="stCodeBlock"] {
    background-color: var(--code-bg) !important;
    color: var(--code-text) !important;
    border-color: var(--border-color) !important;
}

/* Tables / DataFrames */
div[data-testid="stDataFrame"], div[data-testid="stTable"] {
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
    border-radius: 8px;
}

/* Metric Display */
div[data-testid="stMetricValue"] {
    color: #0f172a !important;
    font-weight: 700;
}
div[data-testid="stMetricLabel"] {
    color: var(--text-secondary) !important;
}

/* Buttons */
button[kind="primary"] {
    background: var(--accent-gradient) !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
    padding: 8px 20px !important;
}
button[kind="secondary"], button[data-testid="baseButton-secondary"] {
    background-color: #f1f5f9 !important;
    color: #0f172a !important;
    border: 1px solid var(--border-color) !important;
    border-radius: 8px !important;
}
</style>
"""


def apply_theme():
    """Applies the user-selected theme (Dark or Light) globally across the entire app."""
    if "theme" not in st.session_state:
        st.session_state["theme"] = "dark"

    current = st.session_state["theme"]
    if current == "light":
        st.markdown(LIGHT_THEME_CSS, unsafe_allow_html=True)
    else:
        st.markdown(DARK_THEME_CSS, unsafe_allow_html=True)


def get_effective_region() -> str:
    """Returns the effective active deployment region from session state or the single source of truth."""
    try:
        import streamlit as st
        if hasattr(st, "session_state"):
            if "active_plan_result" in st.session_state and st.session_state["active_plan_result"]:
                plan = st.session_state["active_plan_result"]
                if getattr(plan, "normalized_spec", None) and getattr(plan.normalized_spec, "region", None):
                    return plan.normalized_spec.region
            if "active_interpreted_infra" in st.session_state and st.session_state["active_interpreted_infra"]:
                infra = st.session_state["active_interpreted_infra"]
                if getattr(infra, "region", None):
                    return infra.region
    except Exception:
        pass

    from services.region_service import get_selected_region
    return get_selected_region()


def render_top_header(page_title: str):
    """Renders the standard top header with real AWS status, effective region, and Sun/Moon theme toggle."""
    hcol1, hcol2, hcol3 = st.columns([3, 2, 1])

    with hcol1:
        st.markdown(f"<h2 style='margin-bottom: 0px;'>{page_title}</h2>", unsafe_allow_html=True)

    with hcol2:
        # Check actual AWS status & Identity
        active_identity = aws_auth_service.get_active_identity()
        effective_reg = get_effective_region()
        if not config.enable_mock_mode and active_identity and active_identity.is_valid:
            st.markdown(
                f"<div style='margin-top: 8px; text-align: right;'>"
                f"<span style='background: #064e3b; color: #a7f3d0; padding: 4px 10px; border-radius: 12px; font-size: 13px; font-weight: 500;'>"
                f"🟢 AWS: {active_identity.account_id} ({effective_reg})</span></div>",
                unsafe_allow_html=True,
            )
        elif config.enable_mock_mode:
            st.markdown(
                f"<div style='margin-top: 8px; text-align: right;'>"
                f"<span style='background: #312e81; color: #c7d2fe; padding: 4px 10px; border-radius: 12px; font-size: 13px; font-weight: 500;'>"
                f"🛡️ Mock Mode | {effective_reg}</span></div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<div style='margin-top: 8px; text-align: right;'>"
                f"<span style='background: #78350f; color: #fde68a; padding: 4px 10px; border-radius: 12px; font-size: 13px; font-weight: 500;'>"
                f"⚠️ AWS Disconnected ({effective_reg})</span></div>",
                unsafe_allow_html=True,
            )

    with hcol3:
        theme = st.session_state.get("theme", "dark")
        toggle_label = "☀️ Light" if theme == "dark" else "🌙 Dark"
        if st.button(toggle_label, key="global_theme_toggle_btn", width="stretch"):
            st.session_state["theme"] = "light" if theme == "dark" else "dark"
            st.rerun()

    st.markdown("<hr style='margin-top: 6px; margin-bottom: 20px; border-color: rgba(148, 163, 184, 0.2);' />", unsafe_allow_html=True)
