from typing import Optional
import uuid
import streamlit as st
from config import config
from core.exceptions import AIParserError, ConfigurationError, ValidationError
from core.models import DeploymentRecord, DeploymentStatus, InterpretedInfrastructure
from database.repository import DeploymentRepository
from services.ai_parser import AIInfrastructureParser
from ui.theme import render_top_header

PRESET_EXAMPLES = [
    "Create a private EC2 instance in Mumbai with t3.micro, 8 GB encrypted gp3 storage, using my existing VPC and subnet.",
    "Launch a public EC2 instance in Mumbai with t3.micro, 8 GB gp3 storage, public IP enabled, using my existing VPC and public subnet.",
    "Deploy a production EC2 instance with t3.micro, 8 GB encrypted gp3 storage, monitoring enabled, and tag Environment=Production.",
]


def _format_chat_content(content: str) -> str:
    """Converts markdown formatting (e.g. **bold**, `code`, newlines) to clean HTML."""
    if not content:
        return ""
    try:
        import markdown
        html_out = markdown.markdown(content.strip())
        if html_out.startswith("<p>") and html_out.endswith("</p>") and html_out.count("<p>") == 1:
            html_out = html_out[3:-4]
        return html_out
    except Exception:
        import re
        formatted = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', content)
        formatted = re.sub(r'`(.*?)`', r'<code>\1</code>', formatted)
        formatted = formatted.replace('\n\n', '<br/><br/>').replace('\n', '<br/>')
        return formatted


def _get_or_init_chat_messages(repo: Optional[DeploymentRepository] = None):
    """Loads chat messages from SQLite or seeds the initial welcome greeting."""
    if repo is None:
        repo = DeploymentRepository()
    db_msgs = repo.list_chat_messages(limit=100)
    if not db_msgs:
        welcome_id = "msg-welcome-default"
        welcome_content = (
            "Hello! I am your AI Cloud Architect. Describe the AWS infrastructure you want to deploy, "
            "or pick one of the sample requests below to get started."
        )
        repo.add_chat_message(welcome_id, "assistant", welcome_content)
        return [{"message_id": welcome_id, "role": "assistant", "content": welcome_content}]

    # Filter out duplicate message_ids if any corrupted duplicates were inserted
    seen_ids = set()
    deduped = []
    for msg in db_msgs:
        mid = msg.get("message_id")
        if mid and mid in seen_ids:
            continue
        if mid:
            seen_ids.add(mid)
        deduped.append(msg)
    return deduped


def process_chat_submission(
    user_input: str,
    submission_id: str,
    session_state: dict,
    repo: DeploymentRepository,
    parser: Optional[AIInfrastructureParser] = None,
) -> bool:
    """Processes a user chat submission idempotently using a unique submission_id.

    Guarantees:
    - Exactly one user message and one assistant message per unique submission_id.
    - Prevents duplicate messages on reruns of the same submission_id.
    - Allows the same prompt to be intentionally submitted multiple times with distinct submission_ids.
    - Preserves existing conversation history in database and session state.
    """
    clean_input = user_input.strip()
    if not clean_input:
        return False

    processed_ids = session_state.setdefault("processed_submission_ids", set())
    if submission_id in processed_ids:
        # This exact submission has already been processed (e.g. during a rerun)
        return False

    # Mark submission_id as processed immediately
    processed_ids.add(submission_id)
    session_state["last_processed_submission_id"] = submission_id
    session_state["user_chat_input"] = ""

    messages = session_state.setdefault("chat_messages", [])

    # Use unique submission_id for message IDs
    user_msg_id = f"user-{submission_id}"
    repo.add_chat_message(user_msg_id, "user", clean_input)
    messages.append({
        "message_id": user_msg_id,
        "role": "user",
        "content": clean_input,
    })

    # AI Parsing & response generation
    if parser is None:
        parser = AIInfrastructureParser()

    interpreted: InterpretedInfrastructure = parser.parse(clean_input)
    session_state["active_interpreted_infra"] = interpreted

    # Persist draft deployment to SQLite repository
    dep_id = str(uuid.uuid4())
    record = DeploymentRecord(
        deployment_id=dep_id,
        user_request=clean_input,
        service=interpreted.service.value,
        status=DeploymentStatus.DRAFT,
        parsed_intent=interpreted.model_dump(),
        aws_region=interpreted.region,
    )
    repo.create(record)
    session_state["active_deployment_id"] = dep_id

    res = interpreted.resources[0] if interpreted.resources else None
    inst_type = res.instance_type if res else "t3.micro"
    response_text = (
        f"I have successfully interpreted your request! Target service is **{interpreted.service.value.upper()}** "
        f"in **{interpreted.region}** with **{inst_type}**.\n\n"
        "Let's review the structured infrastructure intent."
    )

    ai_msg_id = f"ai-{submission_id}"
    repo.add_chat_message(ai_msg_id, "assistant", response_text, deployment_id=dep_id)
    messages.append({
        "message_id": ai_msg_id,
        "role": "assistant",
        "content": response_text,
    })

    return True


