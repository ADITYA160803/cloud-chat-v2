class CloudChatException(Exception):
    """Base exception for Cloud-Chat V2."""
    pass


class ConfigurationError(CloudChatException):
    """Raised when configuration is invalid or missing required values."""
    pass


class DatabaseError(CloudChatException):
    """Raised when database operations encounter an error."""
    pass


class ValidationError(CloudChatException):
    """Raised when input or output validation fails."""
    pass


class AIParserError(CloudChatException):
    """Raised when AI interpretation fails."""
    pass


class PlanningError(CloudChatException):
    """Raised when infrastructure planning fails."""
    pass


class DependencyError(CloudChatException):
    """Raised when dependency resolution fails or conflicts."""
    pass


class AMIResolutionError(CloudChatException):
    """Raised when AMI resolution encounters an unrecoverable issue."""
    pass


class CostEstimationError(CloudChatException):
    """Raised when cost estimation encounters an error."""
    pass


class SecurityViolationError(CloudChatException):
    """Raised when security validation blocks an operation."""
    pass


class TerraformExecutionError(CloudChatException):
    """Raised when a Terraform command fails."""
    pass


class ApprovalError(CloudChatException):
    """Raised when deployment approval verification fails."""
    pass


class AWSVerificationError(CloudChatException):
    """Raised when independent AWS verification fails."""
    pass

