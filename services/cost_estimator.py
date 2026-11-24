from typing import Dict, List, Optional
from core.models import CostEstimate, CostItem, DeploymentMode, EC2ResourceSpec, VolumeType

# Standard on-demand pricing reference (USD)
# (Hourly compute rates by region and instance type)
EC2_HOURLY_PRICING: Dict[str, Dict[str, float]] = {
    "us-east-1": {
        "t3.nano": 0.0052,
        "t3.micro": 0.0104,
        "t3.small": 0.0208,
        "t3.medium": 0.0416,
        "t3.large": 0.0832,
        "t3.xlarge": 0.1664,
        "t2.nano": 0.0058,
        "t2.micro": 0.0116,
        "t2.small": 0.023,
        "t2.medium": 0.0464,
        "t4g.nano": 0.0042,
        "t4g.micro": 0.0084,
        "t4g.small": 0.0168,
        "t4g.medium": 0.0336,
        "c5.large": 0.085,
        "c5.xlarge": 0.17,
        "m5.large": 0.096,
        "m5.xlarge": 0.192,
        "r5.large": 0.126,
    },
    "ap-south-1": {
        "t3.nano": 0.0057,
        "t3.micro": 0.0114,
        "t3.small": 0.0228,
        "t3.medium": 0.0456,
        "t3.large": 0.0912,
        "t3.xlarge": 0.1824,
        "t2.nano": 0.0064,
        "t2.micro": 0.0128,
        "t2.small": 0.0256,
        "t2.medium": 0.0512,
        "t4g.nano": 0.0046,
        "t4g.micro": 0.0092,
        "t4g.small": 0.0184,
        "t4g.medium": 0.0368,
        "c5.large": 0.093,
        "c5.xlarge": 0.186,
        "m5.large": 0.106,
        "m5.xlarge": 0.212,
        "r5.large": 0.138,
    },
    "us-west-2": {
        "t3.nano": 0.0052,
        "t3.micro": 0.0104,
        "t3.small": 0.0208,
        "t3.medium": 0.0416,
        "t3.large": 0.0832,
        "t3.xlarge": 0.1664,
        "t4g.micro": 0.0084,
        "m5.large": 0.096,
    },
    "eu-west-1": {
        "t3.nano": 0.0057,
        "t3.micro": 0.0114,
        "t3.small": 0.0228,
        "t3.medium": 0.0456,
        "t3.large": 0.0912,
        "t3.xlarge": 0.1824,
        "t4g.micro": 0.0092,
        "m5.large": 0.107,
    },
}

# EBS monthly pricing per GiB
EBS_MONTHLY_PER_GB: Dict[str, Dict[VolumeType, float]] = {
    "us-east-1": {
        VolumeType.GP3: 0.08,
        VolumeType.GP2: 0.10,
        VolumeType.IO2: 0.125,
        VolumeType.IO1: 0.125,
        VolumeType.STANDARD: 0.05,
    },
    "ap-south-1": {
        VolumeType.GP3: 0.088,
        VolumeType.GP2: 0.114,
        VolumeType.IO2: 0.142,
        VolumeType.IO1: 0.142,
        VolumeType.STANDARD: 0.057,
    },
    "us-west-2": {
        VolumeType.GP3: 0.08,
        VolumeType.GP2: 0.10,
        VolumeType.IO2: 0.125,
        VolumeType.IO1: 0.125,
        VolumeType.STANDARD: 0.05,
    },
    "eu-west-1": {
        VolumeType.GP3: 0.088,
        VolumeType.GP2: 0.11,
        VolumeType.IO2: 0.138,
        VolumeType.IO1: 0.138,
        VolumeType.STANDARD: 0.055,
    },
}


