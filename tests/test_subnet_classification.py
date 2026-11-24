import os
from pathlib import Path
import tempfile
import pytest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from core.models import (
    AMIResolutionResult,
    SubnetClassification,
    AWSService,
    EC2ResourceSpec,
    InterpretedInfrastructure,
    NetworkSpec,
    StorageSpec,
    DependencyStatus,
    PlannerReadinessStatus,
)
from database.db import init_db
from database.repository import DeploymentRepository
from services.dependency_resolver import DependencyResolver
from services.infrastructure_planner import InfrastructurePlanner
from ui.chat_view import process_chat_submission, _get_or_init_chat_messages
from ui.intent_view import recalculate_missing_details


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    p = Path(path)
    init_db(p)
    yield p
    if p.exists():
        p.unlink()


@pytest.fixture
def mock_ec2_client():
    client = MagicMock()
    return client


def test_public_subnet_classification_with_igw_route(mock_ec2_client):
    """Test 1: Subnet with default route to an Internet Gateway is classified as PUBLIC."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    subnet = {
        "SubnetId": "subnet-pub1",
        "VpcId": "vpc-001",
        "CidrBlock": "10.0.1.0/24",
        "AvailabilityZone": "ap-south-1a",
        "MapPublicIpOnLaunch": True,
    }
    route_tables = [
        {
            "RouteTableId": "rtb-pub",
            "VpcId": "vpc-001",
            "Associations": [{"SubnetId": "subnet-pub1", "Main": False}],
            "Routes": [
                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-12345678", "State": "active"},
            ],
        }
    ]

    classification, rt_id, explanation = resolver.classify_subnet(subnet, route_tables)
    assert classification == SubnetClassification.PUBLIC
    assert rt_id == "rtb-pub"
    assert "Internet Gateway (igw-12345678)" in explanation


def test_private_subnet_classification_with_nat_route(mock_ec2_client):
    """Test 2: Subnet with default route to a NAT Gateway is classified as PRIVATE_WITH_NAT."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    subnet = {
        "SubnetId": "subnet-priv1",
        "VpcId": "vpc-001",
        "CidrBlock": "10.0.2.0/24",
        "AvailabilityZone": "ap-south-1b",
        "MapPublicIpOnLaunch": False,
    }
    route_tables = [
        {
            "RouteTableId": "rtb-priv",
            "VpcId": "vpc-001",
            "Associations": [{"SubnetId": "subnet-priv1", "Main": False}],
            "Routes": [
                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-87654321", "State": "active"},
            ],
        }
    ]

    classification, rt_id, explanation = resolver.classify_subnet(subnet, route_tables)
    assert classification == SubnetClassification.PRIVATE_WITH_NAT
    assert rt_id == "rtb-priv"
    assert "NAT Gateway (nat-87654321)" in explanation


