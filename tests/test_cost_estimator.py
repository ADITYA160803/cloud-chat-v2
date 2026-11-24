import pytest
from core.models import EC2ResourceSpec, StorageSpec, VolumeType
from services.cost_estimator import CostEstimator


def test_cost_estimator_basic_ec2_calculation():
    """Verify basic compute and storage calculation for t3.micro in us-east-1."""
    estimator = CostEstimator()
    res = EC2ResourceSpec(
        instance_type="t3.micro",
        storage=StorageSpec(size_gb=8, volume_type=VolumeType.GP3),
    )

    cost = estimator.estimate_ec2("us-east-1", res)

    assert cost.region == "us-east-1"
    assert cost.hours_per_month == 730
    assert cost.is_available is True

    # Compute: 0.0104 * 730 = 7.592 -> 7.59
    compute_item = next(i for i in cost.items if "Compute" in i.resource_type)
    assert compute_item.hourly_cost == 0.0104
    assert compute_item.monthly_cost == 7.59

    # Storage: 8 GB * $0.08 = 0.64
    storage_item = next(i for i in cost.items if "Storage" in i.resource_type or "EBS" in i.resource_type)
    assert storage_item.monthly_cost == 0.64

    # Total monthly
    assert cost.total_monthly_cost == round(7.59 + 0.64, 2)

    # Stopped instance monthly: compute is 0, storage persists
    assert cost.stopped_monthly_cost == 0.64


def test_cost_estimator_regional_pricing():
    """Verify regional rate differences between Mumbai (ap-south-1) and N. Virginia (us-east-1)."""
    estimator = CostEstimator()
    res = EC2ResourceSpec(
        instance_type="t3.micro",
        storage=StorageSpec(size_gb=20, volume_type=VolumeType.GP3),
    )

    cost_mumbai = estimator.estimate_ec2("ap-south-1", res)
    cost_virginia = estimator.estimate_ec2("us-east-1", res)

    # In Mumbai, rates are higher (0.0114 vs 0.0104)
    assert cost_mumbai.total_hourly_cost > cost_virginia.total_hourly_cost
    assert cost_mumbai.total_monthly_cost > cost_virginia.total_monthly_cost


def test_cost_estimator_unknown_instance_type():
    """Verify graceful handling without fabrication when instance type pricing is unknown."""
    estimator = CostEstimator()
    res = EC2ResourceSpec(
        instance_type="u-12tb1.metal",
        storage=StorageSpec(size_gb=50),
    )

    cost = estimator.estimate_ec2("us-east-1", res)
    assert cost.is_available is False
    compute_item = next(i for i in cost.items if "Compute" in i.resource_type)
    assert compute_item.hourly_cost == 0.0
    assert compute_item.monthly_cost == 0.0
    assert "unavailable" in compute_item.notes.lower()
