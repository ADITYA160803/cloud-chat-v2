from config import config, AppConfig


def test_default_config_safe_defaults(monkeypatch):
    """Verify that default configuration enforces safe modes when not overridden by environment."""
    monkeypatch.delenv("ENABLE_MOCK_MODE", raising=False)
    monkeypatch.delenv("ENABLE_REAL_TERRAFORM_APPLY", raising=False)
    c = AppConfig()
    # Mock mode must be ENABLED by default
    assert c.enable_mock_mode is True
    # Real apply must be DISABLED by default
    assert c.enable_real_terraform_apply is False
    # Therefore real deployment is strictly disallowed
    assert c.is_real_deployment_allowed() is False


def test_real_deployment_guardrail():
    """Verify that real deployment requires both mock mode off AND real apply on."""
    # Only real apply enabled, but mock mode still on -> disallow
    c1 = AppConfig(enable_mock_mode=True, enable_real_terraform_apply=True)
    assert c1.is_real_deployment_allowed() is False

    # Mock mode off, but real apply off -> disallow
    c2 = AppConfig(enable_mock_mode=False, enable_real_terraform_apply=False)
    assert c2.is_real_deployment_allowed() is False

    # Both explicitly set to allow real deployment
    c3 = AppConfig(enable_mock_mode=False, enable_real_terraform_apply=True)
    assert c3.is_real_deployment_allowed() is True
