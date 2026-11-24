import os
import re
from typing import Any, Dict, List, Optional
import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from config import config
from core.exceptions import ConfigurationError, ValidationError
from core.models import AWSAuthMethod, AWSIdentity

# Ensure boto3 and botocore always read AWS config profiles
os.environ.setdefault("AWS_SDK_LOAD_CONFIG", "1")



class AWSCredentialsContainer:
    """In-memory container for active session AWS credentials.

    CRITICAL SECURITY RULES:
    1. Kept in memory only for the active session.
    2. Never persisted to SQLite.
    3. Never written to Terraform configuration files or state.
    4. Never logged or exposed in the UI.
    5. Redacts secret keys from all logs and output strings.
    """

    def __init__(
        self,
        auth_method: AWSAuthMethod = AWSAuthMethod.DEFAULT_CHAIN,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        session_token: Optional[str] = None,
        profile_name: Optional[str] = None,
        region: Optional[str] = None,
        boto3_session: Optional[boto3.Session] = None,
    ):
        self.auth_method = auth_method
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.session_token = session_token
        self.profile_name = profile_name
        self.boto3_session = boto3_session
        from services.region_service import get_selected_region
        self.region = region or get_selected_region()

    def get_subprocess_env(self, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Provides an environment dictionary for Terraform subprocess execution."""
        env = dict(base_env or os.environ)
        env["AWS_DEFAULT_REGION"] = self.region
        env["AWS_REGION"] = self.region
        env["AWS_SDK_LOAD_CONFIG"] = "1"
        env["AWS_EC2_METADATA_DISABLED"] = "true"

        if self.auth_method == AWSAuthMethod.ACCESS_KEY:
            if self.access_key_id:
                env["AWS_ACCESS_KEY_ID"] = self.access_key_id
            if self.secret_access_key:
                env["AWS_SECRET_ACCESS_KEY"] = self.secret_access_key
            if self.session_token:
                env["AWS_SESSION_TOKEN"] = self.session_token
            # Remove profile if using explicit keys
            env.pop("AWS_PROFILE", None)
            env.pop("AWS_DEFAULT_PROFILE", None)

        elif self.auth_method == AWSAuthMethod.PROFILE:
            if self.profile_name:
                env["AWS_PROFILE"] = self.profile_name
                env["AWS_DEFAULT_PROFILE"] = self.profile_name
            # If temporary or profile session credentials were resolved, pass them directly
            # This ensures Terraform subprocesses authenticate as the exact STS-verified identity
            if self.access_key_id and self.secret_access_key:
                env["AWS_ACCESS_KEY_ID"] = self.access_key_id
                env["AWS_SECRET_ACCESS_KEY"] = self.secret_access_key
                if self.session_token:
                    env["AWS_SESSION_TOKEN"] = self.session_token
                else:
                    env.pop("AWS_SESSION_TOKEN", None)
            else:
                env.pop("AWS_ACCESS_KEY_ID", None)
                env.pop("AWS_SECRET_ACCESS_KEY", None)
                env.pop("AWS_SESSION_TOKEN", None)

        return env

    def get_boto3_session(self, region: Optional[str] = None) -> boto3.Session:
        """Returns a configured Boto3 Session using active in-memory credentials without persisting secrets.

        CRITICAL REUSE RULES:
        1. If frozen credentials (temporary STS credentials or explicit keys) are present,
           instantiate the session with them directly. This guarantees Boto3 calls authenticate
           as the exact STS-verified role without failing with ProfileNotFound or NoCredentialsError.
        2. If an authenticated boto3_session is stored, reuse it or derive credentials for the target region.
        3. Only if explicit credentials are not yet frozen, fall back to profile_name with AWS_SDK_LOAD_CONFIG=1.
        """
        target_region = region or self.region

        # 1. Prefer verified in-memory frozen credentials (from STS assumed role or access keys)
        if self.access_key_id and self.secret_access_key:
            return boto3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                aws_session_token=self.session_token,
                region_name=target_region,
            )

        # 2. Reuse active authenticated session if present
        if self.boto3_session is not None:
            if not target_region or getattr(self.boto3_session, "region_name", None) == target_region:
                return self.boto3_session
            try:
                creds = self.boto3_session.get_credentials()
                if creds:
                    frozen = creds.get_frozen_credentials()
                    if frozen:
                        return boto3.Session(
                            aws_access_key_id=frozen.access_key,
                            aws_secret_access_key=frozen.secret_key,
                            aws_session_token=frozen.token,
                            region_name=target_region,
                        )
            except Exception:
                pass

        # 3. Fallback to profile_name
        if self.auth_method == AWSAuthMethod.PROFILE and self.profile_name:
            os.environ.setdefault("AWS_SDK_LOAD_CONFIG", "1")
            return boto3.Session(
                profile_name=self.profile_name,
                region_name=target_region,
            )

        return boto3.Session(region_name=target_region)

    def redact_secrets(self, text: str, extra_secrets: Optional[List[str]] = None) -> str:
        """Redacts sensitive AWS secrets from any text, stdout, or error string."""
        if not text:
            return ""
        sanitized = text
        if self.secret_access_key and len(self.secret_access_key) > 4:
            sanitized = sanitized.replace(self.secret_access_key, "[REDACTED_SECRET_KEY]")
        if self.session_token and len(self.session_token) > 8:
            sanitized = sanitized.replace(self.session_token, "[REDACTED_SESSION_TOKEN]")
        if self.access_key_id and len(self.access_key_id) > 8:
            # Mask access key keeping only first 4 and last 4 chars
            masked = f"{self.access_key_id[:4]}...{self.access_key_id[-4:]}"
            sanitized = sanitized.replace(self.access_key_id, masked)
        if extra_secrets:
            for s in extra_secrets:
                if s and len(s) > 4:
                    sanitized = sanitized.replace(s, "[REDACTED_SECRET]")
        return sanitized


class AWSAuthService:
    """Handles real AWS authentication, STS caller identity verification, and profile management."""

    def __init__(self):
        self._active_credentials: Optional[AWSCredentialsContainer] = None
        self._active_identity: Optional[AWSIdentity] = None

    def redact_secrets(self, text: str, secret_keys: Optional[List[str]] = None) -> str:
        """Sanitizes sensitive credentials from any text or exception string."""
        if self._active_credentials:
            return self._active_credentials.redact_secrets(text, extra_secrets=secret_keys)
        sanitized = text or ""
        if secret_keys:
            for s in secret_keys:
                if s and len(s) > 4:
                    sanitized = sanitized.replace(s, "[REDACTED_SECRET]")
        return sanitized

    def get_available_profiles(self) -> List[str]:
        """Returns the list of configured named profiles in the local AWS CLI configuration."""
        try:
            session = boto3.Session()
            return session.available_profiles or ["default"]
        except Exception:
            return ["default"]

    def authenticate_access_key(
        self,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        session_token: Optional[str] = None,
    ) -> AWSIdentity:
        """Authenticates using explicit AWS Access Keys via STS GetCallerIdentity."""
        clean_key = access_key_id.strip() if access_key_id else ""
        clean_secret = secret_access_key.strip() if secret_access_key else ""
        clean_token = session_token.strip() if session_token else None
        from services.region_service import get_selected_region, set_selected_region
        clean_region = region.strip() if region else get_selected_region()

        if not clean_key or not clean_secret:
            raise ValidationError("AWS Access Key ID and Secret Access Key are required.")

        try:
            session = boto3.Session(
                aws_access_key_id=clean_key,
                aws_secret_access_key=clean_secret,
                aws_session_token=clean_token,
                region_name=clean_region,
            )
            sts = session.client("sts")
            identity_resp = sts.get_caller_identity()

            identity = AWSIdentity(
                account_id=identity_resp["Account"],
                arn=identity_resp["Arn"],
                user_id=identity_resp["UserId"],
                region=clean_region,
                auth_method=AWSAuthMethod.ACCESS_KEY,
                is_valid=True,
            )

            # Store in-memory credentials container
            self._active_credentials = AWSCredentialsContainer(
                auth_method=AWSAuthMethod.ACCESS_KEY,
                access_key_id=clean_key,
                secret_access_key=clean_secret,
                session_token=clean_token,
                region=clean_region,
                boto3_session=session,
            )
            self._active_identity = identity
            if self is aws_auth_service:
                set_selected_region(clean_region, persist=True)
            return identity

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "AuthenticationFailed")
            msg = e.response.get("Error", {}).get("Message", str(e))
            sanitized = self.redact_secrets(msg, secret_keys=[clean_secret, clean_token])
            raise ConfigurationError(f"AWS Access Key authentication failed [{code}]: {sanitized}") from None
        except Exception as e:
            sanitized = self.redact_secrets(str(e), secret_keys=[clean_secret, clean_token])
            raise ConfigurationError(f"AWS authentication error: {sanitized}") from None

    def authenticate_profile(
        self,
        profile_name: str,
        region: Optional[str] = None,
    ) -> AWSIdentity:
        """Authenticates using an AWS CLI named profile or AWS SSO session."""
        clean_profile = profile_name.strip() if profile_name else "default"
        from services.region_service import get_selected_region, set_selected_region
        clean_region = region.strip() if region else None
        if not clean_region:
            try:
                temp_sess = boto3.Session(profile_name=clean_profile)
                if temp_sess.region_name:
                    clean_region = temp_sess.region_name
            except Exception:
                pass
        if not clean_region:
            clean_region = get_selected_region()

        try:
            session = boto3.Session(profile_name=clean_profile, region_name=clean_region)
            sts = session.client("sts")
            identity_resp = sts.get_caller_identity()

            # Resolve frozen temporary credentials for this profile session if available
            access_key, secret_key, session_token = None, None, None
            try:
                creds = session.get_credentials()
                if creds:
                    frozen = creds.get_frozen_credentials()
                    if frozen:
                        access_key = frozen.access_key
                        secret_key = frozen.secret_key
                        session_token = frozen.token
            except Exception:
                pass

            identity = AWSIdentity(
                account_id=identity_resp["Account"],
                arn=identity_resp["Arn"],
                user_id=identity_resp["UserId"],
                region=clean_region,
                auth_method=AWSAuthMethod.PROFILE,
                profile_name=clean_profile,
                is_valid=True,
            )

            self._active_credentials = AWSCredentialsContainer(
                auth_method=AWSAuthMethod.PROFILE,
                access_key_id=access_key,
                secret_access_key=secret_key,
                session_token=session_token,
                profile_name=clean_profile,
                region=clean_region,
                boto3_session=session,
            )
            self._active_identity = identity
            if self is aws_auth_service:
                set_selected_region(clean_region, persist=True)
            return identity

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "ProfileAuthFailed")
            msg = e.response.get("Error", {}).get("Message", str(e))
            if "ExpiredToken" in code or "Token" in msg:
                raise ConfigurationError(
                    f"AWS SSO / Profile session has expired. Please run 'aws sso login --profile {clean_profile}' in your terminal and retry."
                ) from e
            raise ConfigurationError(f"AWS Profile '{clean_profile}' authentication failed [{code}]: {msg}") from e
        except Exception as e:
            raise ConfigurationError(f"AWS Profile error: {str(e)}") from e

    def get_active_credentials(self) -> Optional[AWSCredentialsContainer]:
        """Returns the in-memory credentials container for the current session."""
        return self._active_credentials

    def get_active_identity(self) -> Optional[AWSIdentity]:
        """Returns the active authenticated AWS identity."""
        return self._active_identity

    def set_active_credentials(self, creds: AWSCredentialsContainer, identity: AWSIdentity) -> None:
        """Sets active credentials and identity manually."""
        self._active_credentials = creds
        self._active_identity = identity

    def get_boto3_client(self, service_name: str, region_name: Optional[str] = None) -> Any:
        """Returns a Boto3 client using the active in-memory session."""
        if not self._active_credentials:
            raise ConfigurationError("Cannot create Boto3 client: No active AWS authentication credentials found.")
        session = self._active_credentials.get_boto3_session(region=region_name)
        return session.client(service_name)

    def clear(self) -> None:
        """Clears active credentials and identity from memory."""
        self._active_credentials = None
        self._active_identity = None


# Singleton instance for session management
aws_auth_service = AWSAuthService()