def test_isolated_subnet_classification_no_igw_no_nat(mock_ec2_client):
    """Test 3: Subnet with no default route to IGW or NAT is classified as ISOLATED."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    subnet = {
        "SubnetId": "subnet-iso1",
        "VpcId": "vpc-001",
        "CidrBlock": "10.0.3.0/24",
        "AvailabilityZone": "ap-south-1c",
        "MapPublicIpOnLaunch": False,
    }
    route_tables = [
        {
            "RouteTableId": "rtb-iso",
            "VpcId": "vpc-001",
            "Associations": [{"SubnetId": "subnet-iso1", "Main": False}],
            "Routes": [
                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
            ],
        }
    ]

    classification, rt_id, explanation = resolver.classify_subnet(subnet, route_tables)
    assert classification == SubnetClassification.ISOLATED
    assert rt_id == "rtb-iso"
    assert "No default route" in explanation


def test_subnet_uses_main_route_table_when_no_explicit_association(mock_ec2_client):
    """Test 4: Subnet with no explicit association inherits classification from the VPC Main route table."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    subnet = {
        "SubnetId": "subnet-main-fallback",
        "VpcId": "vpc-001",
        "CidrBlock": "10.0.4.0/24",
        "AvailabilityZone": "ap-south-1a",
    }
    route_tables = [
        {
            "RouteTableId": "rtb-custom-other",
            "VpcId": "vpc-001",
            "Associations": [{"SubnetId": "subnet-other", "Main": False}],
            "Routes": [
                {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-999", "State": "active"},
            ],
        },
        {
            "RouteTableId": "rtb-vpc-main",
            "VpcId": "vpc-001",
            "Associations": [{"Main": True}],
            "Routes": [
                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-main", "State": "active"},
            ],
        },
    ]

    classification, rt_id, explanation = resolver.classify_subnet(subnet, route_tables)
    assert classification == SubnetClassification.PRIVATE_WITH_NAT
    assert rt_id == "rtb-vpc-main"
    assert "nat-main" in explanation


def test_subnet_to_vpc_relationship_validation(mock_ec2_client):
    """Test 5: Subnet-to-VPC relationship is validated; mismatches are rejected."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    mock_ec2_client.describe_subnets.return_value = {
        "Subnets": [
            {"SubnetId": "subnet-11111111", "VpcId": "vpc-actual"}
        ]
    }

    # Belongs to vpc-actual, but expected vpc-different
    valid, msg = resolver._validate_subnet("ap-south-1", "subnet-11111111", "vpc-different")
    assert not valid
    assert "belongs to VPC 'vpc-actual', not selected VPC 'vpc-different'" in msg

    # Matches expected VPC
    valid_ok, msg_ok = resolver._validate_subnet("ap-south-1", "subnet-11111111", "vpc-actual")
    assert valid_ok
    assert "verified in VPC 'vpc-actual'" in msg_ok


def test_unknown_classification_when_route_info_missing_or_error(mock_ec2_client):
    """Test 6: Subnet classification returns UNKNOWN when route tables are missing or discovery fails."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    subnet = {
        "SubnetId": "subnet-unk1",
        "VpcId": "vpc-unknown",
    }

    # Empty route tables
    classification, rt_id, explanation = resolver.classify_subnet(subnet, [])
    assert classification == SubnetClassification.UNKNOWN
    assert rt_id is None
    assert "missing or ambiguous" in explanation

    # Route tables without matching association or main route table
    route_tables = [
        {
            "RouteTableId": "rtb-other-vpc",
            "VpcId": "vpc-other",
            "Associations": [{"Main": True}],
            "Routes": [],
        }
    ]
    classification2, rt_id2, explanation2 = resolver.classify_subnet(subnet, route_tables)
    assert classification2 == SubnetClassification.UNKNOWN
    assert rt_id2 is None
    assert "No route table or main route table association found" in explanation2


