import re
from typing import List, Optional
import streamlit as st
from core.models import DeploymentMode, InterpretedInfrastructure, SubnetClassification
from database.repository import DeploymentRepository
from services.aws_auth_service import aws_auth_service
from services.dependency_resolver import DependencyResolver
from ui.theme import render_top_header


def recalculate_missing_details(
    missing_info: Optional[List[str]],
    vpc_id: Optional[str],
    subnet_id: Optional[str],
    is_vpc_valid: bool = True,
    is_subnet_valid: bool = True,
    has_ssh: bool = False,
    ssh_input: Optional[str] = None,
    is_ssh_valid: bool = True,
) -> List[str]:
    """Recalculates missing details list dynamically from currently validated VPC and subnet selections.

    Removes VPC and Subnet missing detail items as soon as valid IDs are present.
    Adds them if IDs are missing or invalid.
    Preserves any other non-network missing details.
    """
    cleaned: List[str] = []

    # Preserve any non-network missing info items from original intent
    if missing_info:
        for m in missing_info:
            low = m.lower()
            if "vpc" in low or "subnet" in low or "ssh" in low or "networking" in low or "deployment mode" in low:
                continue
            cleaned.append(m)

    # VPC missing check
    if not vpc_id or not is_vpc_valid:
        cleaned.append("Existing VPC ID or selection required")

    # Subnet missing check
    if not subnet_id or not is_subnet_valid:
        cleaned.append("Existing Subnet ID or selection required")

    # SSH missing check
    if has_ssh and (not ssh_input or not is_ssh_valid):
        cleaned.append("Trusted SSH Ingress CIDR required")

    return cleaned


