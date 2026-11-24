from typing import Dict, Optional
import streamlit as st
from core.models import InterpretedInfrastructure, PlannerReadinessStatus, PlannerResult
from services.infrastructure_planner import InfrastructurePlanner
from services.terraform_service import TerraformService
from ui.theme import render_top_header


def render_terraform_code_view():
    """Renders Page 5: Terraform Configuration."""
    render_top_header("Terraform Configuration")

    interpreted: Optional[InterpretedInfrastructure] = st.session_state.get("active_interpreted_infra")

    if not interpreted:
        st.info("No active infrastructure request in session. Please start from **New Deployment**.")
        if st.button("➔ Go to New Deployment"):
            st.session_state["current_page"] = "New Deployment"
            st.rerun()
        return

    # Generate normalized plan
    planner = InfrastructurePlanner()
    plan_result: PlannerResult = planner.plan(interpreted)
    st.session_state["active_plan_result"] = plan_result

    # Block generation if required inputs are unresolved
    if plan_result.readiness in (PlannerReadinessStatus.NEEDS_USER_INPUT, PlannerReadinessStatus.VALIDATION_FAILED):
        reasons = plan_result.missing_information or plan_result.validation_errors
        st.error(
            "🛑 **Terraform Generation Blocked**: Required infrastructure inputs are missing or invalid: "
            f"{', '.join(reasons)}.\n\n"
            "Declarative Terraform configuration cannot be generated without these required dependencies. "
            "Please return to **Infrastructure Intent** to supply the required parameters."
        )
        c_act1, c_act2 = st.columns([1.5, 3])
        with c_act1:
            if st.button("⬅ Return to Infrastructure Intent", type="primary", width="stretch"):
                st.session_state["current_page"] = "Infrastructure Intent"
                st.rerun()
        return

    # Generate Terraform configuration
    tf_service = TerraformService()
    tf_files: Dict[str, str] = tf_service.generate_configuration(plan_result.normalized_spec)
    st.session_state["active_tf_files"] = tf_files

    # Invalidate prior approval if configuration or hash changed
    new_hash = tf_service.calculate_plan_hash(tf_files)
    prev_hash = st.session_state.get("active_plan_hash")
    if prev_hash and prev_hash != new_hash:
        st.session_state["plan_approved"] = False
        dep_id = st.session_state.get("active_deployment_id")
        if dep_id:
            from database.repository import DeploymentRepository
            from core.models import ApprovalStatus
            DeploymentRepository().update_approval(dep_id, ApprovalStatus.PENDING, plan_hash=new_hash)
    st.session_state["active_plan_hash"] = new_hash


    st.markdown(
        "Inspect the generated declarative Terraform infrastructure files. "
        "Review the code structure, root volume encryption, and security group rules."
    )

    # File Tabs
    tabs = st.tabs(["📄 main.tf", "⚙️ variables.tf", "📤 outputs.tf"])

    with tabs[0]:
        st.code(tf_files.get("main.tf", "# main.tf"), language="hcl")

    with tabs[1]:
        st.code(tf_files.get("variables.tf", "# variables.tf"), language="hcl")

    with tabs[2]:
        st.code(tf_files.get("outputs.tf", "# outputs.tf"), language="hcl")

    # Actions
    st.divider()
    bcol1, bcol2, bcol3 = st.columns([1.2, 2.5, 2.3])
    with bcol1:
        if st.button("⬅ Back to Cost"):
            st.session_state["current_page"] = "Cost Estimation"
            st.rerun()

    with bcol2:
        if st.button("Run Terraform Validation & Scan ➔", type="primary", width="stretch"):
            dep_id = st.session_state.get("active_deployment_id", "dep-default")
            st.session_state["active_deployment_id"] = dep_id
            with st.spinner("Executing Terraform pipeline (fmt, init, validate, security scan, plan)..."):
                pipeline_result = tf_service.run_pipeline(dep_id, plan_result.normalized_spec)
                st.session_state["active_tf_plan_result"] = pipeline_result
                st.session_state["active_plan_hash"] = pipeline_result.plan_hash
                st.session_state["current_page"] = "Terraform Plan Review"
                st.rerun()