def test_private_ec2_request_rejects_public_subnet_in_planner(mock_ec2_client):
    """Test 7: Private EC2 request rejects a public subnet in dependency resolver and planner."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)

    test_sub_id = "subnet-0123456789abcdef1"
    test_vpc_id = "vpc-0123456789abcdef0"

    mock_ec2_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": test_sub_id,
                "VpcId": test_vpc_id,
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "ap-south-1a",
            }
        ]
    }
    mock_ec2_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-pub",
                "VpcId": test_vpc_id,
                "Associations": [{"SubnetId": test_sub_id, "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-pub", "State": "active"},
                ],
            }
        ]
    }

    # Private EC2 resource (public_ip = False)
    res = EC2ResourceSpec(
        name="test-server",
        instance_type="t3.micro",
        network=NetworkSpec(
            vpc_id=test_vpc_id,
            subnet_id=test_sub_id,
            public_ip=False,  # Private instance requested!
        ),
        storage=StorageSpec(size_gb=20),
    )

    interpreted = InterpretedInfrastructure(
        service=AWSService.EC2,
        region="ap-south-1",
        resources=[res],
        user_request="Launch private EC2",
    )

    planner = InfrastructurePlanner(dependency_resolver=resolver)
    with patch.object(planner.ami_resolver, "resolve") as mock_ami:
        mock_ami.return_value = AMIResolutionResult(
            status="resolved",
            ami_id="ami-12345678",
            architecture="x86_64",
            os_preference="Linux",
            is_explicit=True,
        )
        result = planner.plan(interpreted)

    subnet_dep = next((d for d in result.dependencies if d.name == "Subnet"), None)
    assert subnet_dep is not None
    assert subnet_dep.status == DependencyStatus.UNRESOLVED
    assert "PUBLIC" in subnet_dep.message
    assert "PRIVATE_WITH_NAT" in subnet_dep.message
    assert result.readiness == PlannerReadinessStatus.NEEDS_USER_INPUT


def test_subnet_dropdown_label_formatting(mock_ec2_client):
    """Test 8: Subnet dropdown label formatting conforms to 'subnet-xxxx | az | cidr | CLASSIFICATION'."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    resolver.clear_cache()

    mock_ec2_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-0123456789abcdef0",
                "VpcId": "vpc-001",
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "ap-south-1a",
                "MapPublicIpOnLaunch": True,
            }
        ]
    }
    mock_ec2_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-01",
                "VpcId": "vpc-001",
                "Associations": [{"SubnetId": "subnet-0123456789abcdef0", "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-01", "State": "active"},
                ],
            }
        ]
    }

    subnets = resolver.discover_subnets("ap-south-1", vpc_id="vpc-001", refresh=True)
    assert len(subnets) == 1
    sub = subnets[0]
    expected_label = "subnet-0123456789abcdef0 | ap-south-1a | 10.0.1.0/24 | PUBLIC"
    assert sub["label"] == expected_label
    assert sub["classification"] == "PUBLIC"
    assert sub["map_public_ip_on_launch"] is True


def test_duplicate_chat_message_prevention_on_rerun(temp_db):
    """Test 9: Streamlit reruns of the same submission_id do not create duplicate messages."""
    repo = DeploymentRepository(db_path=temp_db)
    session_state = {"chat_messages": []}

    mock_parser = MagicMock()
    mock_infra = MagicMock(
        service=MagicMock(value="ec2"),
        region="ap-south-1",
        resources=[MagicMock(instance_type="t3.micro")],
        model_dump=lambda: {"service": "ec2", "region": "ap-south-1"},
    )
    mock_parser.parse.return_value = mock_infra

    prompt = "Deploy a private t3.micro server in Mumbai"
    submission_id = "sub-test-rerun-001"

    # First submission
    success1 = process_chat_submission(prompt, submission_id, session_state, repo, parser=mock_parser)
    assert success1 is True
    assert len(session_state["chat_messages"]) == 2  # 1 user + 1 assistant
    assert len(repo.list_chat_messages()) == 2

    # Rerun of the exact same submission_id
    success2 = process_chat_submission(prompt, submission_id, session_state, repo, parser=mock_parser)
    assert success2 is False  # Rejected by idempotency guard

    # Message counts remain strictly 2 (no duplicates!)
    assert len(session_state["chat_messages"]) == 2
    assert len(repo.list_chat_messages()) == 2


def test_same_prompt_multiple_submissions_creates_separate_turns(temp_db):
    """Test 10: Intentionally submitting the same prompt multiple times creates separate user and assistant turns."""
    repo = DeploymentRepository(db_path=temp_db)
    session_state = {"chat_messages": []}

    mock_parser = MagicMock()
    mock_infra = MagicMock(
        service=MagicMock(value="ec2"),
        region="ap-south-1",
        resources=[MagicMock(instance_type="t3.micro")],
        model_dump=lambda: {"service": "ec2", "region": "ap-south-1"},
    )
    mock_parser.parse.return_value = mock_infra

    prompt = "Deploy a private t3.micro server in Mumbai"

    # First submission turn
    sub_id_1 = "sub-turn-1"
    ok1 = process_chat_submission(prompt, sub_id_1, session_state, repo, parser=mock_parser)
    assert ok1 is True
    assert len(session_state["chat_messages"]) == 2
    assert session_state["chat_messages"][0]["message_id"] == "user-sub-turn-1"
    assert session_state["chat_messages"][1]["message_id"] == "ai-sub-turn-1"

    # Second intentional submission turn with the identical prompt
    sub_id_2 = "sub-turn-2"
    ok2 = process_chat_submission(prompt, sub_id_2, session_state, repo, parser=mock_parser)
    assert ok2 is True
    assert len(session_state["chat_messages"]) == 4
    assert session_state["chat_messages"][2]["message_id"] == "user-sub-turn-2"
    assert session_state["chat_messages"][3]["message_id"] == "ai-sub-turn-2"

    # Verify both turns are persisted in database
    db_messages = repo.list_chat_messages()
    assert len(db_messages) == 4


