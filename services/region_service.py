from typing import List, Optional
import os
import re

# Standard supported AWS regions in Cloud-Chat V2
SUPPORTED_REGIONS: List[str] = [
    "ap-south-1",
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "ap-southeast-1",
    "ap-northeast-1",
]

# In-memory session override to guarantee zero unintentional fallback across reruns
_ACTIVE_SELECTED_REGION: Optional[str] = None


def get_selected_region(db_path: Optional[Any] = None) -> str:
    """Returns the single consistent source of truth for the selected AWS deployment region.

    Resolution hierarchy:
    1. Active authenticated AWS identity or credentials container in memory (e.g. ap-south-1).
    2. Active Streamlit session state ('selected_aws_region').
    3. In-memory session override (_ACTIVE_SELECTED_REGION).
    4. Persisted setting in SQLite database (system_settings 'selected_region').
    5. Environment variables (AWS_DEFAULT_REGION or AWS_REGION).
    6. Configured default fallback.
    """
    global _ACTIVE_SELECTED_REGION

    # 1. Active authenticated AWS identity or credentials container (Priority #1)
    try:
        from services.aws_auth_service import aws_auth_service
        ident = aws_auth_service.get_active_identity()
        if ident and ident.is_valid and ident.region and ident.region.strip():
            _ACTIVE_SELECTED_REGION = ident.region.strip()
            return ident.region.strip()

        creds = aws_auth_service.get_active_credentials()
        if creds and creds.region and creds.region.strip():
            _ACTIVE_SELECTED_REGION = creds.region.strip()
            return creds.region.strip()
    except Exception:
        pass

    # 2. Active Streamlit session state
    try:
        import streamlit as st
        if hasattr(st, "session_state") and "selected_aws_region" in st.session_state:
            val = st.session_state.get("selected_aws_region")
            if val and isinstance(val, str) and val.strip():
                _ACTIVE_SELECTED_REGION = val.strip()
                return val.strip()
    except Exception:
        pass

    # 3. In-memory session override
    if _ACTIVE_SELECTED_REGION and _ACTIVE_SELECTED_REGION.strip():
        return _ACTIVE_SELECTED_REGION.strip()

    # 4. Persisted setting in SQLite database
    try:
        from database.repository import DeploymentRepository
        persisted = DeploymentRepository(db_path=db_path).get_setting("selected_region")
        if persisted and persisted.strip():
            _ACTIVE_SELECTED_REGION = persisted.strip()
            return persisted.strip()
    except Exception:
        pass

    # 5. Environment variable
    env_region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION")
    if env_region and env_region.strip():
        _ACTIVE_SELECTED_REGION = env_region.strip()
        return env_region.strip()

    # 6. Fallback from configuration
    try:
        from config import config
        if config.aws_default_region and config.aws_default_region.strip():
            _ACTIVE_SELECTED_REGION = config.aws_default_region.strip()
            return config.aws_default_region.strip()
    except Exception:
        pass

    return "ap-south-1"


def set_selected_region(region: str, persist: bool = True, db_path: Optional[Any] = None) -> str:
    """Sets the active deployment region across memory, session state, environment, and persistence."""
    global _ACTIVE_SELECTED_REGION
    clean_region = region.strip().lower() if region else "ap-south-1"
    if not clean_region:
        clean_region = "ap-south-1"

    _ACTIVE_SELECTED_REGION = clean_region

    # Update Streamlit session state if running
    try:
        import streamlit as st
        if hasattr(st, "session_state"):
            st.session_state["selected_aws_region"] = clean_region
    except Exception:
        pass

    # Update process environment variables so subprocesses and Boto3 inherit it
    os.environ["AWS_DEFAULT_REGION"] = clean_region
    os.environ["AWS_REGION"] = clean_region

    # Update active credentials container in aws_auth_service if one is active
    try:
        from services.aws_auth_service import aws_auth_service
        creds = aws_auth_service.get_active_credentials()
        if creds:
            creds.region = clean_region
        ident = aws_auth_service.get_active_identity()
        if ident:
            ident.region = clean_region
    except Exception:
        pass

    # Persist to SQLite database
    if persist:
        try:
            from database.repository import DeploymentRepository
            DeploymentRepository(db_path=db_path).set_setting("selected_region", clean_region)
        except Exception:
            pass

    return clean_region


def get_region_options() -> List[str]:
    """Returns the list of supported regions with the currently selected region placed first."""
    current = get_selected_region()
    regions = list(SUPPORTED_REGIONS)
    if current in regions:
        regions.remove(current)
    regions.insert(0, current)
    return regions
