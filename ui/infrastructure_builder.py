import uuid
import streamlit as st
from config import config
from core.exceptions import AIParserError, ConfigurationError, ValidationError
from core.models import DeploymentRecord, DeploymentStatus, InterpretedInfrastructure
from database.repository import DeploymentRepository
from services.ai_parser import AIInfrastructureParser

PRESET_EXAMPLES = [
    "Create a private EC2 instance in Mumbai with t3.micro, 8 GB encrypted gp3 storage, using my existing VPC and subnet.",
    "Launch an EC2 instance in Virginia using t3.small, 30 GB gp3 storage with encryption, and assign a public IP.",
    "Deploy a t3.medium app server in us-east-2 with 50 GB storage, private networking only, and tag Environment=Production.",
]


def render_infrastructure_builder():
    """Renders the natural-language infrastructure builder."""
    st.title("Infrastructure Builder")
    st.markdown(
        "Describe your desired AWS infrastructure in plain English. "
        "Cloud-Chat will interpret your requirements, apply safe defaults, and generate structured configurations."
    )

    st.divider()

    # AI Provider selector & status bar
    c1, c2 = st.columns([1, 2])
    with c1:
        provider_options = ["groq", "gemini"]
        current_default = config.default_ai_provider if config.default_ai_provider in provider_options else "groq"
        selected_provider = st.selectbox(
            "AI Provider",
            provider_options,
            index=provider_options.index(current_default),
            help="Choose the active AI provider for interpreting natural language.",
        )
    with c2:
        # Show key status
        if selected_provider == "groq":
            has_key = bool(config.groq_api_key)
            status_text = "Configured" if has_key else "Missing GROQ_API_KEY"
            status_color = "green" if has_key else "orange"
        else:
            has_key = bool(config.gemini_api_key)
            status_text = "Configured" if has_key else "Missing GEMINI_API_KEY"
            status_color = "green" if has_key else "orange"

        st.markdown(
            f"<div style='margin-top:28px;'>Provider Status: <b style='color:{status_color};'>{status_text}</b></div>",
            unsafe_allow_html=True,
        )

    # Example chips
    st.caption("Quick Examples:")
    ecol1, ecol2, ecol3 = st.columns(3)
    with ecol1:
        if st.button("Private EC2 (Mumbai)", width="stretch"):
            st.session_state["nl_prompt_input"] = PRESET_EXAMPLES[0]
            st.rerun()
    with ecol2:
        if st.button("Public t3.small (Virginia)", width="stretch"):
            st.session_state["nl_prompt_input"] = PRESET_EXAMPLES[1]
            st.rerun()
    with ecol3:
        if st.button("Prod t3.medium (Ohio)", width="stretch"):
            st.session_state["nl_prompt_input"] = PRESET_EXAMPLES[2]
            st.rerun()

    # Main text input
    current_prompt = st.session_state.get("nl_prompt_input", "")
    prompt = st.text_area(
        "Describe your infrastructure:",
        value=current_prompt,
        placeholder="Example: Create a private EC2 instance in Mumbai with t3.micro, 8 GB encrypted gp3 storage, using my existing VPC and subnet.",
        height=130,
        key="nl_prompt_input",
    )

    btn_col1, btn_col2 = st.columns([1, 4])
    with btn_col1:
        interpret_clicked = st.button("Interpret Infrastructure", type="primary", width="stretch")
    with btn_col2:
        if st.button("Reset Form", width="content"):
            st.session_state["nl_prompt_input"] = ""
            st.session_state.pop("active_interpreted_infra", None)
            st.rerun()

    # Execution handling
    if interpret_clicked:
        if not prompt.strip():
            st.error("Please enter an infrastructure request to continue.")
            return

        parser = AIInfrastructureParser(provider=selected_provider)
        with st.spinner(f"Interpreting infrastructure request with {selected_provider.upper()}..."):
            try:
                interpreted: InterpretedInfrastructure = parser.parse(prompt)
                st.session_state["active_interpreted_infra"] = interpreted

                # Persist draft deployment to SQLite repository
                deployment_id = str(uuid.uuid4())
                repo = DeploymentRepository()
                record = DeploymentRecord(
                    deployment_id=deployment_id,
                    user_request=prompt.strip(),
                    service=interpreted.service.value,
                    status=DeploymentStatus.DRAFT,
                    parsed_intent=interpreted.model_dump(),
                )
                repo.create(record)
                st.session_state["active_deployment_id"] = deployment_id

                st.success("Infrastructure request interpreted successfully!")
            except ConfigurationError as e:
                st.error(f"Configuration Error: {e}")
                st.info("Tip: You can add API keys in `.env` (e.g. `GROQ_API_KEY=your_key` or `GEMINI_API_KEY=your_key`).")
            except ValidationError as e:
                st.error(f"Validation Error: {e}")
            except AIParserError as e:
                st.error(f"AI Interpretation Error: {e}")
            except Exception as e:
                st.error(f"Unexpected error: {e}")

    # Render Active Interpretation
    interpreted_data: Optional[InterpretedInfrastructure] = st.session_state.get("active_interpreted_infra")
    if interpreted_data:
        st.divider()
        st.subheader("Interpreted Infrastructure Specification")

        # Check if clarification is required
        if interpreted_data.requires_clarification:
            st.warning(f"⚠️ **Clarification Needed**: {interpreted_data.clarification_question or 'Missing essential information.'}")

        # Summary Metrics
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("Target Service", interpreted_data.service.value.upper())
        with m2:
            st.metric("Target Region", interpreted_data.region)
        with m3:
            st.metric("Total Resources", len(interpreted_data.resources))

        # Resources detail
        for idx, res in enumerate(interpreted_data.resources):
            with st.expander(f"Resource #{idx + 1}: {res.name} ({res.instance_type})", expanded=True):
                rcol1, rcol2 = st.columns(2)
                with rcol1:
                    st.markdown("##### Compute & Sizing")
                    st.write(f"- **Instance Type**: `{res.instance_type}`")
                    st.write(f"- **Name Tag**: `{res.name}`")
                    if res.ami_id:
                        st.write(f"- **AMI ID**: `{res.ami_id}`")
                    if res.key_name:
                        st.write(f"- **Key Pair**: `{res.key_name}`")

                    st.markdown("##### Networking")
                    pub_ip_color = "red" if res.network.public_ip else "green"
                    st.markdown(
                        f"- **Public IP**: <span style='color:{pub_ip_color}; font-weight:bold;'>"
                        f"{'ENABLED' if res.network.public_ip else 'DISABLED (Private)'}</span>",
                        unsafe_allow_html=True,
                    )
                    vpc_ref = res.network.vpc_id if res.network.vpc_id else ("⚠️ Existing VPC (ID required)" if res.network.use_existing_vpc else "Default VPC")
                    subnet_ref = res.network.subnet_id if res.network.subnet_id else ("⚠️ Existing Subnet (ID required)" if res.network.use_existing_subnet else "Auto-selected private subnet")
                    st.write(f"- **VPC Reference**: `{vpc_ref}`")
                    st.write(f"- **Subnet Reference**: `{subnet_ref}`")

                with rcol2:
                    st.markdown("##### Storage (EBS)")
                    st.write(f"- **Size**: `{res.storage.size_gb} GiB`")
                    st.write(f"- **Volume Type**: `{res.storage.volume_type.value}`")
                    enc_color = "green" if res.storage.encrypted else "red"
                    st.markdown(
                        f"- **Encryption**: <span style='color:{enc_color}; font-weight:bold;'>"
                        f"{'ENCRYPTED (KMS)' if res.storage.encrypted else 'UNENCRYPTED'}</span>",
                        unsafe_allow_html=True,
                    )

                    if res.tags:
                        st.markdown("##### Tags")
                        st.json(res.tags)

        # Assumptions & missing details
        if interpreted_data.assumptions:
            st.markdown("##### Applied Safe Defaults & Assumptions")
            for a in interpreted_data.assumptions:
                st.markdown(f"- ℹ️ *{a}*")

        if interpreted_data.missing_information:
            st.markdown("##### Missing Details")
            for m in interpreted_data.missing_information:
                st.markdown(f"- ⚠️ *{m}*")

        # Next Step Action Button
        st.divider()
        act_col1, act_col2 = st.columns([1, 4])
        with act_col1:
            if st.button("Proceed to Planning ➔", type="primary", width="stretch"):
                st.session_state["current_page"] = "Deployment Plans"
                st.rerun()
        with act_col2:
            st.caption("Proceed to Milestone 3 / Milestone 4 to generate normalized Terraform configuration and security checks.")