class CostEstimator:
    """Estimates infrastructure costs transparently based on standard AWS pricing data."""

    HOURS_PER_MONTH = 730

    def estimate_ec2(self, region: Optional[str], resource: EC2ResourceSpec) -> CostEstimate:
        """Calculates cost estimate for an EC2 instance and its attached EBS volume."""
        from services.region_service import get_selected_region
        norm_region = (region or "").lower().strip() or get_selected_region().lower()
        items: List[CostItem] = []
        assumptions: List[str] = [
            f"Monthly calculation is based on {self.HOURS_PER_MONTH} continuously running hours.",
            "Estimates assume standard AWS On-Demand instance rates.",
            "Network egress data transfer is not included in base estimates.",
        ]

        # 1. Compute Cost
        region_pricing = EC2_HOURLY_PRICING.get(norm_region, EC2_HOURLY_PRICING.get("ap-south-1", EC2_HOURLY_PRICING.get("us-east-1", {})))
        inst_type = resource.instance_type.lower()
        hourly_rate = region_pricing.get(inst_type)

        if hourly_rate is not None:
            monthly_compute = round(hourly_rate * self.HOURS_PER_MONTH, 2)
            items.append(
                CostItem(
                    resource_type=f"EC2 Compute ({resource.instance_type})",
                    hourly_cost=hourly_rate,
                    monthly_cost=monthly_compute,
                    notes=f"On-demand rate in {norm_region}: ${hourly_rate:.4f}/hour",
                )
            )
        else:
            items.append(
                CostItem(
                    resource_type=f"EC2 Compute ({resource.instance_type})",
                    hourly_cost=0.0,
                    monthly_cost=0.0,
                    notes=f"Pricing reference is unavailable for '{resource.instance_type}' in {norm_region}.",
                )
            )
            assumptions.append(f"Notice: Specific pricing for '{resource.instance_type}' was unavailable; marked as $0.00.")

        # 2. EBS Storage Cost
        stg = resource.storage
        ebs_rates = EBS_MONTHLY_PER_GB.get(norm_region, EBS_MONTHLY_PER_GB.get("us-east-1", {}))
        monthly_per_gb = ebs_rates.get(stg.volume_type, 0.08)
        monthly_storage = round(stg.size_gb * monthly_per_gb, 2)
        hourly_storage = round(monthly_storage / self.HOURS_PER_MONTH, 5)

        items.append(
            CostItem(
                resource_type=f"EBS Volume ({stg.size_gb} GiB {stg.volume_type.value.upper()})",
                hourly_cost=hourly_storage,
                monthly_cost=monthly_storage,
                notes=f"Rate: ${monthly_per_gb:.3f}/GiB-month in {norm_region}",
            )
        )

        # 3. Networking Cost (Transparent & Distinct)
        is_public = (
            getattr(resource.network, "deployment_mode", None) == DeploymentMode.PUBLIC
            or resource.network.public_ip
        )
        if resource.network.public_ip:
            # Standard in-use public IPv4 address rate: $0.005/hr
            ipv4_hourly = 0.005
            ipv4_monthly = round(ipv4_hourly * self.HOURS_PER_MONTH, 2)
            items.append(
                CostItem(
                    resource_type="Networking (Public IPv4 Address)",
                    hourly_cost=ipv4_hourly,
                    monthly_cost=ipv4_monthly,
                    notes=f"Standard AWS in-use public IPv4 charge: ${ipv4_hourly:.3f}/hour in {norm_region}",
                )
            )
            assumptions.append("Includes standard AWS Public IPv4 address rate ($0.005/hr).")
        else:
            mode_lbl = "Private Subnet Routing" if is_public is False else "Public Subnet (No Public IP)"
            items.append(
                CostItem(
                    resource_type=f"Networking ({mode_lbl})",
                    hourly_cost=0.0,
                    monthly_cost=0.0,
                    notes="Direct VPC routing incurs no additional AWS charges. No NAT Gateway is provisioned.",
                )
            )
            assumptions.append("No NAT Gateway or additional billable networking resources are provisioned.")

        # 4. Stopped-instance Cost
        # When an instance is stopped, compute charges cease ($0), but EBS volumes persist and incur storage charges
        stopped_monthly = monthly_storage

        total_hourly = round(sum(i.hourly_cost for i in items), 4)
        total_monthly = round(sum(i.monthly_cost for i in items), 2)

        return CostEstimate(
            region=norm_region,
            total_hourly_cost=total_hourly,
            total_monthly_cost=total_monthly,
            stopped_monthly_cost=stopped_monthly,
            items=items,
            assumptions=assumptions,
            currency="USD",
            hours_per_month=self.HOURS_PER_MONTH,
            pricing_source="AWS Standard Regional Pricing Reference",
            is_available=hourly_rate is not None,
        )
