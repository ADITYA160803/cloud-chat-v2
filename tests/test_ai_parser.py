import json
from unittest.mock import MagicMock, patch
import pytest
from core.exceptions import AIParserError, ConfigurationError, ValidationError
from core.models import AWSService, VolumeType
from services.ai_parser import (
    AIInfrastructureParser,
    extract_json_from_response,
    REGION_MAPPINGS,
)

SAMPLE_EC2_JSON = {
    "service": "ec2",
    "region": "ap-south-1",
    "resources": [
        {
            "name": "mumbai-worker",
            "instance_type": "t3.micro",
            "ami_id": None,
            "key_name": None,
            "storage": {
                "size_gb": 8,
                "volume_type": "gp3",
                "encrypted": True,
                "iops": None,
                "throughput": None,
            },
            "network": {
                "public_ip": False,
                "vpc_id": None,
                "subnet_id": None,
                "security_group_ids": [],
                "allowed_ssh_cidrs": [],
            },
            "tags": {"Environment": "Production"},
        }
    ],
    "assumptions": [
        "Default VPC selected",
        "Encrypted gp3 volume defaulted",
    ],
    "missing_information": [],
    "requires_clarification": False,
    "clarification_question": None,
}


def test_extract_json_raw_and_markdown():
    """Verify JSON extraction handles raw JSON, markdown blocks, and surrounding text."""
    raw = json.dumps(SAMPLE_EC2_JSON)
    assert extract_json_from_response(raw)["service"] == "ec2"

    md_wrapped = f"```json\n{raw}\n```"
    assert extract_json_from_response(md_wrapped)["region"] == "ap-south-1"

    text_wrapped = f"Here is the parsed infrastructure specification:\n```\n{raw}\n```\nHope that helps!"
    assert extract_json_from_response(text_wrapped)["resources"][0]["instance_type"] == "t3.micro"


def test_extract_json_malformed():
    """Verify invalid JSON raises AIParserError."""
    with pytest.raises(AIParserError):
        extract_json_from_response("This is not JSON at all.")


def test_groq_parser_successful_interpretation():
    """Verify AI parser with mocked Groq response."""
    parser = AIInfrastructureParser(provider="groq")

    with patch.object(parser, "_call_groq", return_value=json.dumps(SAMPLE_EC2_JSON)):
        res = parser.parse("Create a private EC2 in Mumbai with t3.micro and 8 GB storage")
        assert res.service == AWSService.EC2
        assert res.region == "ap-south-1"
        assert len(res.resources) == 1
        assert res.resources[0].instance_type == "t3.micro"
        assert res.resources[0].storage.size_gb == 8
        assert res.resources[0].storage.volume_type == VolumeType.GP3
        assert res.resources[0].storage.encrypted is True
        assert res.resources[0].network.public_ip is False
        assert res.requires_clarification is False


def test_gemini_parser_successful_interpretation():
    """Verify AI parser with mocked Gemini response."""
    parser = AIInfrastructureParser(provider="gemini")

    with patch.object(parser, "_call_gemini", return_value=json.dumps(SAMPLE_EC2_JSON)):
        res = parser.parse("Create an EC2 instance in ap-south-1")
        assert res.service == AWSService.EC2
        assert res.region == "ap-south-1"
        assert res.resources[0].instance_type == "t3.micro"


def test_region_normalization():
    """Verify common region names (e.g. 'mumbai') map to standard AWS region codes."""
    sample_with_city = dict(SAMPLE_EC2_JSON)
    sample_with_city["region"] = "Mumbai"

    parser = AIInfrastructureParser(provider="groq")
    with patch.object(parser, "_call_groq", return_value=json.dumps(sample_with_city)):
        res = parser.parse("Launch instance in Mumbai")
        assert res.region == "ap-south-1"


