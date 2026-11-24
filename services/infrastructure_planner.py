import re
from typing import Any, List, Optional
from core.exceptions import ValidationError
from core.models import (
    AMIResolutionResult,
    AWSService,
    DependencyItem,
    DependencyStatus,
    EC2ResourceSpec,
    InterpretedInfrastructure,
    NetworkSpec,
    NormalizedEC2Spec,
    NormalizedInfrastructureSpec,
    PlannerReadinessStatus,
    PlannerResult,
    StorageSpec,
    VolumeType,
)
from services.ami_resolver import AMIResolver
from services.dependency_resolver import DependencyResolver
from services.cost_estimator import CostEstimator


class InfrastructurePlanner:
    """Transforms interpreted natural-language requirements into a normalized,

    validated, and cost-estimated infrastructure specification.
    """

    def __init__(
        self,
        ami_resolver: Optional[AMIResolver] = None,
        dependency_resolver: Optional[DependencyResolver] = None,
        cost_estimator: Optional[CostEstimator] = None,
    ):
        self.ami_resolver = ami_resolver or AMIResolver()
        self.dependency_resolver = dependency_resolver or DependencyResolver()
        self.cost_estimator = cost_estimator or CostEstimator()

    def plan(self, interpreted: InterpretedInfrastructure) -> PlannerResult:
        """Main planning pipeline for converting interpreted requirements into a complete deployment plan."""
        validation_errors: List[str] = []
        assumptions: List[str] = list(interpreted.assumptions)
        missing_info: List[str] = list(interpreted.missing_information)

        if not interpreted.resources:
            validation_errors.append("No resources defined in interpreted infrastructure.")
            return PlannerResult(
                readiness=PlannerReadinessStatus.VALIDATION_FAILED,
                validation_errors=validation_errors,
                assumptions=assumptions,
                missing_information=missing_info,
            )

        # Focus initially on EC2 (Milestone 3 primary objective)
        res = interpreted.resources[0]
        from services.region_service import get_selected_region
        target_region = (interpreted.region or "").strip() or get_selected_region()

        # 1. Validate instance sizing & storage constraints
        self._validate_instance_and_storage(res, validation_errors)

        # 2. Resolve AMI
        os_preference = "Amazon Linux 2023"
        prompt_lower = interpreted.user_request.lower()
        if "ubuntu" in prompt_lower:
            os_preference = "Ubuntu 22.04"
        elif "debian" in prompt_lower:
            os_preference = "Debian 12"

        ami_result = self.ami_resolver.resolve(
            region=target_region,
            instance_type=res.instance_type,
            explicit_ami_id=res.ami_id,
            os_preference=os_preference,
        )

        if ami_result.status == "lookup_failed":
            validation_errors.append(ami_result.message or "AMI resolution failed.")

        # 3. Resolve Dependencies
        dependencies = self.dependency_resolver.resolve_dependencies(
            region=target_region,
            resource=res,
            ami_result=ami_result,
        )

        # 4. Check for missing dependency inputs (e.g. existing VPC/subnet selection required)
        for dep in dependencies:
            if dep.status == DependencyStatus.REQUIRES_SELECTION:
                if dep.name not in [m.split()[0] for m in missing_info]:
                    missing_info.append(f"{dep.name} selection or ID required")

        # 5. Cost Estimation
        cost_estimate = self.cost_estimator.estimate_ec2(
            region=target_region,
            resource=res,
        )

        # 6. Build Normalized Infrastructure Spec
        # Ensure canonical Pydantic model instances for storage, network, and ami
        storage_input = res.storage
        if isinstance(storage_input, StorageSpec):
            pass
        elif hasattr(storage_input, "model_dump"):
            storage_input = StorageSpec.model_validate(storage_input.model_dump())
        elif isinstance(storage_input, dict):
            storage_input = StorageSpec.model_validate(storage_input)

        network_input = res.network
        if isinstance(network_input, NetworkSpec):
            pass
        elif hasattr(network_input, "model_dump"):
            network_input = NetworkSpec.model_validate(network_input.model_dump())
        elif isinstance(network_input, dict):
            network_input = NetworkSpec.model_validate(network_input)

        ami_input = ami_result
        if isinstance(ami_input, AMIResolutionResult):
            pass
        elif hasattr(ami_input, "model_dump"):
            ami_input = AMIResolutionResult.model_validate(ami_input.model_dump())
        elif isinstance(ami_input, dict):
            ami_input = AMIResolutionResult.model_validate(ami_input)

        normalized_ec2 = NormalizedEC2Spec(
            name=res.name,
            instance_type=res.instance_type,
            region=target_region,
            ami=ami_input,
            storage=storage_input,
            network=network_input,
            tags=res.tags,
        )

        normalized_spec = NormalizedInfrastructureSpec(
            service=interpreted.service,
            region=target_region,
            resources=[normalized_ec2],
            assumptions=assumptions,
            user_request=interpreted.user_request,
        )

        # 7. Evaluate Readiness
        readiness = self._evaluate_readiness(dependencies, validation_errors, ami_result)

        return PlannerResult(
            readiness=readiness,
            normalized_spec=normalized_spec,
            dependencies=dependencies,
            ami_resolution=ami_result,
            cost_estimate=cost_estimate,
            assumptions=assumptions,
            missing_information=missing_info,
            validation_errors=validation_errors,
        )

    def _validate_instance_and_storage(self, res: EC2ResourceSpec, errors: List[str]) -> None:
        """Validates instance type format and storage configuration bounds."""
        # Instance type format check
        if not re.match(r"^[a-z0-9]+\.[a-z0-9]+$", res.instance_type):
            errors.append(f"Invalid EC2 instance type format: '{res.instance_type}' (expected e.g. 't3.micro').")

        # Storage bounds check
        stg = res.storage
        if stg.size_gb < 1 or stg.size_gb > 16384:
            errors.append(f"EBS storage size {stg.size_gb} GiB is out of AWS bounds (must be between 1 and 16,384 GiB).")

        if stg.volume_type == VolumeType.GP3:
            if stg.iops is not None and (stg.iops < 3000 or stg.iops > 16000):
                errors.append(f"gp3 IOPS ({stg.iops}) must be between 3,000 and 16,000.")
            if stg.throughput is not None and (stg.throughput < 125 or stg.throughput > 1000):
                errors.append(f"gp3 throughput ({stg.throughput} MiB/s) must be between 125 and 1,000 MiB/s.")

    def _evaluate_readiness(
        self,
        dependencies: List[DependencyItem],
        validation_errors: List[str],
        ami_result: Any,
    ) -> PlannerReadinessStatus:
        """Determines overall readiness status based on dependency satisfaction and validation errors."""
        if validation_errors:
            return PlannerReadinessStatus.VALIDATION_FAILED

        # Check if any required dependency requires user selection (e.g. existing VPC/subnet)
        requires_selection = any(
            dep.required and dep.status == DependencyStatus.REQUIRES_SELECTION
            for dep in dependencies
        )
        if requires_selection:
            return PlannerReadinessStatus.NEEDS_USER_INPUT

        # Check if AMI lookup was unavailable due to missing credentials
        if ami_result.status == "unresolved":
            return PlannerReadinessStatus.AWS_LOOKUP_UNAVAILABLE

        # Check for any unresolved required dependency
        unresolved = any(
            dep.required and dep.status == DependencyStatus.UNRESOLVED
            for dep in dependencies
        )
        if unresolved:
            return PlannerReadinessStatus.NEEDS_USER_INPUT

        return PlannerReadinessStatus.READY
