from pathlib import Path
from unittest.mock import MagicMock, patch
import os
import pytest
from core.models import (
    AWSAuthMethod,
    AWSIdentity,
    AWSService,
    EC2ResourceSpec,
    InterpretedInfrastructure,
    NetworkSpec,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    StorageSpec,
)
from database.db import init_db
from database.repository import DeploymentRepository
from services.ai_parser import AIInfrastructureParser
from services.ami_resolver import AMIResolver
from services.aws_auth_service import AWSCredentialsContainer, aws_auth_service
from services.cost_estimator import CostEstimator
from services.infrastructure_planner import InfrastructurePlanner
from services.region_service import (
    get_region_options,
    get_selected_region,
    set_selected_region,
)
from services.terraform_service import TerraformService
from ui.theme import get_effective_region


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_cloud_chat.db"
    init_db(db_file)
    return db_file


@pytest.fixture(autouse=True)
def reset_region_state(temp_db, monkeypatch):
    """Resets region state before each test."""
    from config import AppConfig
    test_config = AppConfig(db_path=temp_db)
    monkeypatch.setattr("config.config", test_config)
    monkeypatch.setattr("database.db.config", test_config)
    aws_auth_service.clear()
    set_selected_region("ap-south-1", persist=True, db_path=temp_db)
    yield
    aws_auth_service.clear()
    set_selected_region("ap-south-1", persist=True, db_path=temp_db)


def test_selected_region_default_is_ap_south_1():
    """Verify that default selected region is ap-south-1 and does not silently fall back to us-east-1."""
    assert get_selected_region() == "ap-south-1"
    assert get_effective_region() == "ap-south-1"


def test_set_selected_region_updates_memory_env_and_db(temp_db):
    """Verify set_selected_region updates memory, environment variables, and SQLite persistence."""
    set_selected_region("us-west-2", persist=True, db_path=temp_db)

    assert get_selected_region(db_path=temp_db) == "us-west-2"
    assert os.environ.get("AWS_DEFAULT_REGION") == "us-west-2"
    assert os.environ.get("AWS_REGION") == "us-west-2"

    # Verify SQLite persistence
    repo = DeploymentRepository(temp_db)
    persisted = repo.get_setting("selected_region")
    assert persisted == "us-west-2"


def test_persistence_recovers_region_when_in_memory_override_cleared(temp_db):
    """Verify selected region survives across memory resets by reloading from SQLite database."""
    set_selected_region("eu-central-1", persist=True, db_path=temp_db)

    # Simulate fresh process by clearing in-memory global override
    import services.region_service as rs
    rs._ACTIVE_SELECTED_REGION = None

    # Should recover eu-central-1 from SQLite, NOT fall back to us-east-1
    assert get_selected_region(db_path=temp_db) == "eu-central-1"


def test_profile_connection_synchronizes_region():
    """Verify connecting an AWS profile sets the active region and synchronizes across services."""
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {
        "Account": "082229155432",
        "Arn": "arn:aws:sts::082229155432:assumed-role/CloudChatTerraformDeployRole/session",
        "UserId": "AROA082229155432:session",
    }
    mock_session = MagicMock()
    mock_session.client.return_value = mock_sts

    with patch("boto3.Session", return_value=mock_session):
        ident = aws_auth_service.authenticate_profile(
            profile_name="cloud-chat-deploy",
            region="ap-south-1",
        )

    assert ident.account_id == "082229155432"
    assert ident.region == "ap-south-1"
    assert get_selected_region() == "ap-south-1"
    assert get_effective_region() == "ap-south-1"

    # Verify active credentials container region
    creds = aws_auth_service.get_active_credentials()
    assert creds is not None
    assert creds.region == "ap-south-1"
    env = creds.get_subprocess_env()
    assert env["AWS_DEFAULT_REGION"] == "ap-south-1"
    assert env["AWS_REGION"] == "ap-south-1"