def render_chat_view():
    """Renders Page 1: New Deployment."""
    render_top_header("New Deployment")

    st.markdown(
        "Describe the required AWS infrastructure in plain English. "
        "Cloud-Chat will interpret your requirements and generate a Terraform deployment plan."
    )

    # Initialize chat history from persistent repository
    if "chat_messages" not in st.session_state:
        st.session_state["chat_messages"] = _get_or_init_chat_messages()

    # Three Quick Examples
    st.markdown("<div style='font-size: 13px; font-weight: 600; color: #94a3b8; margin-bottom: 6px;'>Quick Examples:</div>", unsafe_allow_html=True)
    ecol1, ecol2, ecol3 = st.columns(3)
    with ecol1:
        if st.button("🔒 Private EC2", key="ex_1", width="stretch"):
            st.session_state["user_chat_input"] = PRESET_EXAMPLES[0]
            st.rerun()
    with ecol2:
        if st.button("🌐 Public EC2", key="ex_2", width="stretch"):
            st.session_state["user_chat_input"] = PRESET_EXAMPLES[1]
            st.rerun()
    with ecol3:
        if st.button("🚀 Production EC2", key="ex_3", width="stretch"):
            st.session_state["user_chat_input"] = PRESET_EXAMPLES[2]
            st.rerun()

    # User Request Input (visible immediately without scrolling)
    preset_val = st.session_state.get("user_chat_input", "")
    with st.form("chat_form", clear_on_submit=True):
        user_input = st.text_area(
            "Infrastructure Request:",
            value=preset_val,
            placeholder="e.g. Create a private EC2 instance in Mumbai with t3.micro, 8 GB encrypted gp3 storage...",
            height=110,
        )
        fcol1, fcol2 = st.columns([1.5, 3.5])
        with fcol1:
            submitted = st.form_submit_button("Send Request", type="primary", width="stretch")
        with fcol2:
            st.caption("Press Send Request to interpret requirements and plan infrastructure.")

    if submitted:
        clean_input = user_input.strip()
        if not clean_input:
            st.warning("Please type an infrastructure request before sending.")
            return

        # Maintain or generate unique submission_id for this actual form submission
        if "pending_submission_id" not in st.session_state:
            st.session_state["pending_submission_id"] = str(uuid.uuid4())

        submission_id = st.session_state["pending_submission_id"]

        with st.spinner("Analyzing infrastructure intent with AI..."):
            try:
                repo = DeploymentRepository()
                success = process_chat_submission(clean_input, submission_id, st.session_state, repo)
                if success:
                    st.session_state.pop("pending_submission_id", None)
                    st.session_state["current_page"] = "Infrastructure Intent"
                    st.rerun()
            except ConfigurationError as e:
                st.error(f"Configuration Error: {e}")
            except ValidationError as e:
                st.error(f"Validation Error: {e}")
            except AIParserError as e:
                st.error(f"AI Interpretation Error: {e}")
            except Exception as e:
                st.error(f"Unexpected Error: {e}")

    # Recent Conversation showing only the latest 2-3 messages
    all_msgs = st.session_state.get("chat_messages", [])
    recent_msgs = all_msgs[-3:] if len(all_msgs) > 3 else all_msgs
    if recent_msgs:
        st.markdown("<hr style='margin-top: 20px; margin-bottom: 14px;' />", unsafe_allow_html=True)
        st.markdown(
            "<div style='font-size: 13px; font-weight: 600; color: #94a3b8; margin-bottom: 8px;'>Recent Conversation:</div>",
            unsafe_allow_html=True,
        )
        for msg in recent_msgs:
            formatted_body = _format_chat_content(msg['content'])
            if msg["role"] == "user":
                st.markdown(
                    f"<div class='chat-user' style='margin-bottom: 8px;'><b>You:</b><div style='margin-top: 4px;'>{formatted_body}</div></div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<div class='chat-ai' style='margin-bottom: 8px;'><b>Cloud-Chat Assistant:</b><div style='margin-top: 4px;'>{formatted_body}</div></div>",
                    unsafe_allow_html=True,
                )
