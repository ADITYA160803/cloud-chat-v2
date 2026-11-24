from unittest.mock import MagicMock
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from services.ami_resolver import AMIResolver, determine_architecture


def test_determine_architecture():
    """Verify architecture inference for x86_64 vs arm64 (Graviton)."""
    assert determine_architecture("t3.micro") == "x86_64"
    assert determine_architecture("t2.micro") == "x86_64"
    assert determine_architecture("m5.large") == "x86_64"
    assert determine_architecture("c5.xlarge") == "x86_64"
    assert determine_architecture("t4g.micro") == "arm64"
    assert determine_architecture("c7g.large") == "arm64"
    assert determine_architecture("m6g.medium") == "arm64"


def test_ami_resolver_successful_lookup():
    """Verify AMI resolution with mocked Boto3 describe_images response."""
    mock_client = MagicMock()
    mock_client.describe_images.return_value = {
        "Images": [
            {
                "ImageId": "ami-0123456789abcdef0",
                "Name": "al2023-ami-2023.4.20240401.0-kernel-6.1-x86_64",
                "Architecture": "x86_64",
                "OwnerId": "amazon",
                "CreationDate": "2024-04-01T12:00:00.000Z",
            },
            {
                "ImageId": "ami-older00000000000",
                "Name": "al2023-ami-2023.3.20240301.0-kernel-6.1-x86_64",
                "Architecture": "x86_64",
                "OwnerId": "amazon",
                "CreationDate": "2024-03-01T12:00:00.000Z",
            },
        ]
    }

    resolver = AMIResolver(ec2_client=mock_client)
    res = resolver.resolve(region="ap-south-1", instance_type="t3.micro")

    assert res.status == "resolved"
    assert res.ami_id == "ami-0123456789abcdef0"
    assert res.architecture == "x86_64"
    assert res.is_explicit is False


def test_ami_resolver_explicit_ami_preservation():
    """Verify explicit AMI provided by user is preserved and verified."""
    mock_client = MagicMock()
    mock_client.describe_images.return_value = {
        "Images": [
            {
                "ImageId": "ami-custom1234567890",
                "Name": "my-custom-ami",
                "Architecture": "x86_64",
                "OwnerId": "123456789012",
                "CreationDate": "2024-01-01T00:00:00.000Z",
            }
        ]
    }

    resolver = AMIResolver(ec2_client=mock_client)
    res = resolver.resolve(
        region="us-east-1",
        instance_type="t3.micro",
        explicit_ami_id="ami-custom1234567890",
    )

    assert res.status == "resolved"
    assert res.ami_id == "ami-custom1234567890"
    assert res.is_explicit is True


def test_ami_resolver_architecture_mismatch():
    """Verify error when explicit AMI architecture conflicts with instance type."""
    mock_client = MagicMock()
    mock_client.describe_images.return_value = {
        "Images": [
            {
                "ImageId": "ami-armonly123456789",
                "Name": "al2023-arm64",
                "Architecture": "arm64",
            }
        ]
    }

    resolver = AMIResolver(ec2_client=mock_client)
    # Requested t3.micro (x86_64), but AMI is arm64
    res = resolver.resolve(
        region="us-east-1",
        instance_type="t3.micro",
        explicit_ami_id="ami-armonly123456789",
    )

    assert res.status == "lookup_failed"
    assert "mismatch" in res.message.lower()


def test_ami_resolver_missing_credentials_does_not_invent_id():
    """Verify resolver reports unresolved status without inventing fake IDs when credentials missing."""
    AMIResolver.clear_cache()
    mock_client = MagicMock()
    mock_client.describe_images.side_effect = NoCredentialsError()

    resolver = AMIResolver(ec2_client=mock_client)
    res = resolver.resolve(region="eu-central-1", instance_type="t3.micro")

    assert res.status == "unresolved"
    assert res.ami_id is None
    assert "credentials" in res.message.lower()



def test_ami_resolver_no_images_found():
    """Verify lookup_failed status when no matching AMI exists."""
    mock_client = MagicMock()
    mock_client.describe_images.return_value = {"Images": []}

    resolver = AMIResolver(ec2_client=mock_client)
    res = resolver.resolve(region="us-east-1", instance_type="t3.micro")

    assert res.status == "lookup_failed"
    assert res.ami_id is None
    assert "no suitable" in res.message.lower()