def test_existing_chat_history_preservation_without_duplication(temp_db):
    """Test 11: Existing conversation history is loaded and preserved without duplicate pollution."""
    repo = DeploymentRepository(db_path=temp_db)

    # Pre-seed history with 2 distinct turns
    repo.add_chat_message("msg-1", "user", "Turn 1 user request")
    repo.add_chat_message("msg-2", "assistant", "Turn 1 assistant response")
    repo.add_chat_message("msg-3", "user", "Turn 2 user request")
    repo.add_chat_message("msg-4", "assistant", "Turn 2 assistant response")

    # Load messages
    messages = _get_or_init_chat_messages(repo=repo)
    assert len(messages) == 4
    assert [m["message_id"] for m in messages] == ["msg-1", "msg-2", "msg-3", "msg-4"]


def test_ignore_blackhole_and_inactive_routes_for_subnet_classification(mock_ec2_client):
    """Test 12: Routes in blackhole or inactive state are ignored when classifying connectivity."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    subnet = {
        "SubnetId": "subnet-bh1",
        "VpcId": "vpc-001",
        "CidrBlock": "10.0.1.0/24",
    }

    # Case A: Internet Gateway route is blackholed, but NAT Gateway route is active
    route_tables_a = [
        {
            "RouteTableId": "rtb-bh-a",
            "VpcId": "vpc-001",
            "Associations": [{"SubnetId": "subnet-bh1", "Main": False}],
            "Routes": [
                {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-dead", "State": "blackhole"},
                {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-live", "State": "active"},
            ],
        }
    ]
    cls_a, rt_a, expl_a = resolver.classify_subnet(subnet, route_tables_a)
    assert cls_a == SubnetClassification.PRIVATE_WITH_NAT
    assert "nat-live" in expl_a
    assert cls_a != SubnetClassification.PUBLIC

    # Case B: Only route is a blackholed Internet Gateway -> classified as ISOLATED
    route_tables_b = [
        {
            "RouteTableId": "rtb-bh-b",
            "VpcId": "vpc-001",
            "Associations": [{"SubnetId": "subnet-bh1", "Main": False}],
            "Routes": [
                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-dead", "State": "blackhole"},
            ],
        }
    ]
    cls_b, rt_b, expl_b = resolver.classify_subnet(subnet, route_tables_b)
    assert cls_b == SubnetClassification.ISOLATED
    assert "No default route" in expl_b


def test_ipv6_egress_only_internet_gateway_classified_as_private(mock_ec2_client):
    """Test 13: IPv6 Egress-Only Internet Gateway (eigw-*) route is classified as private, NOT PUBLIC."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    sub_id = "subnet-0123456789abcdefa"
    vpc_id = "vpc-0123456789abcdef0"
    subnet = {
        "SubnetId": sub_id,
        "VpcId": vpc_id,
        "CidrBlock": "10.0.1.0/24",
        "Ipv6CidrBlockAssociationSet": [{"Ipv6CidrBlock": "2001:db8:1234:1a00::/64"}],
    }

    route_tables = [
        {
            "RouteTableId": "rtb-eigw",
            "VpcId": vpc_id,
            "Associations": [{"SubnetId": sub_id, "Main": False}],
            "Routes": [
                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                {
                    "DestinationIpv6CidrBlock": "::/0",
                    "EgressOnlyInternetGatewayId": "eigw-0123456789abcdef0",
                    "State": "active",
                },
            ],
        }
    ]

    cls, rt_id, expl = resolver.classify_subnet(subnet, route_tables)
    assert cls == SubnetClassification.PRIVATE_WITH_EGRESS_ONLY_IGW
    assert cls != SubnetClassification.PUBLIC
    assert "eigw-0123456789abcdef0" in expl
    assert "no direct inbound" in expl

    # Validate that _validate_subnet allows this private subnet for private workloads
    mock_ec2_client.describe_subnets.return_value = {"Subnets": [subnet]}
    mock_ec2_client.describe_route_tables.return_value = {"RouteTables": route_tables}
    valid, msg = resolver._validate_subnet("ap-south-1", sub_id, vpc_id, require_private=True)
    assert valid is True
    assert "verified in VPC" in msg


