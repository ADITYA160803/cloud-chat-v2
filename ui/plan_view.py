"""Infrastructure Plan views and legacy compatibility wrappers."""
from ui.saved_plans_view import (
    extract_plan_resource_summary,
    get_plan_actions_details,
    render_saved_plans_view,
)


def render_plan_view():
    """Renders the dedicated saved deployment plans view."""
    render_saved_plans_view()


__all__ = [
    "render_saved_plans_view",
    "render_plan_view",
    "extract_plan_resource_summary",
    "get_plan_actions_details",
]
