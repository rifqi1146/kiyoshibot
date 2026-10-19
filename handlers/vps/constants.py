DEFAULT_REGION = "ap-southeast-5"

REGION_LABELS = {
    "ap-southeast-5": "Malaysia (Kuala Lumpur)",
    "ap-southeast-1": "Singapore",
    "ap-southeast-2": "Sydney",
    "ap-southeast-3": "Jakarta",
    "ap-southeast-6": "Thailand (Bangkok)",
    "ap-southeast-7": "Taiwan (Taipei)",
    "ap-northeast-1": "Tokyo",
    "ap-northeast-2": "Seoul",
    "ap-northeast-3": "Osaka",
    "ap-south-1": "Mumbai",
    "ap-south-2": "Hyderabad",
    "ap-east-1": "Hong Kong",
    "ap-east-2": "Taipei (East)",
    "us-east-1": "N. Virginia",
    "us-east-2": "Ohio",
    "us-west-1": "N. California",
    "us-west-2": "Oregon",
    "eu-west-1": "Ireland",
    "eu-west-2": "London",
    "eu-west-3": "Paris",
    "eu-central-1": "Frankfurt",
    "eu-central-2": "Zurich",
    "eu-north-1": "Stockholm",
    "eu-south-1": "Milan",
    "eu-south-2": "Spain",
    "ca-central-1": "Canada Central",
    "ca-west-1": "Canada West",
    "sa-east-1": "Sao Paulo",
    "mx-central-1": "Mexico",
    "af-south-1": "Cape Town",
    "me-south-1": "Bahrain",
    "me-central-1": "UAE",
    "il-central-1": "Israel",
}

REGION_FLAGS = {
    "ap-southeast-5": "🇲🇾",
    "ap-southeast-1": "🇸🇬",
    "ap-southeast-2": "🇦🇺",
    "ap-southeast-3": "🇮🇩",
    "ap-southeast-6": "🇹🇭",
    "ap-southeast-7": "🇹🇼",
    "ap-northeast-1": "🇯🇵",
    "ap-northeast-2": "🇰🇷",
    "ap-northeast-3": "🇯🇵",
    "ap-south-1": "🇮🇳",
    "ap-south-2": "🇮🇳",
    "ap-east-1": "🇭🇰",
    "ap-east-2": "🇹🇼",
    "us-east-1": "🇺🇸",
    "us-east-2": "🇺🇸",
    "us-west-1": "🇺🇸",
    "us-west-2": "🇺🇸",
    "eu-west-1": "🇮🇪",
    "eu-west-2": "🇬🇧",
    "eu-west-3": "🇫🇷",
    "eu-central-1": "🇩🇪",
    "eu-central-2": "🇨🇭",
    "eu-north-1": "🇸🇪",
    "eu-south-1": "🇮🇹",
    "eu-south-2": "🇪🇸",
    "ca-central-1": "🇨🇦",
    "ca-west-1": "🇨🇦",
    "sa-east-1": "🇧🇷",
    "mx-central-1": "🇲🇽",
    "af-south-1": "🇿🇦",
    "me-south-1": "🇧🇭",
    "me-central-1": "🇦🇪",
    "il-central-1": "🇮🇱",
}

PREFERRED_ORDER = [
    "ap-southeast-5",
    "ap-southeast-1",
    "ap-southeast-3",
    "ap-southeast-2",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-south-1",
    "ap-east-1",
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-central-1",
    "eu-north-1",
    "ca-central-1",
    "sa-east-1",
]

REGIONS_PER_PAGE = 8

SG_NAME = "kiyoshibot-vps"
SG_DESCRIPTION = "KiyoshiBot VPS SSH access"

TIERS = [
    {
        "id": "vps_1gb",
        "label": "1 GB",
        "instance_type": "t3.micro",
        "vcpu": 2,
        "ram_gb": 1,
        "disk_gb": 30,
        "bandwidth": "1 TB",
    },
    {
        "id": "vps_2gb",
        "label": "2 GB",
        "instance_type": "t3.small",
        "vcpu": 2,
        "ram_gb": 2,
        "disk_gb": 60,
        "bandwidth": "2 TB",
    },
    {
        "id": "vps_4gb",
        "label": "4 GB",
        "instance_type": "t3.medium",
        "vcpu": 2,
        "ram_gb": 4,
        "disk_gb": 100,
        "bandwidth": "3 TB",
    },
    {
        "id": "vps_8gb",
        "label": "8 GB",
        "instance_type": "t3.large",
        "vcpu": 2,
        "ram_gb": 8,
        "disk_gb": 160,
        "bandwidth": "5 TB",
    },
]

OS_IMAGES = [
    {
        "id": "ubuntu-24.04",
        "label": "Ubuntu 24.04 LTS",
        "name": "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*",
        "owner": "099720109477",
        "family": "ubuntu",
    },
    {
        "id": "ubuntu-22.04",
        "label": "Ubuntu 22.04 LTS",
        "name": "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*",
        "owner": "099720109477",
        "family": "ubuntu",
    },
    {
        "id": "debian-12",
        "label": "Debian 12",
        "name": "debian-12-amd64-*",
        "owner": "136693071363",
        "family": "debian",
    },
]

DURATIONS = [
    {"days": 30, "key": "30d", "label": "30 Days"},
    {"days": 14, "key": "14d", "label": "14 Days"},
    {"days": 7, "key": "7d", "label": "7 Days"},
]

WARRANTY_DAYS = 7
MAX_DAYS = 30

USER_PREFIX = "vps"
OWNER_PREFIX = "vpsown"

DEADLINE_SEC = 300
POLL_INTERVAL_SEC = 15

FEE_BPS = 200

DISK_DEVICE = "/dev/sda1"


def get_tier(tier_id: str) -> dict | None:
    for t in TIERS:
        if t["id"] == tier_id:
            return t
    return None


def get_os(os_id: str) -> dict | None:
    for o in OS_IMAGES:
        if o["id"] == os_id:
            return o
    return None


def region_label(region: str) -> str:
    return REGION_LABELS.get(region, region)


def region_flag(region: str) -> str:
    return REGION_FLAGS.get(region, "🌐")


def region_display(region: str) -> str:
    flag = region_flag(region)
    label = region_label(region)
    if label == region:
        return f"{flag} {region}"
    return f"{flag} {label}"


def sort_regions(regions: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for r in PREFERRED_ORDER:
        if r in regions and r not in seen:
            ordered.append(r)
            seen.add(r)
    for r in sorted(regions):
        if r not in seen:
            ordered.append(r)
            seen.add(r)
    return ordered
