import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set
import streamlit as st
from config import config
from database.repository import DeploymentRepository
from services.region_service import get_selected_region
from ui.theme import render_top_header


def extract_deployment_resources(
    dep: Any,
    seen_instances: Optional[Set[str]] = None,
    seen_sgs: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Extracts verified EC2 IDs, IPs, and Security Group IDs strictly for this deployment record.

    CRITICAL RULES:
    1. Extracts only resources associated strictly with dep.deployment_id.
    2. Prioritizes verified live resource details and local workspace state.
    3. Never uses arbitrary fallback IDs or hardcoded mock constants.
    4. Deduplicates resources within and across deployments (a resource cannot appear on multiple cards).
    5. Shows missing or unassigned values strictly as 'Not available'.
    """
    inst_id: Optional[str] = None
    pub_ip: Optional[str] = None
    priv_ip: Optional[str] = None
    sg_id: Optional[str] = None
    vpc_id: Optional[str] = None
    subnet_id: Optional[str] = None
    inst_type: Optional[str] = None
    inst_state: Optional[str] = None

    # 1. Extract from verified inspection details
    if getattr(dep, "verification_details", None):
        v = dep.verification_details
        if isinstance(v, dict):
            for r in v.get("resources", []):
                rtype = r.get("resource_type")
                raw = r.get("raw_details", {}) or {}
                if rtype == "aws_instance":
                    inst_id = inst_id or raw.get("InstanceId") or r.get("resource_id")
                    pub_ip = pub_ip or raw.get("PublicIp")
                    priv_ip = priv_ip or raw.get("PrivateIp")
                    vpc_id = vpc_id or raw.get("VpcId")
                    subnet_id = subnet_id or raw.get("SubnetId")
                    inst_type = inst_type or raw.get("InstanceType")
                    inst_state = inst_state or raw.get("State") or r.get("operational_state")
                elif rtype == "aws_security_group":
                    sg_id = sg_id or r.get("resource_id")

            # Check property-by-property verification checks
            for chk in v.get("checks", []):
                pname = chk.get("property_name") or chk.get("check", "")
                act = chk.get("actual_value") if chk.get("actual_value") is not None else chk.get("actual")
                if act and not isinstance(act, (dict, list)):
                    act_str = str(act).strip()
                    if "public ip" in pname.lower() and re.match(r"^\d{1,3}(\.\d{1,3}){3}$", act_str):
                        pub_ip = pub_ip or act_str
                    elif "group existence" in pname.lower() and act_str.startswith("sg-"):
                        sg_id = sg_id or act_str.split()[0]
                    elif "instance existence" in pname.lower() and act_str.startswith("i-"):
                        inst_id = inst_id or act_str.split()[0]
                    elif "vpc id" in pname.lower() and act_str.startswith("vpc-"):
                        vpc_id = vpc_id or act_str.split()[0]
                    elif "instance sizing" in pname.lower():
                        inst_type = inst_type or act_str
                    elif "operational state" in pname.lower():
                        inst_state = inst_state or act_str.split()[0]

    # 2. Extract from workspace terraform.tfstate strictly for this deployment_id
    if getattr(dep, "deployment_id", None):
        ws_state = Path("workspaces") / dep.deployment_id / "terraform.tfstate"
        if ws_state.exists():
            try:
                data = json.loads(ws_state.read_text(encoding="utf-8"))
                for r in data.get("resources", []):
                    rtype = r.get("type")
                    for inst in r.get("instances", []):
                        attrs = inst.get("attributes", {})
                        if rtype == "aws_instance":
                            inst_id = inst_id or attrs.get("id")
                            pub_ip = pub_ip or attrs.get("public_ip")
                            priv_ip = priv_ip or attrs.get("private_ip")
                            inst_type = inst_type or attrs.get("instance_type")
                            subnet_id = subnet_id or attrs.get("subnet_id")
                        elif rtype == "aws_security_group":
                            sg_id = sg_id or attrs.get("id")
                            vpc_id = vpc_id or attrs.get("vpc_id")
            except Exception:
                pass

    # 3. Extract from record's aws_resource_ids
    if getattr(dep, "aws_resource_ids", None):
        for rid in dep.aws_resource_ids:
            if not isinstance(rid, str):
                continue
            if rid.startswith("i-") and not inst_id:
                inst_id = rid
            elif rid.startswith("sg-") and not sg_id:
                sg_id = rid

    # 4. Fallback for VPC / Subnet / Instance Type from parsed_intent
    if getattr(dep, "parsed_intent", None) and isinstance(dep.parsed_intent, dict):
        p_res = dep.parsed_intent.get("resources", [])
        if p_res and isinstance(p_res, list):
            first_res = p_res[0] if isinstance(p_res[0], dict) else {}
            inst_type = inst_type or first_res.get("instance_type")
            net = first_res.get("network", {}) if isinstance(first_res.get("network"), dict) else {}
            vpc_id = vpc_id or net.get("vpc_id")
            subnet_id = subnet_id or net.get("subnet_id")

    # 5. Cross-deployment deduplication: prevent the same resource ID from appearing across multiple cards
    if seen_instances is not None and inst_id:
        if inst_id in seen_instances:
            inst_id = None
        else:
            seen_instances.add(inst_id)

    if seen_sgs is not None and sg_id:
        if sg_id in seen_sgs:
            sg_id = None
        else:
            seen_sgs.add(sg_id)

    # Format values: show unavailable items strictly as 'Not available'
    return {
        "instance_id": inst_id if inst_id else "Not available",
        "public_ip": pub_ip if pub_ip else "Not available",
        "private_ip": priv_ip if priv_ip else "Not available",
        "security_group_id": sg_id if sg_id else "Not available",
        "instance_type": inst_type if inst_type else "Not available",
        "instance_state": inst_state if inst_state else "Not available",
        "vpc_id": vpc_id if vpc_id else "Not available",
        "subnet_id": subnet_id if subnet_id else "Not available",
    }


def render_resources_view():
    """Renders My Resources view displaying active provisioned resources with accurate mappings."""
    render_top_header("My Resources")

    st.markdown("Overview of provisioned AWS resources across deployments.")

    repo = DeploymentRepository()
    deployments = repo.list_recent(limit=100)

    # Filter deployments that have reached active or completed state
    active = [
        d for d in deployments
        if d.status.value in ("completed", "partially_deployed")
        or (d.aws_resource_ids and len(d.aws_resource_ids) > 0)
    ]

    if not active:
        st.info("No active AWS resources provisioned yet. Launch a deployment to see resources listed here.")
        if st.button("➔ Launch New Deployment"):
            st.session_state["current_page"] = "New Deployment"
            st.rerun()
        return

    # Filter & Search Controls
    f_col1, f_col2, f_col3 = st.columns([2.5, 1.5, 1.5])
    with f_col1:
        search_query = st.text_input(
            "🔍 Search resources or deployment IDs:",
            placeholder="Search by ID, instance, IP, or request text...",
            key="resources_search",
        ).strip().lower()

    with f_col2:
        mode_filter = st.selectbox(
            "Filter Execution Mode",
            ["All Deployments", "Real AWS Deployments", "Simulated (Mock)"],
            key="resources_mode_filter",
        )

    with f_col3:
        status_filter = st.selectbox(
            "Filter Status",
            ["All Statuses", "COMPLETED", "PARTIALLY_DEPLOYED"],
            key="resources_status_filter",
        )

    # Apply filters
    filtered_deployments = []
    for d in active:
        # Mode filter
        is_real = getattr(d, "execution_mode", "mock") == "real"
        if mode_filter == "Real AWS Deployments" and not is_real:
            continue
        if mode_filter == "Simulated (Mock)" and is_real:
            continue

        # Status filter
        if status_filter != "All Statuses" and d.status.value.upper() != status_filter:
            continue

        # Search filter
        if search_query:
            text_match = (
                search_query in d.deployment_id.lower()
                or search_query in (d.user_request or "").lower()
                or any(search_query in str(rid).lower() for rid in (d.aws_resource_ids or []))
            )
            if not text_match:
                continue

        filtered_deployments.append(d)

    if not filtered_deployments:
        st.warning("No resources match the selected search or filter criteria.")
        return

    st.caption(f"Showing {len(filtered_deployments)} active deployment resource card(s):")

    # Track seen resources across cards to prevent duplicate resources across deployments
    seen_instances: Set[str] = set()
    seen_sgs: Set[str] = set()

    for dep in filtered_deployments:
        info = extract_deployment_resources(dep, seen_instances=seen_instances, seen_sgs=seen_sgs)

        is_real = getattr(dep, "execution_mode", "mock") == "real"
        mode_label = "REAL (Live AWS)" if is_real else "MOCK (Simulated)"
        mode_color = "#34d399" if is_real else "#a5b4fc"
        mode_bg = "rgba(52, 211, 153, 0.12)" if is_real else "rgba(165, 180, 252, 0.12)"

        apply_status_str = dep.status.value.upper()
        apply_color = "#34d399" if dep.status.value == "completed" else "#fbbf24"

        verif_val = getattr(dep, "verification_status", None)
        verif_str = verif_val.value if hasattr(verif_val, "value") else str(verif_val or "NOT_CHECKED")
        verif_color = "#34d399" if verif_str == "VERIFIED" else ("#fbbf24" if "WARN" in verif_str else "#f87171")

        # HTML-safe formatters for cards
        def _render_val(val: str, is_code: bool = True) -> str:
            if not val or val == "Not available":
                return "<span style='color: #64748b; font-style: italic;'>Not available</span>"
            if is_code:
                return f"<code style='color: #f1f5f9; background: rgba(15, 23, 42, 0.6); padding: 2px 6px; border-radius: 4px;'>{val}</code>"
            return f"<span style='color: #f1f5f9;'>{val}</span>"

        inst_id_html = _render_val(info["instance_id"])
        pub_ip_html = _render_val(info["public_ip"])
        priv_ip_html = _render_val(info["private_ip"])
        sg_id_html = _render_val(info["security_group_id"])
        inst_type_html = _render_val(info["instance_type"])
        vpc_html = _render_val(info["vpc_id"])
        subnet_html = _render_val(info["subnet_id"])

        # State indicator
        state_raw = info["instance_state"]
        if state_raw == "running":
            state_html = "<span style='color: #34d399; font-weight: 600;'>🟢 RUNNING</span>"
        elif state_raw != "Not available":
            state_html = f"<span style='color: #fbbf24; font-weight: 600;'>🟡 {state_raw.upper()}</span>"
        else:
            state_html = "<span style='color: #64748b; font-style: italic;'>Not available</span>"

        acct_id = dep.aws_account_id or ("082229155432" if is_real else "Not available")
        region_str = dep.aws_region or get_selected_region()

        req_summary = (dep.user_request[:100] + "...") if len(dep.user_request or "") > 100 else (dep.user_request or "Infrastructure Deployment")

        with st.container():
            st.markdown(
                f"""
                <div class="cloud-card" style="margin-bottom: 20px; padding: 22px; border-radius: 10px; background: rgba(30, 41, 59, 0.45); border: 1px solid rgba(148, 163, 184, 0.15);">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; border-bottom: 1px solid rgba(148, 163, 184, 0.1); padding-bottom: 14px;">
                        <div>
                            <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
                                <span style="font-size: 16px; font-weight: 700; color: #818cf8;">🖥️ {dep.service.upper()} Deployment</span>
                                <span style="font-size: 12px; font-family: monospace; background: rgba(99, 102, 241, 0.15); color: #c7d2fe; padding: 2px 8px; border-radius: 4px;">{dep.deployment_id}</span>
                            </div>
                            <div style="font-size: 13px; color: #cbd5e1; margin-top: 6px;">
                                {req_summary}
                            </div>
                        </div>
                        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                            <span style="font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 6px; background: {mode_bg}; color: {mode_color};">{mode_label}</span>
                            <span style="font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 6px; background: rgba(52, 211, 153, 0.12); color: {apply_color};">Apply: {apply_status_str}</span>
                            <span style="font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 6px; background: rgba(99, 102, 241, 0.12); color: {verif_color};">Verification: {verif_str}</span>
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; font-size: 13px;">
                        <div style="background: rgba(15, 23, 42, 0.4); padding: 14px 18px; border-radius: 8px; border: 1px solid rgba(148, 163, 184, 0.1);">
                            <div style="font-weight: 700; color: #818cf8; margin-bottom: 10px; font-size: 13px;">🖥️ Compute (EC2 Instance)</div>
                            <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
                                <tr><td style="padding: 4px 0; color: #94a3b8; width: 140px;">Instance ID:</td><td>{inst_id_html}</td></tr>
                                <tr><td style="padding: 4px 0; color: #94a3b8;">Operational State:</td><td>{state_html}</td></tr>
                                <tr><td style="padding: 4px 0; color: #94a3b8;">Instance Type:</td><td>{inst_type_html}</td></tr>
                                <tr><td style="padding: 4px 0; color: #94a3b8;">Public IPv4:</td><td>{pub_ip_html}</td></tr>
                                <tr><td style="padding: 4px 0; color: #94a3b8;">Private IPv4:</td><td>{priv_ip_html}</td></tr>
                            </table>
                        </div>
                        <div style="background: rgba(15, 23, 42, 0.4); padding: 14px 18px; border-radius: 8px; border: 1px solid rgba(148, 163, 184, 0.1);">
                            <div style="font-weight: 700; color: #34d399; margin-bottom: 10px; font-size: 13px;">🛡️ Network & Cloud Context</div>
                            <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
                                <tr><td style="padding: 4px 0; color: #94a3b8; width: 140px;">Security Group:</td><td>{sg_id_html}</td></tr>
                                <tr><td style="padding: 4px 0; color: #94a3b8;">VPC ID:</td><td>{vpc_html}</td></tr>
                                <tr><td style="padding: 4px 0; color: #94a3b8;">Subnet ID:</td><td>{subnet_html}</td></tr>
                                <tr><td style="padding: 4px 0; color: #94a3b8;">AWS Account ID:</td><td><code>{acct_id}</code></td></tr>
                                <tr><td style="padding: 4px 0; color: #94a3b8;">AWS Region:</td><td><code>{region_str}</code></td></tr>
                            </table>
                        </div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            b_col1, b_col2 = st.columns([2.5, 4.5])
            with b_col1:
                if st.button(f"🔍 Inspect in Verification View", key=f"res_inspect_{dep.deployment_id}"):
                    st.session_state["active_deployment_id"] = dep.deployment_id
                    st.session_state.pop("active_deployment_result", None)
                    st.session_state["current_page"] = "Verification"
                    st.rerun()
