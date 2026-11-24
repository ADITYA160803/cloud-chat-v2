from typing import Optional
import streamlit as st
from core.models import CostEstimate, InterpretedInfrastructure, PlannerReadinessStatus
from services.cost_estimator import CostEstimator
from services.infrastructure_planner import InfrastructurePlanner
from ui.theme import render_top_header


def render_cost_view():
    """Renders Page 4: Cost Estimation."""
    render_top_header("Cost Estimation")

    interpreted: Optional[InterpretedInfrastructure] = st.session_state.get("active_interpreted_infra")

    if not interpreted:
        st.info("No active infrastructure request in session. Please start from **New Deployment**.")
        if st.button("➔ Go to New Deployment"):
            st.session_state["current_page"] = "New Deployment"
            st.rerun()
        return

    res = interpreted.resources[0] if interpreted.resources else None
    estimator = CostEstimator()
    cost: CostEstimate = estimator.estimate_ec2(interpreted.region, res)
    st.session_state["active_cost_estimate"] = cost

    st.markdown(
        "Transparent cost estimation based on standard AWS On-Demand regional pricing. "
        "Review estimated charges before generating Terraform configuration."
    )

    # Top Metrics
    st.markdown("<div class='cloud-card'>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    c1.metric("Hourly Running Cost", f"${cost.total_hourly_cost:.4f}", help="Sum of on-demand compute and storage per hour")
    c2.metric("Estimated Monthly Cost", f"${cost.total_monthly_cost:.2f}", help="Based on 730 continuously running hours/month")
    c3.metric("Stopped Monthly Cost", f"${cost.stopped_monthly_cost:.2f}", help="EBS storage charges persist even when instance is stopped")
    st.markdown("</div>", unsafe_allow_html=True)

    # Resource Cost Breakdown Table
    st.subheader("Resource Cost Breakdown")
    cost_rows = [
        {
            "Resource Component": item.resource_type,
            "Hourly (USD)": f"${item.hourly_cost:.4f}",
            "Monthly (USD)": f"${item.monthly_cost:.2f}",
            "Pricing Basis": item.notes,
        }
        for item in cost.items
    ]
    st.dataframe(cost_rows, width="stretch")

    # Informational Note / Disclaimer
    st.info(
        "ℹ️ **Pricing Note**: These are estimated costs based on standard AWS On-Demand rates in "
        f"**{cost.region}** for 730 hours/month. Actual charges may vary depending on usage patterns, "
        "data transfer egress, and account-specific discounts."
    )

    # Check planner readiness
    planner = InfrastructurePlanner()
    plan_result = planner.plan(interpreted)
    has_unresolved_inputs = plan_result.readiness in (
        PlannerReadinessStatus.NEEDS_USER_INPUT,
        PlannerReadinessStatus.VALIDATION_FAILED,
    )

    if has_unresolved_inputs:
        reasons = plan_result.missing_information or plan_result.validation_errors
        st.error(
            f"⚠️ **Cannot Proceed to Terraform**: Infrastructure requirements remain unresolved: "
            f"{', '.join(reasons)}. "
            "Please return to **Infrastructure Intent** to supply the required inputs before generating configuration."
        )

    # Actions
    st.divider()
    bcol1, bcol2, bcol3 = st.columns([1.2, 2.0, 2.8])
    with bcol1:
        if st.button("⬅ Back to Intent"):
            st.session_state["current_page"] = "Infrastructure Intent"
            st.rerun()

    with bcol2:
        if st.button(
            "Continue to Terraform ➔",
            type="primary",
            width="stretch",
            disabled=has_unresolved_inputs,
        ):
            st.session_state["current_page"] = "Terraform Configuration"
            st.rerun()