def render_intent_view():
    """Renders Page 3: Infrastructure Intent / Parsed Output."""
    render_top_header("Infrastructure Intent")

    interpreted: Optional[InterpretedInfrastructure] = st.session_state.get("active_interpreted_infra")

    if not interpreted:
        st.info("No active infrastructure intent found. Please submit a request in **New Deployment** first.")
        if st.button("➔ Go to New Deployment", type="primary"):
            st.session_state["current_page"] = "New Deployment"
            st.rerun()
        return

    st.markdown("Review the structured requirements interpreted by the AI before cost estimation and planning.")

    # Clarification Callout if needed
    if interpreted.requires_clarification:
        st.warning(f"⚠️ **Clarification Needed**: {interpreted.clarification_question or 'Missing critical details.'}")

    # Synchronize authenticated AWS credentials from session state if available
    active_identity = aws_auth_service.get_active_identity()
    if not aws_auth_service.get_active_credentials() and "aws_credentials" in st.session_state:
        aws_auth_service.set_active_credentials(
            st.session_state["aws_credentials"],
            st.session_state.get("aws_identity"),
        )
        active_identity = aws_auth_service.get_active_identity()

    if active_identity and active_identity.is_valid and not aws_auth_service.get_active_credentials():
        st.error(
            f"⚠️ **AWS Session Expired**: Active session for `{active_identity.profile_name or active_identity.account_id}` "
            "has expired or credentials need to be reloaded. Please visit **Settings** to re-authenticate."
        )

    res = interpreted.resources[0] if interpreted.resources else None
    dep_resolver = DependencyResolver(auth_service=aws_auth_service)

    # Track validation errors and suitability errors separately
    validation_errors: List[str] = []
    suitability_errors: List[str] = []

    st.markdown(
        """
        <div class="cloud-card" style="border-left: 4px solid #f59e0b; margin-bottom: 20px;">
            <div style="font-weight: 700; color: #fbbf24; font-size: 16px; margin-bottom: 6px;">
                🔧 Network Infrastructure Requirements
            </div>
            <div style="font-size: 13px; color: #cbd5e1;">
                Every EC2 deployment must be explicitly assigned to an existing VPC and Subnet. Default VPCs and auto-selected subnets are never silently assumed.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 0. Deployment Networking Architecture Mode Selection
    cur_mode = res.network.deployment_mode.value if (res and hasattr(res.network, "deployment_mode")) else "PRIVATE"
    mode_options = ["PRIVATE", "PUBLIC"]
    mode_idx = mode_options.index(cur_mode) if cur_mode in mode_options else 0

    mode_col1, mode_col2 = st.columns([1.5, 2.5])
    with mode_col1:
        chosen_mode = st.radio(
            "🌐 Deployment Networking Mode *",
            options=mode_options,
            index=mode_idx,
            horizontal=True,
            help="PUBLIC: Internet-facing workload in a public subnet with an Internet Gateway. PRIVATE: Internal workload in a private or isolated subnet with zero public IP exposure.",
            key="intent_deployment_mode_radio",
        )
        if res:
            res.network.deployment_mode = DeploymentMode(chosen_mode)

    is_private_workload = (chosen_mode == "PRIVATE")

    with mode_col2:
        if not is_private_workload:
            allow_pub_ip = st.checkbox(
                "Assign Public IPv4 Address",
                value=res.network.public_ip if res else True,
                help="Auto-assigns a public IPv4 address to the instance on launch. Requires an active Internet Gateway.",
                key="intent_public_ip_toggle",
            )
            if res:
                res.network.public_ip = allow_pub_ip
            if allow_pub_ip:
                st.caption("🌐 **Internet Accessible**: Auto-assigned public IPv4 address will be provisioned.")
            else:
                st.caption("🔒 **No Public IPv4**: Instance will not have a public IPv4 address and cannot be accessed directly from the public internet.")
        else:
            if res:
                res.network.public_ip = False
            st.caption("🔒 **Strict Private Mode**: Public IP assignment is permanently DISABLED.")

    in_col1, in_col2 = st.columns(2)

    # 1. VPC Discovery & Selection
    discovered_vpcs = dep_resolver.discover_vpcs(interpreted.region)
    selected_vpc_id = ""
    is_vpc_valid = False

    with in_col1:
        if discovered_vpcs:
            vpc_options = [v["vpc_id"] for v in discovered_vpcs] + ["manual"]
            vpc_labels = {v["vpc_id"]: v["label"] for v in discovered_vpcs}
            vpc_labels["manual"] = "Enter VPC ID manually..."

            active_vpc = st.session_state.get("intent_selected_vpc") or (res.network.vpc_id if res else "")
            default_idx = 0
            if active_vpc in vpc_options:
                default_idx = vpc_options.index(active_vpc)

            chosen_vpc = st.selectbox(
                "Target VPC *",
                options=vpc_options,
                index=default_idx,
                format_func=lambda x: vpc_labels.get(x, x),
                key="intent_vpc_select",
            )
            if chosen_vpc == "manual":
                cur_val = (res.network.vpc_id or "") if res else ""
                selected_vpc_id = st.text_input(
                    "VPC ID *",
                    value=cur_val if cur_val != "manual" else "",
                    placeholder="vpc-0123456789abcdef0",
                    key="intent_vpc_manual_input",
                ).strip()
            else:
                selected_vpc_id = chosen_vpc
        else:
            cur_vpc = st.session_state.get("intent_selected_vpc") or ((res.network.vpc_id or "").strip() if res else "")
            selected_vpc_id = st.text_input(
                "Target VPC ID *",
                value=cur_vpc,
                placeholder="vpc-0123456789abcdef0",
                help="AWS VPC identifier (e.g. vpc-12345678 or vpc-0123456789abcdef0)",
                key="intent_vpc_text_input",
            ).strip()

        if not selected_vpc_id:
            st.error("❌ Target VPC is required.")
        elif not re.match(r"^vpc-[0-9a-fA-F]{8,17}$", selected_vpc_id):
            st.error("❌ Invalid VPC ID format. Expected `vpc-xxxxxxxx` or `vpc-xxxxxxxxxxxxxxxxx`.")
            validation_errors.append("Invalid VPC ID format.")
        else:
            vpc_ok, vpc_err = dep_resolver._validate_vpc(interpreted.region, selected_vpc_id)
            if not vpc_ok:
                st.error(f"❌ {vpc_err}")
                validation_errors.append(vpc_err)
            else:
                is_vpc_valid = True
                st.session_state["intent_selected_vpc"] = selected_vpc_id
                if res:
                    res.network.vpc_id = selected_vpc_id
                    res.network.use_existing_vpc = True

    # Invalidate previous subnet selection if VPC changed
    prev_vpc = st.session_state.get("intent_last_vpc_id")
    if prev_vpc and prev_vpc != selected_vpc_id:
        st.session_state["intent_selected_subnet"] = ""
        if res:
            res.network.subnet_id = ""
            res.network.use_existing_subnet = False
        if "intent_subnet_select" in st.session_state:
            del st.session_state["intent_subnet_select"]
        if "intent_subnet_manual_input" in st.session_state:
            del st.session_state["intent_subnet_manual_input"]
        if "intent_subnet_text_input" in st.session_state:
            del st.session_state["intent_subnet_text_input"]
    st.session_state["intent_last_vpc_id"] = selected_vpc_id

    # 2. Subnet Discovery & Selection
    selected_subnet_id = ""
    is_subnet_valid = False
    subnet_vpc_valid = False

    with in_col2:
        sub_top_col1, sub_top_col2 = st.columns([3, 1])
        with sub_top_col1:
            st.caption("Subnet Classification (Read-Only AWS Discovery)")
        with sub_top_col2:
            if st.button("🔄 Refresh", key="btn_refresh_subnets", help="Re-query AWS subnets and route tables"):
                dep_resolver.clear_cache()
                st.rerun()

        discovered_subnets = dep_resolver.discover_subnets(interpreted.region, vpc_id=selected_vpc_id if selected_vpc_id else None)
        if discovered_subnets:
            subnet_options = [""] + [s["subnet_id"] for s in discovered_subnets] + ["manual"]
            subnet_labels = {s["subnet_id"]: s["label"] for s in discovered_subnets}
            subnet_labels[""] = "-- Select a Subnet --"
            subnet_labels["manual"] = "Enter Subnet ID manually..."

            active_sub = st.session_state.get("intent_selected_subnet") or (res.network.subnet_id if res else "")
            default_s_idx = 0
            if active_sub and active_sub in subnet_options:
                default_s_idx = subnet_options.index(active_sub)

            chosen_subnet = st.selectbox(
                "Target Subnet *",
                options=subnet_options,
                index=default_s_idx,
                format_func=lambda x: subnet_labels.get(x, x),
                key="intent_subnet_select",
            )
            if chosen_subnet == "manual":
                cur_s_val = (res.network.subnet_id or "") if res else ""
                selected_subnet_id = st.text_input(
                    "Subnet ID *",
                    value=cur_s_val if cur_s_val != "manual" else "",
                    placeholder="subnet-0123456789abcdef0",
                    key="intent_subnet_manual_input",
                ).strip()
            else:
                selected_subnet_id = chosen_subnet

            # Missing infrastructure check against discovered subnets in VPC
            if is_private_workload:
                has_private_subnets = any(
                    s.get("classification") in (
                        SubnetClassification.PRIVATE_WITH_NAT.value,
                        SubnetClassification.PRIVATE_WITH_EGRESS_ONLY_IGW.value,
                        SubnetClassification.ISOLATED.value,
                    )
                    for s in discovered_subnets
                )
                if not has_private_subnets:
                    suit_msg = (
                        f"❌ **Missing Infrastructure**: No suitable private subnets found in VPC `{selected_vpc_id}`. "
                        f"All {len(discovered_subnets)} discovered subnet(s) are **PUBLIC** (default route to an Internet Gateway). "
                        "A private workload requires an existing **PRIVATE_WITH_NAT**, **PRIVATE_WITH_EGRESS_ONLY_IGW**, or **ISOLATED** subnet. "
                        "Cloud-Chat V2 operates strictly under read-only discovery guardrails and will never automatically create "
                        "subnets, route tables, or NAT Gateways."
                    )
                    st.error(suit_msg)
            else:
                has_public_subnets = any(
                    s.get("classification") == SubnetClassification.PUBLIC.value
                    for s in discovered_subnets
                )
                if not has_public_subnets:
                    suit_msg = (
                        f"❌ **Missing Infrastructure**: No suitable public subnets found in VPC `{selected_vpc_id}`. "
                        f"A PUBLIC workload requires an existing **PUBLIC** subnet with an active default route to an Internet Gateway (`igw-*`). "
                        "Cloud-Chat V2 operates strictly under read-only discovery guardrails and will never automatically create "
                        "subnets, route tables, or Internet Gateways."
                    )
                    st.error(suit_msg)

        else:
            cur_subnet = st.session_state.get("intent_selected_subnet") or ((res.network.subnet_id or "").strip() if res else "")
            selected_subnet_id = st.text_input(
                "Target Subnet ID *",
                value=cur_subnet,
                placeholder="subnet-0123456789abcdef0",
                help="AWS Subnet identifier (e.g. subnet-12345678 or subnet-0123456789abcdef0)",
                key="intent_subnet_text_input",
            ).strip()

        # Strict Subnet Validation & Classification (identical for dropdown and manual IDs)
        if not selected_subnet_id:
            st.error("❌ Target Subnet is required.")
        elif not re.match(r"^subnet-[0-9a-fA-F]{8,17}$", selected_subnet_id):
            st.error("❌ Invalid Subnet ID format. Expected `subnet-xxxxxxxx` or `subnet-xxxxxxxxxxxxxxxxx`.")
            validation_errors.append("Invalid Subnet ID format.")
        else:
            sub_ok, classification, sub_details, expl = dep_resolver.inspect_and_classify_subnet(
                interpreted.region,
                selected_subnet_id,
                expected_vpc_id=selected_vpc_id if is_vpc_valid else None,
            )
            if not sub_ok:
                st.error(f"❌ {expl}")
                validation_errors.append(expl)
            else:
                is_subnet_valid = True
                subnet_vpc_valid = True
                st.session_state["intent_selected_subnet"] = selected_subnet_id
                if res:
                    res.network.subnet_id = selected_subnet_id
                    res.network.use_existing_subnet = True

                cls_val = classification.value
                rt_id = (sub_details or {}).get("route_table_id") or "N/A"
                map_pub = "Yes" if (sub_details or {}).get("map_public_ip_on_launch") else "No"

                if is_private_workload:
                    if classification == SubnetClassification.PUBLIC:
                        unsuit_msg = (
                            f"❌ **Unsuitable Subnet**: Subnet `{selected_subnet_id}` is classified as **PUBLIC** ({expl}). "
                            "A private EC2 workload must not be placed in a public subnet. Please select a `PRIVATE_WITH_NAT`, `PRIVATE_WITH_EGRESS_ONLY_IGW`, or `ISOLATED` subnet."
                        )
                        st.error(unsuit_msg)
                        suitability_errors.append(f"Subnet '{selected_subnet_id}' is PUBLIC and unsuitable for a private workload.")
                    elif classification == SubnetClassification.UNKNOWN:
                        unsuit_msg = (
                            f"❌ **Unsuitable Subnet**: Subnet `{selected_subnet_id}` routing classification is **UNKNOWN** ({expl}). "
                            "A private EC2 workload requires a verified `PRIVATE_WITH_NAT`, `PRIVATE_WITH_EGRESS_ONLY_IGW`, or `ISOLATED` subnet."
                        )
                        st.error(unsuit_msg)
                        suitability_errors.append(f"Subnet '{selected_subnet_id}' classification is UNKNOWN and cannot be verified as private.")
                    elif classification == SubnetClassification.PRIVATE_WITH_NAT:
                        st.success(f"✅ Subnet `{selected_subnet_id}` is **PRIVATE_WITH_NAT** ({expl}). Outbound internet access enabled via NAT Gateway; zero inbound public exposure.")
                    elif classification == SubnetClassification.PRIVATE_WITH_EGRESS_ONLY_IGW:
                        st.success(f"✅ Subnet `{selected_subnet_id}` is **PRIVATE_WITH_EGRESS_ONLY_IGW** ({expl}). Outbound IPv6 access enabled; zero inbound public exposure.")
                    elif classification == SubnetClassification.ISOLATED:
                        st.success(f"✅ Subnet `{selected_subnet_id}` is **ISOLATED** ({expl}). Pure internal routing with zero inbound or outbound internet access.")
                    else:
                        st.info(f"ℹ️ Route Table: `{rt_id}` | Classification: **{cls_val}** | MapPublicIP: `{map_pub}`\n*{expl}*")
                else:
                    # PUBLIC workload
                    if classification == SubnetClassification.PUBLIC:
                        st.success(f"✅ Subnet `{selected_subnet_id}` is **PUBLIC** ({expl}). Suitable for PUBLIC deployment mode.")
                    elif classification in (
                        SubnetClassification.PRIVATE_WITH_NAT,
                        SubnetClassification.PRIVATE_WITH_EGRESS_ONLY_IGW,
                        SubnetClassification.ISOLATED,
                    ):
                        unsuit_msg = (
                            f"❌ **Unsuitable Subnet**: Subnet `{selected_subnet_id}` is classified as **{classification.value}** ({expl}). "
                            "A PUBLIC deployment requires a verified PUBLIC subnet with an active default route to an Internet Gateway (`igw-*`)."
                        )
                        st.error(unsuit_msg)
                        suitability_errors.append(f"Subnet '{selected_subnet_id}' is {classification.value} and cannot be used for a PUBLIC deployment.")
                    else:
                        unsuit_msg = (
                            f"❌ **Unsuitable Subnet**: Subnet `{selected_subnet_id}` routing classification is **UNKNOWN** ({expl}). "
                            "A PUBLIC deployment requires a verified PUBLIC subnet."
                        )
                        st.error(unsuit_msg)
                        suitability_errors.append(f"Subnet '{selected_subnet_id}' classification is UNKNOWN and cannot be verified as public.")

    # 3. Verify Subnet Belongs to Selected VPC (if not already verified by inspect_and_classify_subnet)
    if is_vpc_valid and is_subnet_valid and not subnet_vpc_valid and not validation_errors:
        sub_valid, sub_msg = dep_resolver._validate_subnet(
            interpreted.region, selected_subnet_id, selected_vpc_id, require_private=is_private_workload
        )
        if not sub_valid:
            st.error(f"❌ {sub_msg}")
            validation_errors.append(sub_msg)
        else:
            subnet_vpc_valid = True

    # 4. Inbound Network Security (SSH Access)
    st.markdown("##### 🛡️ Inbound Network Security")
    has_ssh = bool(res and res.network.allowed_ssh_cidrs)
    allow_ssh = st.checkbox(
        "Configure SSH Ingress (Port 22)",
        value=has_ssh,
        help="Default is completely private: zero inbound access. If enabled, you must provide a trusted administrator CIDR.",
        key="intent_ssh_toggle",
    )
    is_ssh_valid = True
    ssh_input = ""
    if allow_ssh:
        curr_cidr = ", ".join(res.network.allowed_ssh_cidrs) if (res and res.network.allowed_ssh_cidrs) else ""
        ssh_input = st.text_input(
            "Trusted SSH Ingress CIDR *",
            value=curr_cidr,
            placeholder="e.g. 203.0.113.50/32 or 10.0.0.0/16",
            help="Unrestricted 0.0.0.0/0 is strictly prohibited by security guardrails. Provide a specific trusted IP range.",
            key="intent_ssh_cidr_input",
        ).strip()
        if not ssh_input:
            st.error("❌ A trusted CIDR is required when SSH access is enabled.")
            is_ssh_valid = False
        elif ssh_input in ("0.0.0.0/0", "::/0"):
            st.error("❌ Unrestricted SSH access (0.0.0.0/0) is strictly prohibited. Provide a specific administrator CIDR.")
            is_ssh_valid = False
            validation_errors.append("Unrestricted SSH prohibited.")
        elif not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$", ssh_input):
            st.error("❌ Invalid CIDR notation. Expected format like `192.168.1.0/24` or `10.0.0.0/16`.")
            is_ssh_valid = False
            validation_errors.append("Invalid SSH CIDR format.")
        else:
            if res:
                res.network.allowed_ssh_cidrs = [ssh_input]
    else:
        if res:
            res.network.allowed_ssh_cidrs = []
        st.caption("🔒 **Private Mode**: Zero inbound ports or protocols will be exposed.")

    # 5. Dynamic Recalculation of Missing Details on Every Rerun
    missing_details = recalculate_missing_details(
        missing_info=interpreted.missing_information,
        vpc_id=selected_vpc_id,
        subnet_id=selected_subnet_id,
        is_vpc_valid=is_vpc_valid,
        is_subnet_valid=(is_subnet_valid and (subnet_vpc_valid if (selected_vpc_id and selected_subnet_id) else False)),
        has_ssh=allow_ssh,
        ssh_input=ssh_input,
        is_ssh_valid=is_ssh_valid,
    )
    interpreted.missing_information = missing_details
    if not missing_details:
        interpreted.requires_clarification = False
        interpreted.clarification_question = None

    # Always persist updated selections to session state
    st.session_state["active_interpreted_infra"] = interpreted

    # Update SQLite repository with latest parsed_intent
    dep_id = st.session_state.get("active_deployment_id")
    if dep_id:
        try:
            repo = DeploymentRepository()
            conn = repo._get_conn()
            import json
            with conn:
                conn.execute(
                    "UPDATE deployments SET parsed_intent = ?, updated_at = CURRENT_TIMESTAMP WHERE deployment_id = ?",
                    (json.dumps(interpreted.model_dump()), dep_id)
                )
            conn.close()
        except Exception:
            pass

    # Structured Summary Card
    st.markdown("<div class='cloud-card'>", unsafe_allow_html=True)
    st.subheader("Structured Requirements Summary")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Service", interpreted.service.value.upper())
    m2.metric("Region", interpreted.region)
    m3.metric("Instance Type", res.instance_type if res else "N/A")
    m4.metric("Storage", f"{res.storage.size_gb if res else 20} GiB ({res.storage.volume_type.value if res else 'gp3'})")

    st.markdown("<hr style='margin: 16px 0;' />", unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### Compute & Sizing")
        st.write(f"- **Resource Name**: `{res.name if res else 'app-server'}`")
        st.write(f"- **Instance Sizing**: `{res.instance_type if res else 't3.micro'}`")
        st.write(f"- **AMI**: `{res.ami_id or 'Automatic resolution (Amazon Linux 2023)'}`")

        st.markdown("##### Networking")
        mode_val = res.network.deployment_mode.value if (res and hasattr(res.network, "deployment_mode")) else ("PRIVATE" if is_private_workload else "PUBLIC")
        st.write(f"- **Deployment Mode**: `{'🔒 PRIVATE' if is_private_workload else '🌐 PUBLIC'}`")
        pub_status = "ENABLED" if (res and res.network.public_ip) else "DISABLED (Private)"
        st.write(f"- **Public IP**: `{pub_status}`")
        vpc_ref = res.network.vpc_id if (res and res.network.vpc_id) else ("⚠️ Existing VPC (ID required)" if (res and res.network.use_existing_vpc) else "Default VPC")
        subnet_ref = res.network.subnet_id if (res and res.network.subnet_id) else ("⚠️ Existing Subnet (ID required)" if (res and res.network.use_existing_subnet) else "Auto-selected subnet")
        st.write(f"- **VPC Reference**: `{vpc_ref}`")
        st.write(f"- **Subnet Reference**: `{subnet_ref}`")

    with c2:
        st.markdown("##### Storage (EBS)")
        st.write(f"- **Volume Size**: `{res.storage.size_gb if res else 20} GiB`")
        st.write(f"- **Volume Type**: `{res.storage.volume_type.value if res else 'gp3'}`")
        enc_status = "ENCRYPTED (KMS)" if (res and res.storage.encrypted) else "UNENCRYPTED"
        st.write(f"- **Encryption**: `{enc_status}`")

        if res and res.tags:
            st.markdown("##### Tags")
            st.json(res.tags)

    st.markdown("</div>", unsafe_allow_html=True)

    # Applied Assumptions & Missing Info
    if interpreted.assumptions:
        st.markdown("##### Applied Safe Defaults & Assumptions")
        for a in interpreted.assumptions:
            st.write(f"- ℹ️ *{a}*")

    if interpreted.missing_information:
        st.markdown("##### Missing Details")
        for m in interpreted.missing_information:
            st.write(f"- ⚠️ *{m}*")

    # Action Buttons
    st.divider()
    bcol1, bcol2, bcol3 = st.columns([1, 1.8, 2.7])
    with bcol1:
        if st.button("⬅ Edit Request"):
            st.session_state["current_page"] = "New Deployment"
            st.rerun()

    has_missing_inputs = bool(interpreted.missing_information)
    has_suitability_errors = bool(suitability_errors)
    has_validation_errors = bool(validation_errors)

    can_continue = (
        not has_missing_inputs
        and not has_suitability_errors
        and not has_validation_errors
        and not interpreted.requires_clarification
    )

    with bcol2:
        if st.button("Confirm & Continue ➔", type="primary", width="stretch", disabled=not can_continue):
            st.session_state["current_page"] = "Cost Estimation"
            st.rerun()

    if not can_continue:
        with bcol3:
            if has_suitability_errors:
                if is_private_workload:
                    st.warning("⚠️ Cannot continue: Selected subnet is PUBLIC or no private subnets exist in this VPC. Private workloads require a verified PRIVATE_WITH_NAT, PRIVATE_WITH_EGRESS_ONLY_IGW, or ISOLATED subnet.")
                else:
                    st.warning("⚠️ Cannot continue: Selected subnet is not PUBLIC or route table does not support internet access. Public workloads require a verified PUBLIC subnet with an active route to an Internet Gateway.")
            elif has_missing_inputs:
                st.warning("⚠️ Please provide all required infrastructure inputs above to continue.")
            else:
                st.warning("⚠️ Please resolve the validation issues above to continue.")