def test_ai_parser_defaults_to_selected_region():
    """Verify AI parser assigns the selected region when prompt does not specify a region."""
    set_selected_region("ap-south-1", persist=True)

    parser = AIInfrastructureParser()
    model = InterpretedInfrastructure(
        service=AWSService.EC2,
        region="",  # Empty region from LLM
        resources=[
            EC2ResourceSpec(
                name="server-1",
                instance_type="t3.micro",
                storage=StorageSpec(size_gb=10),
                network=NetworkSpec(),
            )
        ],
        user_request="Create an EC2 t3.micro instance",
    )

    parser._normalize_region(model, prompt="Create an EC2 t3.micro instance")

    assert model.region == "ap-south-1"
    assert model.region != "us-east-1"


def test_infrastructure_planner_uses_selected_region():
    """Verify infrastructure planner plans against selected region when not explicit."""
    set_selected_region("ap-south-1", persist=True)

    interpreted = InterpretedInfrastructure(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            EC2ResourceSpec(
                name="mumbai-app",
                instance_type="t3.micro",
                storage=StorageSpec(size_gb=20),
                network=NetworkSpec(),
            )
        ],
        user_request="Deploy t3.micro",
    )

    planner = InfrastructurePlanner()
    result = planner.plan(interpreted)

    assert result.normalized_spec.region == "ap-south-1"
    assert result.ami_resolution.status == "resolved"
    assert result.ami_resolution.ami_id == "ami-00bb6a80f01f03502"  # Official AL2023 Mumbai AMI
    assert result.cost_estimate.region == "ap-south-1"


def test_terraform_generator_provider_region_consistency():
    """Verify Terraform configuration generates AWS provider with selected region."""
    set_selected_region("ap-south-1", persist=True)

    service = TerraformService()
    spec = NormalizedInfrastructureSpec(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[
            NormalizedEC2Spec(
                name="mumbai-srv",
                instance_type="t3.micro",
                region="ap-south-1",
            )
        ],
    )

    files = service.generate_configuration(spec)
    main_tf = files["main.tf"]
    variables_tf = files["variables.tf"]

    assert 'provider "aws"' in main_tf
    assert "region = var.aws_region" in main_tf
    assert 'default     = "ap-south-1"' in variables_tf


def test_terraform_subprocess_env_matches_selected_region():
    """Verify TerraformService._run_command injects the selected region into subprocess env."""
    set_selected_region("ap-south-1", persist=True)

    service = TerraformService()
    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Terraform initialized"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        service._run_command(["terraform", "init"], Path("."), timeout=10)

        # Inspect env passed to subprocess.run
        _, kwargs = mock_run.call_args
        passed_env = kwargs.get("env", {})
        assert passed_env.get("AWS_DEFAULT_REGION") == "ap-south-1"
        assert passed_env.get("AWS_REGION") == "ap-south-1"


def test_disconnecting_session_does_not_reset_persisted_region(temp_db):
    """Verify disconnecting active AWS session clears credentials but preserves the selected region."""
    set_selected_region("ap-south-1", persist=True)

    # Authenticate session
    creds = AWSCredentialsContainer(
        auth_method=AWSAuthMethod.PROFILE,
        profile_name="cloud-chat-deploy",
        region="ap-south-1",
    )
    ident = AWSIdentity(
        account_id="082229155432",
        arn="arn:aws:iam::082229155432:role/DeployRole",
        user_id="AROA082229155432:session",
        region="ap-south-1",
        auth_method=AWSAuthMethod.PROFILE,
        is_valid=True,
    )
    aws_auth_service.set_active_credentials(creds, ident)

    assert get_selected_region() == "ap-south-1"

    # Disconnect
    aws_auth_service.clear()

    # Region must remain ap-south-1, not fall back to us-east-1
    assert get_selected_region() == "ap-south-1"
    assert get_effective_region() == "ap-south-1"