def test_clarification_flag_propagation():
    """Verify requests with missing critical information flag clarification."""
    sample_clarification = dict(SAMPLE_EC2_JSON)
    sample_clarification["requires_clarification"] = True
    sample_clarification["clarification_question"] = "Please specify instance type or CPU/RAM requirements."

    parser = AIInfrastructureParser(provider="groq")
    with patch.object(parser, "_call_groq", return_value=json.dumps(sample_clarification)):
        res = parser.parse("Create a server")
        assert res.requires_clarification is True
        assert "specify" in res.clarification_question.lower()


def test_missing_api_key_raises_configuration_error():
    """Verify calling without configured API keys raises ConfigurationError."""
    from config import AppConfig

    parser = AIInfrastructureParser(provider="groq")
    empty_cfg = AppConfig(groq_api_key="", gemini_api_key="")
    with patch("services.ai_parser.config", empty_cfg):
        with pytest.raises(ConfigurationError) as exc_info:
            parser.parse("Create an EC2 instance")
        assert "GROQ_API_KEY" in str(exc_info.value)



def test_empty_prompt_raises_validation_error():
    """Verify empty or whitespace-only prompts raise ValidationError."""
    parser = AIInfrastructureParser(provider="groq")
    with pytest.raises(ValidationError):
        parser.parse("   ")


def test_invalid_instance_type_validation():
    """Verify malformed instance types fail Pydantic model validation."""
    malformed = dict(SAMPLE_EC2_JSON)
    malformed["resources"] = [
        {
            "name": "bad-instance",
            "instance_type": "invalidformat",  # missing period (e.g. t3.micro)
            "storage": {"size_gb": 10},
            "network": {},
        }
    ]
    parser = AIInfrastructureParser(provider="groq")
    with patch.object(parser, "_call_groq", return_value=json.dumps(malformed)):
        with pytest.raises(ValidationError):
            parser.parse("Create an invalid instance")


def test_existing_vpc_subnet_missing_ids_requires_clarification():
    """Verify that requesting existing VPC and subnet without IDs sets clarification and missing info."""
    prompt = "Create a private EC2 instance in Mumbai with t3.micro, 8 GB encrypted gp3 storage, using my existing VPC and subnet."
    mock_llm_output = {
        "service": "ec2",
        "region": "ap-south-1",
        "resources": [
            {
                "name": "app-server",
                "instance_type": "t3.micro",
                "storage": {
                    "size_gb": 8,
                    "volume_type": "gp3",
                    "encrypted": True,
                },
                "network": {
                    "public_ip": False,
                    "vpc_id": None,
                    "subnet_id": None,
                    "use_existing_vpc": True,
                    "use_existing_subnet": True,
                },
                "tags": {},
            }
        ],
        "assumptions": ["Used default VPC because no specific VPC ID was provided", "Enabled EBS gp3 encryption"],
        "missing_information": [],
        "requires_clarification": False,
        "clarification_question": None,
    }

    parser = AIInfrastructureParser(provider="groq")
    with patch.object(parser, "_call_groq", return_value=json.dumps(mock_llm_output)):
        res = parser.parse(prompt)

        # Basic infrastructure preservation
        assert res.service == AWSService.EC2
        assert res.region == "ap-south-1"
        assert res.resources[0].instance_type == "t3.micro"
        assert res.resources[0].storage.size_gb == 8
        assert res.resources[0].storage.encrypted is True
        assert res.resources[0].network.public_ip is False

        # Existing VPC and Subnet required
        assert res.resources[0].network.use_existing_vpc is True
        assert res.resources[0].network.use_existing_subnet is True
        assert res.resources[0].network.vpc_id is None
        assert res.resources[0].network.subnet_id is None

        # Clarification and missing info enforced
        assert res.requires_clarification is True
        assert "Existing VPC ID or selection required" in res.missing_information
        assert "Existing Subnet ID or selection required" in res.missing_information
        assert res.clarification_question is not None
        assert "VPC" in res.clarification_question and "subnet" in res.clarification_question.lower()

        # Default VPC assumption must be removed
        for a in res.assumptions:
            assert "default vpc" not in a.lower()


