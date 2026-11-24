from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load .env if present
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class AppConfig:
    """Application configuration for Cloud-Chat V2.

    Strictly enforces safe defaults:
    - Mock mode is ENABLED by default.
    - Real Terraform apply is DISABLED by default.
    """

    # AI Configuration
    groq_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("GROQ_API_KEY", "")
    )
    gemini_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY", "")
    )
    default_ai_provider: str = field(
        default_factory=lambda: os.getenv("DEFAULT_AI_PROVIDER", "groq").lower()
    )
    groq_model: str = field(
        default_factory=lambda: os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    )
    gemini_model: str = field(
        default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    )

    # Safety Guardrails
    enable_mock_mode: bool = field(
        default_factory=lambda: os.getenv("ENABLE_MOCK_MODE", "True").lower() in ("true", "1", "yes")
    )
    enable_real_terraform_apply: bool = field(
        default_factory=lambda: os.getenv("ENABLE_REAL_TERRAFORM_APPLY", "False").lower() in ("true", "1", "yes")
    )

    # AWS Defaults
    aws_default_region: str = field(
        default_factory=lambda: os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
    )

    # Paths & Persistence
    db_path: Path = field(
        default_factory=lambda: BASE_DIR / os.getenv("SQLITE_DB_PATH", "database/cloud_chat.db")
    )
    workspace_root: Path = field(
        default_factory=lambda: BASE_DIR / os.getenv("WORKSPACE_ROOT", "workspaces")
    )

    # Performance & Timeouts
    terraform_timeout_seconds: int = 180
    command_timeout_seconds: int = 60

    def is_real_deployment_allowed(self) -> bool:
        """Check if real deployment is deliberately allowed and mock mode disabled."""
        return self.enable_real_terraform_apply and not self.enable_mock_mode


# Singleton configuration instance
config = AppConfig()
