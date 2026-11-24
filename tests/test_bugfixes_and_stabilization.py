from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from config import AppConfig, config
from core.exceptions import ConfigurationError
from core.models import (
    ApprovalStatus,
    AWSService,
    DeploymentRecord,
    DeploymentStatus,
    InterpretedInfrastructure,
    NormalizedInfrastructureSpec,
    PlannerReadinessStatus,
    PlannerResult,
)
from database.db import init_db
from database.repository import DeploymentRepository
from services.ai_parser import (
    AIInfrastructureParser,
    test_groq_connectivity as run_groq_connectivity_test,
)
from ui.theme import get_effective_region


@pytest.fixture
def temp_db(tmp_path):
    """Fixture providing an isolated temporary SQLite database."""
    test_db_path = tmp_path / "test_cloud_chat.db"
    init_db(test_db_path)
    return test_db_path


# ============================================================================
# 1. Groq Model Integration Tests
# ============================================================================

def test_groq_connectivity_missing_key():
    """Verify test_groq_connectivity cleanly handles missing API key without exceptions."""
    status = run_groq_connectivity_test(api_key="", target_model="openai/gpt-oss-120b")
    assert status["api_key_configured"] is False
    assert status["connection_successful"] is False
    assert status["model_available"] is False
    assert "not configured" in status["error_message"].lower()


def test_groq_connectivity_successful_mock():
    """Verify test_groq_connectivity identifies available models and sets timestamps."""
    mock_model_1 = MagicMock()
    mock_model_1.id = "openai/gpt-oss-120b"
    mock_model_2 = MagicMock()
    mock_model_2.id = "openai/gpt-oss-20b"

    mock_models_resp = MagicMock()
    mock_models_resp.data = [mock_model_1, mock_model_2]

    mock_client = MagicMock()
    mock_client.models.list.return_value = mock_models_resp

    with patch("groq.Groq", return_value=mock_client):
        status = run_groq_connectivity_test(api_key="gsk_valid_dummy", target_model="openai/gpt-oss-120b")
        assert status["api_key_configured"] is True
        assert status["connection_successful"] is True
        assert status["model_available"] is True
        assert "openai/gpt-oss-120b" in status["available_models"]
        assert status["last_successful_test"] is not None
        assert status["error_message"] is None


def test_groq_connectivity_model_unavailable_mock():
    """Verify test_groq_connectivity detects when target model is not in the model list."""
    mock_model = MagicMock()
    mock_model.id = "some-other-model"
    mock_models_resp = MagicMock()
    mock_models_resp.data = [mock_model]

    mock_client = MagicMock()
    mock_client.models.list.return_value = mock_models_resp

    with patch("groq.Groq", return_value=mock_client):
        status = run_groq_connectivity_test(api_key="gsk_valid_dummy", target_model="openai/gpt-oss-120b")
        assert status["api_key_configured"] is True
        assert status["connection_successful"] is True
        assert status["model_available"] is False
        assert "not found" in status["error_message"].lower()


def test_groq_connectivity_sanitizes_credentials_on_error():
    """Verify error messages never expose secret API keys."""
    secret_key = "gsk_SUPER_SECRET_KEY_12345"
    mock_client = MagicMock()
    mock_client.models.list.side_effect = Exception(f"Internal error for credential {secret_key}")

    with patch("groq.Groq", return_value=mock_client):
        status = run_groq_connectivity_test(api_key=secret_key, target_model="openai/gpt-oss-120b")
        assert status["connection_successful"] is False
        assert secret_key not in (status["error_message"] or "")


def test_groq_call_catches_deprecated_model_404():
    """Verify _call_groq provides clear error when model returns HTTP 404 model_not_found."""
    parser = AIInfrastructureParser(provider="groq")
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = Exception("Error code: 404 - {'error': {'code': 'model_not_found'}}")

    dummy_cfg = AppConfig(groq_api_key="gsk_dummy", groq_model="deprecated-model")
    with patch("services.ai_parser.config", dummy_cfg):
        with patch("groq.Groq", return_value=mock_client):
            with pytest.raises(ConfigurationError) as exc_info:
                parser._call_groq("Create EC2")
            assert "not found or has been deprecated" in str(exc_info.value)


# ============================================================================
# 2. Region Handling & Mumbai Normalization Tests
# ============================================================================

def test_mumbai_resolves_to_ap_south_1():
    """Verify prompt mentioning Mumbai deterministically maps to ap-south-1."""
    parser = AIInfrastructureParser(provider="groq")
    mock_response = (
        '{"service": "ec2", "region": "us-east-1", "resources": [{"name": "srv", "instance_type": "t3.micro"}]}'
    )
    with patch.object(parser, "_call_groq", return_value=mock_response):
        res = parser.parse("Create a private EC2 instance in Mumbai with t3.micro, 8 GB encrypted gp3 storage.")
        assert res.region == "ap-south-1"


def test_effective_region_resolution():
    """Verify get_effective_region respects active plan or infra over default region."""
    # 1. Empty session state -> defaults to config.aws_default_region
    state = {}
    with patch("streamlit.session_state", state):
        assert get_effective_region() == config.aws_default_region

        # 2. Interpreted infrastructure present -> uses interpreted region
        state["active_interpreted_infra"] = InterpretedInfrastructure(
            service=AWSService.EC2,
            region="ap-south-1",
            user_request="EC2 in Mumbai",
        )
        assert get_effective_region() == "ap-south-1"

        # 3. Plan result present -> uses normalized spec region
        spec = NormalizedInfrastructureSpec(region="eu-west-1", service=AWSService.EC2)
        state["active_plan_result"] = PlannerResult(
            readiness=PlannerReadinessStatus.READY,
            normalized_spec=spec,
        )
        assert get_effective_region() == "eu-west-1"


def test_deployment_record_preserves_region(temp_db):
    """Verify DeploymentRecord stores and retains target region in repository."""
    repo = DeploymentRepository(db_path=temp_db)
    record = DeploymentRecord(
        deployment_id="dep-mumbai-001",
        user_request="Launch in Mumbai",
        service="ec2",
        status=DeploymentStatus.DRAFT,
        aws_region="ap-south-1",
    )
    repo.create(record)

    fetched = repo.get("dep-mumbai-001")
    assert fetched is not None
    assert fetched.aws_region == "ap-south-1"


# ============================================================================
# 3. Chat Deduplication & Message Persistence Tests
# ============================================================================

def test_chat_message_persistence_and_order(temp_db):
    """Verify messages are stored in SQLite and returned in chronological order."""
    repo = DeploymentRepository(db_path=temp_db)
    repo.add_chat_message("msg-1", "user", "Create EC2 in Mumbai")
    repo.add_chat_message("msg-2", "assistant", "I have structured your request in ap-south-1.")

    messages = repo.list_chat_messages()
    assert len(messages) == 2
    assert messages[0]["message_id"] == "msg-1"
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Create EC2 in Mumbai"
    assert messages[1]["message_id"] == "msg-2"
    assert messages[1]["role"] == "assistant"


def test_duplicate_chat_message_idempotence(temp_db):
    """Verify duplicate message_id insertions are ignored by SQLite."""
    repo = DeploymentRepository(db_path=temp_db)
    repo.add_chat_message("msg-unique-1", "user", "Original message")
    # Attempt duplicate insert
    repo.add_chat_message("msg-unique-1", "user", "Original message")

    messages = repo.list_chat_messages()
    assert len(messages) == 1
    assert messages[0]["message_id"] == "msg-unique-1"
