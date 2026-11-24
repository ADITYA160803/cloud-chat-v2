from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator


class DeploymentMode(str, Enum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class SubnetClassification(str, Enum):
    PUBLIC = "PUBLIC"
    PRIVATE_WITH_NAT = "PRIVATE_WITH_NAT"
    PRIVATE_WITH_EGRESS_ONLY_IGW = "PRIVATE_WITH_EGRESS_ONLY_IGW"
    ISOLATED = "ISOLATED"
    UNKNOWN = "UNKNOWN"


class DeploymentStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIALLY_DEPLOYED = "partially_deployed"
    EXECUTION_STATUS_UNKNOWN = "execution_status_unknown"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


class VerificationStatus(str, Enum):
    NOT_CHECKED = "NOT_CHECKED"
    VERIFIED = "VERIFIED"
    VERIFIED_WITH_WARNINGS = "VERIFIED_WITH_WARNINGS"
    CONFIGURATION_MISMATCH = "CONFIGURATION_MISMATCH"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    FAILED = "FAILED"
    STALE = "STALE"


class PropertyComparisonResult(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    NOT_FOUND = "NOT_FOUND"
    UNABLE_TO_VERIFY = "UNABLE_TO_VERIFY"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class VerificationSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    ERROR = "ERROR"


class PropertyVerificationCheck(BaseModel):
    """Result of an individual property conformance comparison."""
    category: str = Field(description="EC2, EBS, Network, Security Group")
    property_name: str = Field(description="e.g. Instance Type, Storage Size, Public IP")
    expected_value: Any
    actual_value: Any
    result: PropertyComparisonResult
    explanation: Optional[str] = None
    severity: VerificationSeverity = VerificationSeverity.ERROR


class ResourceVerificationDetails(BaseModel):
    """Detailed live AWS state for a specific verified resource."""
    resource_type: str = Field(description="e.g. aws_instance, aws_ebs_volume")
    resource_id: str
    exists: bool = False
    operational_state: str = "unknown"
    checks: List[PropertyVerificationCheck] = Field(default_factory=list)
    raw_details: Dict[str, Any] = Field(default_factory=dict)


class DeploymentVerificationReport(BaseModel):
    """Comprehensive post-deployment live AWS verification and conformance report."""
    deployment_id: str
    aws_account_id: str
    aws_region: str
    verification_status: VerificationStatus
    verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resource_ids: List[str] = Field(default_factory=list)
    checks: List[PropertyVerificationCheck] = Field(default_factory=list)
    resources: List[ResourceVerificationDetails] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    is_mock: bool = False
    summary: str = ""


class AWSService(str, Enum):
    EC2 = "ec2"
    S3 = "s3"
    VPC = "vpc"
    RDS = "rds"


class VolumeType(str, Enum):
    GP3 = "gp3"
    GP2 = "gp2"
    IO2 = "io2"
    IO1 = "io1"
    STANDARD = "standard"


class StorageSpec(BaseModel):
    """Specification for block or persistent storage."""
    size_gb: int = Field(default=20, ge=1, le=16384, description="Storage size in GiB")
    volume_type: VolumeType = Field(default=VolumeType.GP3, description="EBS volume type")
    encrypted: bool = Field(default=True, description="Whether storage volume is encrypted")
    iops: Optional[int] = Field(default=None, description="Provisioned IOPS if applicable")
    throughput: Optional[int] = Field(default=None, description="Provisioned throughput in MiB/s")


class NetworkSpec(BaseModel):
    """Specification for network placement and public exposure."""
    deployment_mode: DeploymentMode = Field(default=DeploymentMode.PRIVATE, description="Networking mode: PUBLIC or PRIVATE")
    public_ip: bool = Field(default=False, description="Whether a public IP address is assigned")
    vpc_id: Optional[str] = Field(default=None, description="Existing VPC ID reference")
    subnet_id: Optional[str] = Field(default=None, description="Existing Subnet ID reference")
    security_group_ids: List[str] = Field(default_factory=list, description="Existing Security Group IDs")
    allowed_ssh_cidrs: List[str] = Field(default_factory=list, description="CIDR blocks allowed for SSH access")
    use_existing_vpc: bool = Field(default=False, description="Whether user explicitly requested an existing VPC")
    use_existing_subnet: bool = Field(default=False, description="Whether user explicitly requested an existing Subnet")

    @model_validator(mode="before")
    @classmethod
    def sync_deployment_mode(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # If public_ip is True and deployment_mode wasn't specified, infer PUBLIC
            if data.get("public_ip") is True and "deployment_mode" not in data:
                data["deployment_mode"] = DeploymentMode.PUBLIC
            # In PRIVATE mode, public_ip is strictly disabled
            elif data.get("deployment_mode") in (DeploymentMode.PRIVATE, "PRIVATE"):
                data["public_ip"] = False
        return data



class EC2ResourceSpec(BaseModel):
    """Structured specification for an EC2 compute instance."""
    name: str = Field(default="app-server", description="Logical name / Name tag for resource")
    instance_type: str = Field(default="t3.micro", description="AWS EC2 instance type")
    ami_id: Optional[str] = Field(default=None, description="Explicit AMI ID if requested")
    key_name: Optional[str] = Field(default=None, description="SSH key pair name")
    storage: StorageSpec = Field(default_factory=StorageSpec)
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    tags: Dict[str, str] = Field(default_factory=dict, description="Resource tags")

    @field_validator("instance_type")
    @classmethod
    def validate_instance_type(cls, v: str) -> str:
        clean = v.strip().lower()
        if not clean or "." not in clean:
            raise ValueError(f"Invalid EC2 instance type format: '{v}' (expected e.g. 't3.micro')")
        return clean

    @field_validator("storage", mode="before")
    @classmethod
    def coerce_storage(cls, v: Any) -> Any:
        if isinstance(v, StorageSpec):
            return v
        if hasattr(v, "model_dump"):
            return v.model_dump()
        return v

    @field_validator("network", mode="before")
    @classmethod
    def coerce_network(cls, v: Any) -> Any:
        if isinstance(v, NetworkSpec):
            return v
        if hasattr(v, "model_dump"):
            return v.model_dump()
        return v


class InterpretedInfrastructure(BaseModel):
    """Validated output from AI interpretation representing structured user requirements."""
    service: AWSService = Field(default=AWSService.EC2, description="Target AWS service")
    region: str = Field(default="us-east-1", description="Target AWS region")
    resources: List[EC2ResourceSpec] = Field(default_factory=list, description="List of resources to deploy")
    assumptions: List[str] = Field(default_factory=list, description="Assumptions made where user was not explicit")
    missing_information: List[str] = Field(default_factory=list, description="Essential information missing")
    requires_clarification: bool = Field(default=False, description="Whether execution must pause for clarification")
    clarification_question: Optional[str] = Field(default=None, description="Question asked to the user")
    user_request: str = Field(description="Original natural-language input")


class PlannerReadinessStatus(str, Enum):
    READY = "Ready"
    NEEDS_USER_INPUT = "Needs User Input"
    VALIDATION_FAILED = "Validation Failed"
    AWS_LOOKUP_UNAVAILABLE = "AWS Lookup Unavailable"


class DependencyStatus(str, Enum):
    SATISFIED = "satisfied"
    REQUIRES_SELECTION = "requires_selection"
    DEFAULT_AVAILABLE = "default_available"
    OPTIONAL = "optional"
    UNRESOLVED = "unresolved"


class DependencyItem(BaseModel):
    """Represents a specific dependency required for infrastructure provisioning."""
    name: str = Field(description="Name of the dependency e.g. VPC, Subnet, AMI")
    required: bool = Field(default=True, description="Whether this dependency is strictly required")
    status: DependencyStatus = Field(default=DependencyStatus.UNRESOLVED)
    value: Optional[str] = Field(default=None, description="Resolved identifier or value")
    source: str = Field(default="unresolved", description="Source: user_supplied, aws_discovered, safe_default")
    message: Optional[str] = Field(default=None, description="Actionable message or explanation")


class SubnetDetail(BaseModel):
    """Detailed AWS Subnet metadata and routing classification."""
    subnet_id: str
    vpc_id: str
    cidr: str
    availability_zone: str
    classification: SubnetClassification = SubnetClassification.UNKNOWN
    route_table_id: Optional[str] = None
    map_public_ip_on_launch: bool = False
    explanation: Optional[str] = None
    label: str = ""


class AMIResolutionResult(BaseModel):
    """Result of AMI resolution."""
    ami_id: Optional[str] = Field(default=None, description="AWS AMI ID e.g. ami-0123456789abcdef0")
    image_name: Optional[str] = Field(default=None, description="AMI Name / description")
    os_preference: str = Field(default="Amazon Linux 2023", description="Target operating system")
    architecture: str = Field(default="x86_64", description="CPU Architecture: x86_64 or arm64")
    owner: Optional[str] = Field(default=None, description="Image owner account or alias")
    creation_date: Optional[str] = Field(default=None, description="Creation timestamp of AMI")
    is_explicit: bool = Field(default=False, description="Whether AMI ID was explicitly provided by user")
    status: str = Field(default="resolved", description="Status: resolved, unresolved, lookup_failed")
    message: Optional[str] = Field(default=None, description="Resolution notes or error message")


class CostItem(BaseModel):
    """Cost breakdown for a specific infrastructure component."""
    resource_type: str = Field(description="e.g. EC2 Instance (t3.micro), EBS Volume (8 GB gp3)")
    hourly_cost: float = Field(default=0.0)
    monthly_cost: float = Field(default=0.0)
    notes: str = Field(default="")


class CostEstimate(BaseModel):
    """Transparent cost estimate for planned infrastructure."""
    region: str
    total_hourly_cost: float = 0.0
    total_monthly_cost: float = 0.0
    stopped_monthly_cost: float = 0.0
    items: List[CostItem] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    currency: str = "USD"
    hours_per_month: int = 730
    pricing_source: str = "AWS Standard Regional Pricing Reference"
    is_available: bool = True
    disclaimer: str = (
        "Estimate is approximate based on standard on-demand regional rates for 730 hours/month. "
        "Actual AWS bills depend on real running hours, data transfer, and account-specific discounts."
    )


class NormalizedEC2Spec(BaseModel):
    """Normalized, validated specification for an EC2 compute instance."""
    name: str = "app-server"
    instance_type: str = "t3.micro"
    region: str = "us-east-1"
    ami: AMIResolutionResult = Field(default_factory=AMIResolutionResult)
    storage: StorageSpec = Field(default_factory=StorageSpec)
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    tags: Dict[str, str] = Field(default_factory=dict)

    @field_validator("storage", mode="before")
    @classmethod
    def coerce_storage(cls, v: Any) -> Any:
        if isinstance(v, StorageSpec):
            return v
        if hasattr(v, "model_dump"):
            return v.model_dump()
        return v

    @field_validator("network", mode="before")
    @classmethod
    def coerce_network(cls, v: Any) -> Any:
        if isinstance(v, NetworkSpec):
            return v
        if hasattr(v, "model_dump"):
            return v.model_dump()
        return v

    @field_validator("ami", mode="before")
    @classmethod
    def coerce_ami(cls, v: Any) -> Any:
        if isinstance(v, AMIResolutionResult):
            return v
        if hasattr(v, "model_dump"):
            return v.model_dump()
        return v


class NormalizedInfrastructureSpec(BaseModel):
    """Fully normalized and validated infrastructure specification."""
    service: AWSService = AWSService.EC2
    region: str = "us-east-1"
    resources: List[NormalizedEC2Spec] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    user_request: str = ""


class PlannerResult(BaseModel):
    """Final output produced by the Infrastructure Planner."""
    readiness: PlannerReadinessStatus
    normalized_spec: Optional[NormalizedInfrastructureSpec] = None
    dependencies: List[DependencyItem] = Field(default_factory=list)
    ami_resolution: Optional[AMIResolutionResult] = None
    cost_estimate: Optional[CostEstimate] = None
    assumptions: List[str] = Field(default_factory=list)
    missing_information: List[str] = Field(default_factory=list)
    validation_errors: List[str] = Field(default_factory=list)



class SecurityFinding(BaseModel):
    """Specific finding from Checkov or deterministic security policy scanning."""
    rule_id: str = Field(description="e.g. CKV_AWS_PUBLIC_IP, CKV_AWS_EBS_ENCRYPTION, CKV_AWS_88, CKV_AWS_382, CKV2_AWS_41")
    title: str = Field(description="Human-readable rule title")
    severity: str = Field(default="HIGH", description="HIGH, MEDIUM, LOW, INFO")
    resource: str = Field(description="Resource identifier, e.g. aws_instance.app_server")
    status: str = Field(default="FAILED", description="PASSED or FAILED")
    explanation: str = Field(description="Security explanation of the finding")
    remediation: str = Field(description="Recommended action to remediate the vulnerability")
    disposition: str = Field(default="UNRESOLVED", description="REMEDIATED, ACCEPTED_RISK, or UNRESOLVED")
    rationale: Optional[str] = Field(default=None, description="Security rationale for accepted risk or remediation details")


class SecurityScanResult(BaseModel):
    """Aggregated security scan results."""
    passed_checks: int = Field(default=0)
    failed_checks: int = Field(default=0)
    findings: List[SecurityFinding] = Field(default_factory=list)
    scanner: str = Field(default="checkov", description="checkov or policy_engine")
    summary: str = Field(default="Security scan completed successfully.")
    remediated_findings: List[SecurityFinding] = Field(default_factory=list)
    accepted_findings: List[SecurityFinding] = Field(default_factory=list)
    unresolved_findings: List[SecurityFinding] = Field(default_factory=list)


class TerraformExecutionStage(str, Enum):
    CONFIG_GENERATION = "CONFIG_GENERATION"
    FORMAT = "FORMAT"
    INIT = "INIT"
    VALIDATE = "VALIDATE"
    SECURITY_SCAN = "SECURITY_SCAN"
    PLAN = "PLAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TerraformPlanResult(BaseModel):
    """Output from the Terraform execution and planning pipeline."""
    deployment_id: str
    workspace_dir: str
    stage: TerraformExecutionStage = TerraformExecutionStage.COMPLETED
    format_passed: bool = True
    init_passed: bool = True
    validation_passed: bool = True
    plan_passed: bool = True
    to_add: int = 0
    to_change: int = 0
    to_destroy: int = 0
    is_destructive: bool = False
    plan_hash: str = ""
    plan_artifact_hash: str = ""
    plan_file_path: Optional[str] = None
    security_scan: SecurityScanResult = Field(default_factory=SecurityScanResult)
    actions: List[Dict[str, Any]] = Field(default_factory=list)
    error_message: Optional[str] = None
    execution_logs: str = ""
    is_mock_plan: bool = False


class AWSAuthMethod(str, Enum):
    ACCESS_KEY = "access_key"
    PROFILE = "profile"
    DEFAULT_CHAIN = "default_chain"


class AWSIdentity(BaseModel):
    """Authenticated AWS caller identity verified through STS GetCallerIdentity."""
    account_id: str = Field(description="12-digit AWS Account ID")
    arn: str = Field(description="Full AWS Identity ARN")
    user_id: str = Field(description="Unique AWS Principal User ID")
    region: str = Field(default="us-east-1", description="Active AWS Region")
    auth_method: AWSAuthMethod = Field(default=AWSAuthMethod.ACCESS_KEY)
    profile_name: Optional[str] = Field(default=None, description="AWS CLI named profile if applicable")
    is_valid: bool = Field(default=True)
    error_message: Optional[str] = Field(default=None)


class DeploymentRecord(BaseModel):
    """Domain model for a deployment record stored in SQLite."""
    deployment_id: str
    user_request: str
    service: str = "ec2"
    status: DeploymentStatus = DeploymentStatus.DRAFT
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    plan_hash: Optional[str] = None
    parsed_intent: Optional[Dict[str, Any]] = None
    normalized_spec: Optional[Dict[str, Any]] = None
    plan_summary: Optional[Dict[str, Any]] = None
    aws_resource_ids: Optional[List[str]] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.NOT_CHECKED
    verification_details: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None

    # M5 Audit & Execution Metadata (Strictly non-secret)
    aws_account_id: Optional[str] = None
    aws_identity_arn: Optional[str] = None
    aws_region: Optional[str] = None
    execution_mode: str = "mock"
    credential_source_type: Optional[str] = None
    approved_at: Optional[datetime] = None
    execution_started_at: Optional[datetime] = None
    execution_completed_at: Optional[datetime] = None
    terraform_exit_code: Optional[int] = None
    execution_logs: Optional[str] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