def test_dual_stack_nat_and_egress_only_igw_classification(mock_ec2_client):
    """Test 14: Dual-stack private subnet with IPv4 NAT Gateway and IPv6 Egress-Only IGW."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    subnet = {
        "SubnetId": "subnet-dual1",
        "VpcId": "vpc-001",
        "CidrBlock": "10.0.1.0/24",
    }

    route_tables = [
        {
            "RouteTableId": "rtb-dual",
            "VpcId": "vpc-001",
            "Associations": [{"SubnetId": "subnet-dual1", "Main": False}],
            "Routes": [
                {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-v4", "State": "active"},
                {"DestinationIpv6CidrBlock": "::/0", "GatewayId": "eigw-v6", "State": "active"},
            ],
        }
    ]

    cls, rt_id, expl = resolver.classify_subnet(subnet, route_tables)
    assert cls == SubnetClassification.PRIVATE_WITH_NAT
    assert "nat-v4" in expl
    assert "eigw-v6" in expl
    assert cls != SubnetClassification.PUBLIC


def test_subnets_caching_and_refresh(mock_ec2_client):
    """Test 15: Subnet discovery caches results; refresh=True updates cache."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    resolver.clear_cache()

    mock_ec2_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-c1",
                "VpcId": "vpc-001",
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "ap-south-1a",
            }
        ]
    }
    mock_ec2_client.describe_route_tables.return_value = {"RouteTables": []}

    # First call: hits AWS client
    res1 = resolver.discover_subnets("ap-south-1", vpc_id="vpc-001")
    assert len(res1) == 1
    assert mock_ec2_client.describe_subnets.call_count == 1

    # Second call without refresh: uses cache, call_count stays 1
    res2 = resolver.discover_subnets("ap-south-1", vpc_id="vpc-001")
    assert len(res2) == 1
    assert mock_ec2_client.describe_subnets.call_count == 1

    # Third call with refresh=True: queries AWS client again
    res3 = resolver.discover_subnets("ap-south-1", vpc_id="vpc-001", refresh=True)
    assert len(res3) == 1
    assert mock_ec2_client.describe_subnets.call_count == 2


def test_valid_vpc_selection_clears_vpc_warning():
    """Test 16: Selecting a valid VPC clears the VPC missing-detail warning while preserving subnet warning."""
    initial_missing = [
        "Existing VPC ID or selection required",
        "Existing Subnet ID or selection required",
    ]
    updated = recalculate_missing_details(
        missing_info=initial_missing,
        vpc_id="vpc-0123456789abcdef0",
        subnet_id="",
        is_vpc_valid=True,
        is_subnet_valid=False,
    )
    assert "Existing VPC ID or selection required" not in updated
    assert "Existing Subnet ID or selection required" in updated
    assert len(updated) == 1


def test_valid_subnet_selection_clears_subnet_warning():
    """Test 17: Selecting a valid subnet belonging to the selected VPC clears the subnet missing-detail warning."""
    initial_missing = [
        "Existing VPC ID or selection required",
        "Existing Subnet ID or selection required",
    ]
    updated = recalculate_missing_details(
        missing_info=initial_missing,
        vpc_id="vpc-0123456789abcdef0",
        subnet_id="subnet-0123456789abcdef0",
        is_vpc_valid=True,
        is_subnet_valid=True,
    )
    assert "Existing VPC ID or selection required" not in updated
    assert "Existing Subnet ID or selection required" not in updated
    assert len(updated) == 0


