import os
import re
from typing import Any, Dict, List, Optional, Tuple
import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from core.exceptions import ConfigurationError
from core.models import (
    AMIResolutionResult,
    DependencyItem,
    DependencyStatus,
    EC2ResourceSpec,
    NetworkSpec,
    StorageSpec,
    SubnetClassification,
)


class DependencyResolver:
    """Identifies, validates, and discovers infrastructure dependencies for EC2."""

    _subnets_cache: Dict[str, List[Dict[str, Any]]] = {}

    def __init__(
        self,
        ec2_client: Optional[Any] = None,
        auth_service: Optional[Any] = None,
        session: Optional[boto3.Session] = None,
    ):
        self._client = ec2_client
        self._auth_service = auth_service
        self._session = session
        self._cache = self._subnets_cache

    def clear_cache(self) -> None:
        """Clears the cached subnet and route table discoveries."""
        self._cache.clear()

    def _get_client(self, region: str):
        if self._client is not None:
            return self._client
        if self._session is not None:
            return self._session.client("ec2", region_name=region)

        auth = self._auth_service
        if auth is None:
            try:
                from services.aws_auth_service import aws_auth_service
                auth = aws_auth_service
            except Exception:
                auth = None

        # Re-hydrate from Streamlit session_state if available and active credentials not loaded in singleton
        try:
            import streamlit as st
            if auth and not auth.get_active_credentials() and "aws_credentials" in st.session_state:
                auth.set_active_credentials(
                    st.session_state["aws_credentials"],
                    st.session_state.get("aws_identity"),
                )
        except Exception:
            pass

        if auth and auth.get_active_credentials():
            try:
                return auth.get_boto3_client("ec2", region_name=region)
            except Exception as e:
                err_str = str(e)
                if "ExpiredToken" in err_str or "expired" in err_str.lower():
                    raise ConfigurationError(
                        "AWS session token has expired. Please reconnect your AWS profile in Settings."
                    ) from e
                raise

        # Check if an identity is active (e.g. named profile selected) but credentials could not be loaded
        if auth and auth.get_active_identity() and auth.get_active_identity().is_valid:
            ident = auth.get_active_identity()
            raise ConfigurationError(
                f"AWS session credentials for '{ident.profile_name or ident.account_id}' could not be located. "
                "Please reconnect your AWS profile in Settings."
            )

        # Do NOT silently fall back to default credentials, mock discovery or IMDS when a named profile is selected.
        # Only fall back to boto3.client if no identity was ever configured (unauthenticated/mock test contexts)
        try:
            return boto3.client("ec2", region_name=region)
        except Exception:
            return None

    def discover_vpcs(self, region: str) -> List[Dict[str, str]]:
        """Discovers available VPCs in the target region via read-only Boto3 call."""
        try:
            client = self._get_client(region)
            if not client:
                return []
            resp = client.describe_vpcs()
            vpcs = []
            for v in resp.get("Vpcs", []):
                vpc_id = v.get("VpcId", "")
                cidr = v.get("CidrBlock", "")
                is_default = v.get("IsDefault", False)
                name = ""
                for t in v.get("Tags", []):
                    if t.get("Key") == "Name":
                        name = t.get("Value", "")
                vpcs.append({
                    "vpc_id": vpc_id,
                    "cidr": cidr,
                    "is_default": is_default,
                    "name": name,
                    "label": f"{vpc_id} ({name or 'unnamed'}) [{cidr}] {'[Default]' if is_default else ''}".strip(),
                })
            return vpcs
        except (ConfigurationError, NoCredentialsError):
            return []
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if "ExpiredToken" in code or "Token" in str(e):
                return []
            return []
        except Exception:
            return []

    def discover_route_tables(self, region: str, vpc_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Discovers route tables in the target region, optionally filtered by VPC ID."""
        try:
            client = self._get_client(region)
            if not client:
                return []
            filters = []
            if vpc_id:
                filters.append({"Name": "vpc-id", "Values": [vpc_id]})
            resp = client.describe_route_tables(Filters=filters) if filters else client.describe_route_tables()
            return resp.get("RouteTables", [])
        except (ConfigurationError, NoCredentialsError):
            return []
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if "ExpiredToken" in code or "Token" in str(e):
                return []
            return []
        except Exception:
            return []

    def classify_subnet(
        self,
        subnet: Dict[str, Any],
        route_tables: List[Dict[str, Any]],
    ) -> Tuple[SubnetClassification, Optional[str], str]:
        """Classifies a subnet as PUBLIC, PRIVATE_WITH_NAT, ISOLATED, or UNKNOWN.

        - PUBLIC: Has default route (0.0.0.0/0 or ::/0) to an Internet Gateway (igw-*).
        - PRIVATE_WITH_NAT: Has default route to a NAT Gateway (nat-*), without direct IGW route.
        - ISOLATED: Has no default route to an Internet Gateway or NAT Gateway.
        - UNKNOWN: Route table information is missing, ambiguous, or unavailable.
        """
        sub_id = subnet.get("SubnetId") or subnet.get("subnet_id")
        vpc_id = subnet.get("VpcId") or subnet.get("vpc_id")

        if not sub_id or not route_tables:
            return SubnetClassification.UNKNOWN, None, "Route table information missing or ambiguous."

        # 1. Match explicit subnet association
        matching_rt = None
        for rt in route_tables:
            for assoc in rt.get("Associations", []):
                if assoc.get("SubnetId") == sub_id:
                    matching_rt = rt
                    break
            if matching_rt:
                break

        # 2. If no explicit association, match the VPC's main route table
        if not matching_rt and vpc_id:
            for rt in route_tables:
                if rt.get("VpcId") == vpc_id:
                    for assoc in rt.get("Associations", []):
                        if assoc.get("Main") is True:
                            matching_rt = rt
                            break
                if matching_rt:
                    break

        if not matching_rt:
            return SubnetClassification.UNKNOWN, None, f"No route table or main route table association found for {sub_id}."

        rt_id = matching_rt.get("RouteTableId", "unknown-rtb")
        routes = matching_rt.get("Routes", [])

        def _is_route_active(r: Dict[str, Any]) -> bool:
            st = str(r.get("State") or r.get("state") or "active").strip().lower()
            return st == "active"

        # Active default routes (0.0.0.0/0 or ::/0). Blackhole and inactive routes are ignored.
        default_routes = [
            r for r in routes
            if (r.get("DestinationCidrBlock") == "0.0.0.0/0" or r.get("DestinationIpv6CidrBlock") == "::/0")
            and _is_route_active(r)
        ]

        # 1. Check for Internet Gateway (igw-*). Note: eigw-* is an Egress-Only IGW and NOT a public IGW.
        igw_route = next(
            (r for r in default_routes if (r.get("GatewayId") or "").startswith("igw-")),
            None,
        )
        if igw_route:
            igw_id = igw_route.get("GatewayId")
            return (
                SubnetClassification.PUBLIC,
                rt_id,
                f"Default route points to Internet Gateway ({igw_id}) via {rt_id}",
            )

        # 2. Check for NAT Gateway (nat-*)
        nat_route = next(
            (r for r in default_routes if (r.get("NatGatewayId") or "").startswith("nat-")),
            None,
        )

        # 3. Check for IPv6 Egress-Only Internet Gateway (eigw-*)
        eigw_route = next(
            (
                r for r in default_routes
                if (r.get("EgressOnlyInternetGatewayId") or "").startswith("eigw-")
                or (r.get("GatewayId") or "").startswith("eigw-")
            ),
            None,
        )

        if nat_route and eigw_route:
            nat_id = nat_route.get("NatGatewayId")
            eigw_id = eigw_route.get("EgressOnlyInternetGatewayId") or eigw_route.get("GatewayId")
            return (
                SubnetClassification.PRIVATE_WITH_NAT,
                rt_id,
                f"Default routes point to NAT Gateway ({nat_id}) and Egress-Only IGW ({eigw_id}) via {rt_id} (private outbound only)",
            )
        elif nat_route:
            nat_id = nat_route.get("NatGatewayId")
            return (
                SubnetClassification.PRIVATE_WITH_NAT,
                rt_id,
                f"Default route points to NAT Gateway ({nat_id}) via {rt_id} without direct IGW",
            )
        elif eigw_route:
            eigw_id = eigw_route.get("EgressOnlyInternetGatewayId") or eigw_route.get("GatewayId")
            return (
                SubnetClassification.PRIVATE_WITH_EGRESS_ONLY_IGW,
                rt_id,
                f"Default route points to Egress-Only Internet Gateway ({eigw_id}) via {rt_id} (outbound IPv6 only, no direct inbound internet access)",
            )

        # 4. Isolated (no active default route to IGW, NAT, or Egress-Only IGW)
        return (
            SubnetClassification.ISOLATED,
            rt_id,
            f"No default route to Internet Gateway or NAT Gateway in route table {rt_id} (isolated routing)",
        )

    def discover_subnets(
        self,
        region: str,
        vpc_id: Optional[str] = None,
        refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """Discovers subnets in the target region, classifies them, and caches results."""
        cache_key = f"{region}_{vpc_id or 'all'}"
        if not refresh and cache_key in self._cache:
            return self._cache[cache_key]

        client = self._get_client(region)
        if not client:
            return []

        try:
            filters = []
            if vpc_id:
                filters.append({"Name": "vpc-id", "Values": [vpc_id]})
            resp = client.describe_subnets(Filters=filters) if filters else client.describe_subnets()

            # Attempt route table discovery
            route_tables = []
            route_error_msg = None
            try:
                route_tables = self.discover_route_tables(region, vpc_id=vpc_id)
            except Exception as e:
                route_error_msg = str(e)

            subnets = []
            for s in resp.get("Subnets", []):
                subnet_id = s.get("SubnetId", "")
                cidr = s.get("CidrBlock", "")
                az = s.get("AvailabilityZone", "")
                s_vpc = s.get("VpcId", "")
                map_public = s.get("MapPublicIpOnLaunch", False)

                if route_error_msg:
                    classification = SubnetClassification.UNKNOWN
                    rt_id = None
                    explanation = f"Route table lookup failed: {route_error_msg}"
                else:
                    classification, rt_id, explanation = self.classify_subnet(s, route_tables)

                label = f"{subnet_id} | {az} | {cidr} | {classification.value}"

                subnets.append({
                    "subnet_id": subnet_id,
                    "vpc_id": s_vpc,
                    "cidr": cidr,
                    "availability_zone": az,
                    "map_public_ip_on_launch": map_public,
                    "route_table_id": rt_id,
                    "classification": classification.value,
                    "classification_enum": classification,
                    "explanation": explanation,
                    "label": label,
                })

            self._cache[cache_key] = subnets
            return subnets
        except Exception:
            return []

    def resolve_dependencies(
        self,
        region: str,
        resource: EC2ResourceSpec,
        ami_result: AMIResolutionResult,
    ) -> List[DependencyItem]:
        """Analyzes all required and optional dependencies for an EC2 deployment."""
        items: List[DependencyItem] = []

        # 1. AMI Dependency
        if ami_result.status == "resolved" and ami_result.ami_id:
            items.append(
                DependencyItem(
                    name="AMI",
                    required=True,
                    status=DependencyStatus.SATISFIED,
                    value=ami_result.ami_id,
                    source="user_supplied" if ami_result.is_explicit else "aws_discovered",
                    message=f"{ami_result.os_preference} ({ami_result.architecture})",
                )
            )
        elif ami_result.status == "unresolved":
            items.append(
                DependencyItem(
                    name="AMI",
                    required=True,
                    status=DependencyStatus.UNRESOLVED,
                    value=None,
                    source="unresolved",
                    message=ami_result.message or "AMI could not be resolved without AWS access or explicit ID.",
                )
            )
        else:
            items.append(
                DependencyItem(
                    name="AMI",
                    required=True,
                    status=DependencyStatus.UNRESOLVED,
                    value=ami_result.ami_id,
                    source="unresolved",
                    message=ami_result.message or "AMI validation failed.",
                )
            )

        # 2. Instance Type Dependency
        items.append(
            DependencyItem(
                name="Instance Type",
                required=True,
                status=DependencyStatus.SATISFIED,
                value=resource.instance_type,
                source="user_supplied",
                message=f"Architecture: {ami_result.architecture}",
            )
        )

        # 3. VPC Dependency
        net = resource.network
        if net.vpc_id:
            vpc_valid, vpc_msg = self._validate_vpc(region, net.vpc_id)
            items.append(
                DependencyItem(
                    name="VPC",
                    required=True,
                    status=DependencyStatus.SATISFIED if vpc_valid else DependencyStatus.UNRESOLVED,
                    value=net.vpc_id,
                    source="user_supplied",
                    message=vpc_msg,
                )
            )
        elif net.use_existing_vpc:
            items.append(
                DependencyItem(
                    name="VPC",
                    required=True,
                    status=DependencyStatus.REQUIRES_SELECTION,
                    value=None,
                    source="unresolved",
                    message="VPC selection or ID is required. Please select or provide a valid existing VPC.",
                )
            )
        else:
            items.append(
                DependencyItem(
                    name="VPC",
                    required=True,
                    status=DependencyStatus.DEFAULT_AVAILABLE,
                    value="default",
                    source="aws_default",
                    message="AWS Default VPC will be queried or selected if available.",
                )
            )

        # 4. Subnet Dependency
        from core.models import DeploymentMode
        is_private = (getattr(net, "deployment_mode", DeploymentMode.PRIVATE) == DeploymentMode.PRIVATE)
        if net.subnet_id:
            sub_valid, sub_msg = self._validate_subnet(
                region,
                net.subnet_id,
                net.vpc_id,
                require_private=is_private,
                require_public=not is_private,
            )
            items.append(
                DependencyItem(
                    name="Subnet",
                    required=True,
                    status=DependencyStatus.SATISFIED if sub_valid else DependencyStatus.UNRESOLVED,
                    value=net.subnet_id,
                    source="user_supplied",
                    message=sub_msg,
                )
            )
        elif net.use_existing_subnet:
            items.append(
                DependencyItem(
                    name="Subnet",
                    required=True,
                    status=DependencyStatus.REQUIRES_SELECTION,
                    value=None,
                    source="unresolved",
                    message="Subnet selection or ID is required. Please select or provide a valid existing Subnet.",
                )
            )
        else:
            items.append(
                DependencyItem(
                    name="Subnet",
                    required=True,
                    status=DependencyStatus.DEFAULT_AVAILABLE,
                    value="default",
                    source="aws_default",
                    message="AWS Default Subnet will be queried or selected if available.",
                )
            )

        # 5. Security Group Dependency
        if net.security_group_ids:
            items.append(
                DependencyItem(
                    name="Security Group",
                    required=True,
                    status=DependencyStatus.SATISFIED,
                    value=", ".join(net.security_group_ids),
                    source="user_supplied",
                    message=f"{len(net.security_group_ids)} existing security group(s) specified.",
                )
            )
        else:
            items.append(
                DependencyItem(
                    name="Security Group",
                    required=True,
                    status=DependencyStatus.DEFAULT_AVAILABLE,
                    value="dedicated-sg",
                    source="safe_default",
                    message="Dedicated restrictive security group will be provisioned.",
                )
            )

        # 6. EBS Storage Configuration Dependency
        stg = resource.storage
        items.append(
            DependencyItem(
                name="EBS Storage",
                required=True,
                status=DependencyStatus.SATISFIED,
                value=f"{stg.size_gb} GiB {stg.volume_type.value.upper()}",
                source="user_supplied",
                message=f"Encryption: {'Enabled (KMS)' if stg.encrypted else 'Disabled'}",
            )
        )

        # 7. Key Pair Dependency (Optional)
        if resource.key_name:
            items.append(
                DependencyItem(
                    name="Key Pair",
                    required=False,
                    status=DependencyStatus.SATISFIED,
                    value=resource.key_name,
                    source="user_supplied",
                    message="SSH key pair assigned for remote access.",
                )
            )
        else:
            items.append(
                DependencyItem(
                    name="Key Pair",
                    required=False,
                    status=DependencyStatus.OPTIONAL,
                    value=None,
                    source="safe_default",
                    message="No SSH key pair attached (IAM/SSM Session Manager recommended).",
                )
            )

        return items

    def _validate_vpc(self, region: str, vpc_id: str) -> Tuple[bool, str]:
        """Validates VPC ID format and verifies existence if AWS access is available."""
        if not re.match(r"^vpc-[0-9a-fA-F]{8,17}$", vpc_id):
            return False, f"Invalid VPC ID format: '{vpc_id}' (expected 'vpc-xxxxxxxx' or 'vpc-xxxxxxxxxxxxxxxxx')."

        try:
            client = self._get_client(region)
        except ConfigurationError as e:
            return False, str(e)

        if not client:
            return True, f"VPC ID '{vpc_id}' format valid (live lookup skipped: no AWS client)."

        try:
            resp = client.describe_vpcs(VpcIds=[vpc_id])
            if resp.get("Vpcs"):
                return True, f"VPC '{vpc_id}' verified in region '{region}'."
            return False, f"VPC '{vpc_id}' not found in region '{region}'."
        except NoCredentialsError:
            return False, "AWS authentication error: Unable to locate credentials. Please connect your AWS profile in Settings."
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            msg = e.response.get("Error", {}).get("Message", str(e))
            if "ExpiredToken" in code or "Token" in msg or "expired" in msg.lower():
                return False, "AWS session token has expired. Please reconnect your AWS profile in Settings."
            if code == "InvalidVpcID.NotFound":
                return False, f"VPC '{vpc_id}' does not exist in region '{region}'."
            return True, f"VPC ID '{vpc_id}' format valid (verification skipped: {code})."
        except Exception as e:
            err_str = str(e)
            if "ExpiredToken" in err_str or "expired" in err_str.lower():
                return False, "AWS session token has expired. Please reconnect your AWS profile in Settings."
            if "NoCredentialsError" in err_str or "credentials" in err_str.lower():
                return False, "AWS authentication error: Unable to locate credentials. Please connect your AWS profile in Settings."
            return True, f"VPC ID '{vpc_id}' format valid (live check unavailable)."

    def inspect_and_classify_subnet(
        self,
        region: str,
        subnet_id: str,
        expected_vpc_id: Optional[str] = None,
    ) -> Tuple[bool, SubnetClassification, Optional[Dict[str, Any]], str]:
        """Inspects a subnet via read-only AWS EC2 APIs, confirms VPC membership, and determines route classification."""
        clean_subnet = (subnet_id or "").strip()
        if not re.match(r"^subnet-[0-9a-fA-F]{8,17}$", clean_subnet):
            return False, SubnetClassification.UNKNOWN, None, f"Invalid Subnet ID format: '{clean_subnet}' (expected 'subnet-xxxxxxxx')."

        try:
            client = self._get_client(region)
        except ConfigurationError as e:
            return False, SubnetClassification.UNKNOWN, None, str(e)

        if not client:
            return True, SubnetClassification.UNKNOWN, None, f"Subnet ID '{clean_subnet}' format valid (live lookup skipped: no AWS client)."

        try:
            resp = client.describe_subnets(SubnetIds=[clean_subnet])
            subnets = resp.get("Subnets", [])
            if not subnets:
                return False, SubnetClassification.UNKNOWN, None, f"Subnet '{clean_subnet}' does not exist in region '{region}'."

            subnet_obj = subnets[0]
            actual_vpc = subnet_obj.get("VpcId", "")
            if expected_vpc_id and actual_vpc != expected_vpc_id:
                return (
                    False,
                    SubnetClassification.UNKNOWN,
                    None,
                    f"Subnet '{clean_subnet}' belongs to VPC '{actual_vpc}', not selected VPC '{expected_vpc_id}'.",
                )

            # Discover route tables for this VPC using the same client / session
            route_tables = self.discover_route_tables(region, vpc_id=actual_vpc)
            classification, rt_id, explanation = self.classify_subnet(subnet_obj, route_tables)

            details = {
                "subnet_id": clean_subnet,
                "vpc_id": actual_vpc,
                "cidr": subnet_obj.get("CidrBlock", ""),
                "availability_zone": subnet_obj.get("AvailabilityZone", ""),
                "map_public_ip_on_launch": subnet_obj.get("MapPublicIpOnLaunch", False),
                "classification": classification.value,
                "route_table_id": rt_id,
                "explanation": explanation,
                "label": f"{clean_subnet} | {subnet_obj.get('AvailabilityZone', '')} | {subnet_obj.get('CidrBlock', '')} | {classification.value}",
            }
            return True, classification, details, explanation

        except NoCredentialsError:
            return (
                False,
                SubnetClassification.UNKNOWN,
                None,
                "AWS authentication error: Unable to locate credentials. Please connect your AWS profile in Settings.",
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            msg = e.response.get("Error", {}).get("Message", str(e))
            if "ExpiredToken" in code or "Token" in msg or "expired" in msg.lower():
                return (
                    False,
                    SubnetClassification.UNKNOWN,
                    None,
                    "AWS session token has expired. Please reconnect your AWS profile in Settings.",
                )
            if code == "InvalidSubnetID.NotFound":
                return False, SubnetClassification.UNKNOWN, None, f"Subnet '{clean_subnet}' does not exist in region '{region}'."
            return False, SubnetClassification.UNKNOWN, None, f"AWS Error describing subnet '{clean_subnet}' [{code}]: {msg}"
        except Exception as e:
            err_str = str(e)
            if "ExpiredToken" in err_str or "expired" in err_str.lower():
                return (
                    False,
                    SubnetClassification.UNKNOWN,
                    None,
                    "AWS session token has expired. Please reconnect your AWS profile in Settings.",
                )
            if "NoCredentialsError" in err_str or "credentials" in err_str.lower():
                return (
                    False,
                    SubnetClassification.UNKNOWN,
                    None,
                    "AWS authentication error: Unable to locate credentials. Please connect your AWS profile in Settings.",
                )
            return False, SubnetClassification.UNKNOWN, None, f"Subnet inspection error: {err_str}"

    def _validate_subnet(
        self,
        region: str,
        subnet_id: str,
        expected_vpc_id: Optional[str],
        require_private: bool = False,
        require_public: bool = False,
    ) -> Tuple[bool, str]:
        """Validates Subnet ID format, confirms it belongs to the selected VPC, and verifies route table classification if required."""
        if not re.match(r"^subnet-[0-9a-fA-F]{8,17}$", subnet_id):
            return False, f"Invalid Subnet ID format: '{subnet_id}' (expected 'subnet-xxxxxxxx')."

        try:
            client = self._get_client(region)
        except ConfigurationError as e:
            return False, str(e)

        if not client:
            return True, f"Subnet ID '{subnet_id}' format valid (live lookup skipped: no AWS client)."

        try:
            resp = client.describe_subnets(SubnetIds=[subnet_id])
            subnets = resp.get("Subnets", [])
            if not subnets:
                return False, f"Subnet '{subnet_id}' not found in region '{region}'."
            actual_vpc = subnets[0].get("VpcId")
            if expected_vpc_id and actual_vpc != expected_vpc_id:
                return False, f"Subnet '{subnet_id}' belongs to VPC '{actual_vpc}', not selected VPC '{expected_vpc_id}'."

            if require_private or require_public:
                route_tables = self.discover_route_tables(region, vpc_id=actual_vpc)
                if not route_tables:
                    return (
                        False,
                        f"Subnet '{subnet_id}' route table information is unavailable or missing.",
                    )
                classification, rt_id, explanation = self.classify_subnet(subnets[0], route_tables)
                if require_private:
                    if classification in (SubnetClassification.PUBLIC, SubnetClassification.UNKNOWN):
                        return (
                            False,
                            f"Subnet '{subnet_id}' is {classification.value} ({explanation}). Private workloads require a verified PRIVATE_WITH_NAT, PRIVATE_WITH_EGRESS_ONLY_IGW, or ISOLATED subnet.",
                        )
                    return True, f"Subnet '{subnet_id}' verified in VPC '{actual_vpc}' and verified as {classification.value}."
                elif require_public:
                    if classification != SubnetClassification.PUBLIC:
                        return (
                            False,
                            f"Subnet '{subnet_id}' is {classification.value} ({explanation}). Public deployment mode requires a verified PUBLIC subnet with an active default route to an Internet Gateway.",
                        )
                    return True, f"Subnet '{subnet_id}' verified in VPC '{actual_vpc}' and verified as PUBLIC."

            return True, f"Subnet '{subnet_id}' verified in VPC '{actual_vpc}'."
        except NoCredentialsError:
            return False, "AWS authentication error: Unable to locate credentials. Please connect your AWS profile in Settings."
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            msg = e.response.get("Error", {}).get("Message", str(e))
            if "ExpiredToken" in code or "Token" in msg or "expired" in msg.lower():
                return False, "AWS session token has expired. Please reconnect your AWS profile in Settings."
            if code == "InvalidSubnetID.NotFound":
                return False, f"Subnet '{subnet_id}' does not exist in region '{region}'."
            return True, f"Subnet ID '{subnet_id}' format valid (verification skipped: {code})."
        except Exception as e:
            err_str = str(e)
            if "ExpiredToken" in err_str or "expired" in err_str.lower():
                return False, "AWS session token has expired. Please reconnect your AWS profile in Settings."
            if "NoCredentialsError" in err_str or "credentials" in err_str.lower():
                return False, "AWS authentication error: Unable to locate credentials. Please connect your AWS profile in Settings."
            return True, f"Subnet ID '{subnet_id}' format valid (live check unavailable)."