def test_default_vpc_not_silently_selected_when_existing_vpc_requested():
    """Verify that default VPC is never silently selected when existing VPC is requested."""
    prompt = "Deploy an EC2 instance in Mumbai using my VPC"
    mock_llm_output = {
        "service": "ec2",
        "region": "ap-south-1",
        "resources": [
            {
                "name": "app-server",
                "instance_type": "t3.micro",
                "network": {
                    "public_ip": False,
                    "vpc_id": None,
                    "use_existing_vpc": True,
                },
            }
        ],
        "assumptions": ["Used default VPC because no specific VPC ID was provided"],
        "missing_information": [],
        "requires_clarification": False,
    }

    parser = AIInfrastructureParser(provider="groq")
    with patch.object(parser, "_call_groq", return_value=json.dumps(mock_llm_output)):
        res = parser.parse(prompt)
        assert res.requires_clarification is True
        assert "Existing VPC ID or selection required" in res.missing_information
        assert not any("default vpc" in a.lower() for a in res.assumptions)


def test_explicit_vpc_and_subnet_ids_preserved():
    """Verify that explicitly provided VPC and Subnet IDs are preserved without clarification."""
    prompt = "Create an EC2 instance in vpc-0123456789abcdef0 and subnet-0987654321fedcba0"
    mock_llm_output = {
        "service": "ec2",
        "region": "us-east-1",
        "resources": [
            {
                "name": "app-server",
                "instance_type": "t3.micro",
                "network": {
                    "public_ip": False,
                    "vpc_id": "vpc-0123456789abcdef0",
                    "subnet_id": "subnet-0987654321fedcba0",
                    "use_existing_vpc": True,
                    "use_existing_subnet": True,
                },
                "tags": {},
            }
        ],
        "assumptions": [],
        "missing_information": [],
        "requires_clarification": False,
        "clarification_question": None,
    }

    parser = AIInfrastructureParser(provider="groq")
    with patch.object(parser, "_call_groq", return_value=json.dumps(mock_llm_output)):
        res = parser.parse(prompt)
        assert res.resources[0].network.vpc_id == "vpc-0123456789abcdef0"
        assert res.resources[0].network.subnet_id == "subnet-0987654321fedcba0"
        assert res.requires_clarification is False
        assert len(res.missing_information) == 0


def test_private_networking_requirement_intact():
    """Verify that private networking requirement is strictly kept intact."""
    prompt = "Create a private EC2 instance in Mumbai"
    mock_llm_output = {
        "service": "ec2",
        "region": "ap-south-1",
        "resources": [
            {
                "name": "app-server",
                "instance_type": "t3.micro",
                "network": {
                    "public_ip": True,  # LLM erroneously set public_ip=True
                },
            }
        ],
        "assumptions": [],
        "missing_information": [],
        "requires_clarification": False,
    }

    parser = AIInfrastructureParser(provider="groq")
    with patch.object(parser, "_call_groq", return_value=json.dumps(mock_llm_output)):
        res = parser.parse(prompt)
        # Parser must override and enforce private networking
        assert res.resources[0].network.public_ip is False


def test_arbitrary_tags_not_added_unless_requested():
    """Verify arbitrary tags like Environment=Production are stripped when not requested."""
    prompt = "Create an EC2 instance in Mumbai"
    mock_llm_output = {
        "service": "ec2",
        "region": "ap-south-1",
        "resources": [
            {
                "name": "app-server",
                "instance_type": "t3.micro",
                "tags": {"Environment": "Production", "CreatedBy": "AI"},
            }
        ],
        "assumptions": [],
        "missing_information": [],
        "requires_clarification": False,
    }

    parser = AIInfrastructureParser(provider="groq")
    with patch.object(parser, "_call_groq", return_value=json.dumps(mock_llm_output)):
        res = parser.parse(prompt)
        assert res.resources[0].tags == {}