def test_selecting_public_subnet_still_blocks_private_workload(mock_ec2_client):
    """Test 18: Selecting a PUBLIC subnet clears missing-input warnings but blocks private EC2 workload."""
    resolver = DependencyResolver(ec2_client=mock_ec2_client)
    sub_id = "subnet-0123456789abcdef0"
    vpc_id = "vpc-0123456789abcdef0"

    # Both subnets in this VPC are public
    mock_ec2_client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": sub_id,
                "VpcId": vpc_id,
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "ap-south-1a",
            }
        ]
    }
    mock_ec2_client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-pub1",
                "VpcId": vpc_id,
                "Associations": [{"SubnetId": sub_id, "Main": False}],
                "Routes": [
                    {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-12345678", "State": "active"},
                ],
            }
        ]
    }

    # Recalculate missing inputs: both valid IDs are selected
    missing_details = recalculate_missing_details(
        missing_info=["Existing VPC ID or selection required", "Existing Subnet ID or selection required"],
        vpc_id=vpc_id,
        subnet_id=sub_id,
        is_vpc_valid=True,
        is_subnet_valid=True,
    )
    # Missing input warnings are completely cleared
    assert len(missing_details) == 0

    # Subnet suitability check: classified as PUBLIC
    subnets = resolver.discover_subnets("ap-south-1", vpc_id=vpc_id, refresh=True)
    assert len(subnets) == 1
    assert subnets[0]["classification"] == "PUBLIC"

    # Workload is private: suitability error must be raised
    is_private_workload = True
    suitability_errors = []
    if is_private_workload and subnets[0]["classification"] == "PUBLIC":
        suitability_errors.append(f"Subnet '{sub_id}' is PUBLIC and unsuitable for a private workload.")

    assert len(suitability_errors) == 1
    # Progression must remain blocked because suitability error exists
    can_continue = (not missing_details) and (not suitability_errors)
    assert can_continue is False


def test_changing_vpc_invalidates_previous_subnet():
    """Test 19: Changing selected VPC clears previous subnet selection and restores subnet missing warning."""
    session_state = {
        "intent_last_vpc_id": "vpc-0123456789abcdef0",
        "intent_selected_vpc": "vpc-0123456789abcdef0",
        "intent_selected_subnet": "subnet-0123456789abcdef0",
    }

    new_vpc_id = "vpc-99999999999999999"

    # VPC change detection logic (mirrors intent_view)
    prev_vpc = session_state.get("intent_last_vpc_id")
    if prev_vpc and prev_vpc != new_vpc_id:
        session_state["intent_selected_subnet"] = ""
    session_state["intent_last_vpc_id"] = new_vpc_id

    # Verify previous subnet was cleared
    assert session_state["intent_selected_subnet"] == ""

    # Recalculate missing details with the newly selected VPC and cleared subnet
    missing = recalculate_missing_details(
        missing_info=[],
        vpc_id=new_vpc_id,
        subnet_id=session_state["intent_selected_subnet"],
        is_vpc_valid=True,
        is_subnet_valid=False,
    )
    assert "Existing VPC ID or selection required" not in missing
    assert "Existing Subnet ID or selection required" in missing
    assert len(missing) == 1


def test_missing_selections_still_block_progression():
    """Test 20: Missing VPC or Subnet selections populate missing details and block progression."""
    # Case A: Neither selected
    missing_both = recalculate_missing_details(
        missing_info=[],
        vpc_id="",
        subnet_id="",
        is_vpc_valid=False,
        is_subnet_valid=False,
    )
    assert "Existing VPC ID or selection required" in missing_both
    assert "Existing Subnet ID or selection required" in missing_both
    can_continue_both = not bool(missing_both)
    assert can_continue_both is False

    # Case B: Only VPC selected
    missing_sub = recalculate_missing_details(
        missing_info=[],
        vpc_id="vpc-11111111111111111",
        subnet_id="",
        is_vpc_valid=True,
        is_subnet_valid=False,
    )
    assert "Existing VPC ID or selection required" not in missing_sub
    assert "Existing Subnet ID or selection required" in missing_sub
    can_continue_sub = not bool(missing_sub)
    assert can_continue_sub is False

