import asyncio
import json
import logging
import os
import secrets
import socket
import string
import subprocess
import time

from .constants import DISK_DEVICE, DEFAULT_REGION, SG_DESCRIPTION, SG_NAME, get_os

log = logging.getLogger(__name__)

_AMI_CACHE: dict[tuple[str, str], str] = {}

_LAUNCH_CACHE_LOCK: dict[str, asyncio.Lock] = {}


def _aws_env(region: str | None = None) -> dict:
    env = dict(os.environ)
    env.setdefault("AWS_DEFAULT_REGION", region or DEFAULT_REGION)
    return env


def generate_root_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "@#%="
    return "".join(secrets.choice(chars) for _ in range(length))


def build_cloud_config(password: str) -> str:
    return (
        "#cloud-config\n"
        "disable_root: false\n"
        "ssh_pwauth: true\n\n"
        "chpasswd:\n"
        "  list: |\n"
        f"    root:{password}\n"
        f"    ubuntu:{password}\n"
        "  expire: false\n\n"
        "runcmd:\n"
        "  - sed -i 's/.*PermitRootLogin.*/PermitRootLogin yes/g' /etc/ssh/sshd_config\n"
        "  - sed -i 's/.*PasswordAuthentication.*/PasswordAuthentication yes/g' /etc/ssh/sshd_config\n"
        "  - sed -i 's/.*KbdInteractiveAuthentication.*/KbdInteractiveAuthentication yes/g' /etc/ssh/sshd_config\n"
        "  - rm -f /etc/ssh/sshd_config.d/*\n"
        "  - echo 'PermitRootLogin yes' > /etc/ssh/sshd_config.d/01-root.conf\n"
        "  - echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config.d/01-root.conf\n"
        "  - echo 'KbdInteractiveAuthentication yes' >> /etc/ssh/sshd_config.d/01-root.conf\n"
        "  - systemctl restart ssh || systemctl restart sshd\n"
    )


def _run_aws_cli(args: list[str], region: str | None = None, timeout: int = 60) -> dict | list | str:
    cmd = ["aws", "--region", region or DEFAULT_REGION, "--output", "json"] + args
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_aws_env(region))
    if res.returncode != 0:
        err = (res.stderr or res.stdout).strip()
        raise RuntimeError(f"AWS CLI failed: {err}")
    out = res.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except Exception:
        return out


def list_regions() -> list[str]:
    res = _run_aws_cli([
        "ec2", "describe-regions",
        "--filters", "Name=opt-in-status,Values=opt-in-not-required,opted-in",
        "--query", "Regions[].RegionName",
    ], region=DEFAULT_REGION, timeout=45)
    if isinstance(res, list):
        return [str(r) for r in res]
    return []


def _vpc_for_region(region: str) -> str:
    res = _run_aws_cli([
        "ec2", "describe-vpcs",
        "--filters", "Name=isDefault,Values=true",
        "--query", "Vpcs[0].VpcId",
    ], region=region, timeout=45)
    if isinstance(res, str) and res:
        return res
    if isinstance(res, list) and res:
        return str(res[0])
    raise RuntimeError(f"No default VPC found in {region}")


def _subnets_for_region(region: str, vpc_id: str) -> list[str]:
    res = _run_aws_cli([
        "ec2", "describe-subnets",
        "--filters", f"Name=vpc-id,Values={vpc_id}",
        "--query", "Subnets[?MapPublicIpOnLaunch==`true`].SubnetId",
    ], region=region, timeout=45)
    if isinstance(res, list) and res:
        return [str(s) for s in res]
    raise RuntimeError(f"No public subnets found in {region}")


def _find_ssh_sg(region: str, vpc_id: str) -> str | None:
    try:
        res = _run_aws_cli([
            "ec2", "describe-security-groups",
            "--filters", f"Name=vpc-id,Values={vpc_id}",
            "--query", "SecurityGroups[].GroupId",
        ], region=region, timeout=45)
        ids = [str(s) for s in res] if isinstance(res, list) else []
        if not ids:
            return None
        detail = _run_aws_cli([
            "ec2", "describe-security-groups",
            "--group-ids"] + ids,
            region=region, timeout=45,
        )
        groups = detail.get("SecurityGroups", []) if isinstance(detail, dict) else []
        for g in groups:
            name = (g.get("GroupName") or "").lower()
            for perm in g.get("IpPermissions", []):
                if perm.get("FromPort") != 22 or perm.get("IpProtocol") != "tcp":
                    continue
                for rng in perm.get("IpRanges", []):
                    if rng.get("CidrIp") == "0.0.0.0/0":
                        return g.get("GroupId")
                for rng in perm.get("Ipv6Ranges", []):
                    if rng.get("CidrIpv6") == "::/0":
                        return g.get("GroupId")
            if "kiyoshibot" in name and (g.get("GroupName") or "").startswith(SG_NAME):
                return g.get("GroupId")
        return None
    except Exception as e:
        log.warning("find ssh sg failed | region=%s err=%r", region, e)
        return None


def create_security_group(region: str, vpc_id: str) -> str:
    res = _run_aws_cli([
        "ec2", "create-security-group",
        "--group-name", SG_NAME,
        "--description", SG_DESCRIPTION,
        "--vpc-id", vpc_id,
    ], region=region, timeout=45)
    sg_id = res.get("GroupId") if isinstance(res, dict) else None
    if not sg_id:
        raise RuntimeError(f"Failed to create security group in {region}")
    _run_aws_cli([
        "ec2", "authorize-security-group-ingress",
        "--group-id", sg_id,
        "--ip-permissions",
        json.dumps([
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ]),
    ], region=region, timeout=45)
    return sg_id


def resolve_network(region: str) -> tuple[str, list[str], str]:
    vpc_id = _vpc_for_region(region)
    subnets = _subnets_for_region(region, vpc_id)
    sg_id = _find_ssh_sg(region, vpc_id)
    if not sg_id:
        sg_id = create_security_group(region, vpc_id)
    return vpc_id, subnets, sg_id


def resolve_ami(region: str, os_id: str) -> str:
    key = (region, os_id)
    if key in _AMI_CACHE:
        return _AMI_CACHE[key]
    o = get_os(os_id)
    if not o:
        raise RuntimeError(f"Unknown OS image: {os_id}")
    from database import vps_db

    cached = vps_db.get_region_cache(region)
    if cached and cached.get("amis", {}).get(os_id):
        ami = cached["amis"][os_id]
        _AMI_CACHE[key] = ami
        return ami

    res = _run_aws_cli([
        "ec2", "describe-images",
        "--owners", o["owner"],
        "--filters",
        f"Name=name,Values={o['name']}",
        "Name=state,Values=available",
        "Name=virtualization-type,Values=hvm",
        "--query",
        "Images | sort_by(@, &CreationDate) | [-1].ImageId",
    ], region=region, timeout=60)
    ami = None
    if isinstance(res, str) and res:
        ami = res
    elif isinstance(res, list) and res:
        ami = str(res[0])
    if not ami:
        raise RuntimeError(f"No AMI found in {region} for {os_id}")
    _AMI_CACHE[key] = ami
    vps_db.set_region_ami(region, os_id, ami)
    return ami


def _launch_instance_sync(
    *,
    region: str,
    subnets: list[str],
    sg_id: str,
    image_id: str,
    instance_type: str,
    disk_gb: int,
    user_data: str,
    tag_name: str,
) -> dict:
    last_err = None
    for subnet_id in subnets:
        try:
            bdm = json.dumps([
                {
                    "DeviceName": DISK_DEVICE,
                    "Ebs": {
                        "VolumeSize": int(disk_gb),
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                }
            ])
            tags = json.dumps([
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": tag_name},
                        {"Key": "ManagedBy", "Value": "kiyoshibot"},
                    ],
                }
            ])
            res = _run_aws_cli([
                "ec2", "run-instances",
                "--image-id", image_id,
                "--instance-type", instance_type,
                "--subnet-id", subnet_id,
                "--security-group-ids", sg_id,
                "--associate-public-ip-address",
                "--block-device-mappings", bdm,
                "--user-data", user_data,
                "--tag-specifications", tags,
            ], region=region, timeout=90)
            instances = res.get("Instances") if isinstance(res, dict) else None
            if instances and len(instances) > 0:
                inst = instances[0]
                return {
                    "instance_id": inst.get("InstanceId"),
                    "subnet_id": subnet_id,
                    "state": (inst.get("State") or {}).get("Name", "pending"),
                }
        except Exception as e:
            last_err = e
            log.warning("RunInstances failed in subnet %s (%s), trying next | err=%r", subnet_id, region, e)
            continue
    raise RuntimeError(_humanize_launch_error(region, str(last_err)))


def _humanize_launch_error(region: str, raw: str) -> str:
    low = raw.lower()
    if "vcpulimitexceeded" in low:
        return (
            f"Region {region} has reached its vCPU quota limit. "
            "Please choose a different region or contact support."
        )
    if "insufficientinstancecapacity" in low:
        return (
            f"Region {region} is temporarily out of capacity for this instance type. "
            "Please choose a different region or try again shortly."
        )
    if "instance type" in low and "not supported" in low:
        return (
            f"Region {region} does not support this instance type in any available zone. "
            "Please choose a different region."
        )
    if "unauthorizedoperation" in low or "not authorized" in low:
        return (
            f"This account is not authorized to launch instances in {region}. "
            "Please choose a different region."
        )
    return f"Failed to launch the server in {region}: {raw}"


def _describe_instance_sync(region: str, instance_id: str) -> dict | None:
    try:
        res = _run_aws_cli([
            "ec2", "describe-instances",
            "--instance-ids", instance_id,
        ], region=region, timeout=30)
        reservations = res.get("Reservations", []) if isinstance(res, dict) else []
        if reservations and reservations[0].get("Instances"):
            inst = reservations[0]["Instances"][0]
            return {
                "instance_id": inst.get("InstanceId"),
                "state": (inst.get("State") or {}).get("Name"),
                "public_ip": inst.get("PublicIpAddress"),
                "private_ip": inst.get("PrivateIpAddress"),
                "launch_time": inst.get("LaunchTime"),
                "instance_type": inst.get("InstanceType"),
            }
        return None
    except Exception as e:
        log.warning("DescribeInstance failed | instance_id=%s region=%s err=%r", instance_id, region, e)
        return None


def _terminate_instance_sync(region: str, instance_id: str) -> bool:
    try:
        res = _run_aws_cli([
            "ec2", "terminate-instances",
            "--instance-ids", instance_id,
        ], region=region, timeout=30)
        terminating = res.get("TerminatingInstances", []) if isinstance(res, dict) else []
        return bool(terminating)
    except Exception as e:
        log.error("TerminateInstance failed | instance_id=%s region=%s err=%r", instance_id, region, e)
        return False


def _check_tcp_port_sync(host: str, port: int = 22, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


async def launch_vps(
    *,
    region: str,
    os_id: str,
    instance_type: str,
    disk_gb: int,
    tag_name: str,
) -> tuple[dict, str]:
    from database import vps_db

    password = generate_root_password(16)
    user_data = build_cloud_config(password)

    vpc_id, subnets, sg_id = await asyncio.to_thread(resolve_network, region)
    vps_db.save_region_cache(region, vpc_id=vpc_id, subnet_ids=subnets, sg_id=sg_id, available=True)

    image_id = await asyncio.to_thread(resolve_ami, region, os_id)

    res = await asyncio.to_thread(
        _launch_instance_sync,
        region=region,
        subnets=subnets,
        sg_id=sg_id,
        image_id=image_id,
        instance_type=instance_type,
        disk_gb=disk_gb,
        user_data=user_data,
        tag_name=tag_name,
    )
    return res, password


async def wait_for_public_ip(instance_id: str, region: str = DEFAULT_REGION, max_attempts: int = 20, delay: float = 3.0) -> str | None:
    for _ in range(max_attempts):
        data = await asyncio.to_thread(_describe_instance_sync, region, instance_id)
        if data and data.get("public_ip"):
            return data["public_ip"]
        await asyncio.sleep(delay)
    return None


async def terminate_vps(instance_id: str, region: str = DEFAULT_REGION) -> bool:
    return await asyncio.to_thread(_terminate_instance_sync, region, instance_id)


async def check_vps_health(public_ip: str, port: int = 22) -> bool:
    if not public_ip:
        return False
    return await asyncio.to_thread(_check_tcp_port_sync, public_ip, port)


async def wait_for_ssh(public_ip: str, max_wait_sec: float = 180.0, interval: float = 5.0, port: int = 22) -> bool:
    if not public_ip:
        return False
    deadline = time.time() + float(max_wait_sec)
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        if await check_vps_health(public_ip, port=port):
            log.info("SSH reachable | ip=%s attempt=%s", public_ip, attempt)
            return True
        await asyncio.sleep(interval)
    log.warning("SSH not reachable within %.0fs | ip=%s", max_wait_sec, public_ip)
    return False


async def get_instance_info(instance_id: str, region: str = DEFAULT_REGION) -> dict | None:
    return await asyncio.to_thread(_describe_instance_sync, region, instance_id)


ACCOUNT_SCAN_CONCURRENCY = 10
ACCOUNT_SCAN_TIMEOUT = 25
_LIVE_STATE_FILTER = "Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped"


def _scan_region_sync(region: str) -> list[dict]:
    try:
        res = _run_aws_cli([
            "ec2", "describe-instances",
            "--filters", _LIVE_STATE_FILTER,
            "--query",
            "Reservations[].Instances[].[InstanceId,InstanceType,State.Name,PublicIpAddress,"
            "Placement.AvailabilityZone,Tags[?Key==`Name`].Value|[0],LaunchTime,ImageId]",
        ], region=region, timeout=ACCOUNT_SCAN_TIMEOUT)
    except Exception as e:
        log.warning("Account scan failed | region=%s err=%r", region, e)
        return []

    rows = res if isinstance(res, list) else []
    out = []
    for row in rows:
        if not isinstance(row, list) or not row:
            continue
        out.append({
            "region": region,
            "instance_id": row[0] if len(row) > 0 else None,
            "instance_type": row[1] if len(row) > 1 else None,
            "state": row[2] if len(row) > 2 else None,
            "public_ip": row[3] if len(row) > 3 else None,
            "availability_zone": row[4] if len(row) > 4 else None,
            "name": row[5] if len(row) > 5 else None,
            "launch_time": row[6] if len(row) > 6 else None,
            "image_id": row[7] if len(row) > 7 else None,
        })
    return out


async def scan_account_instances(regions: list[str] | None = None) -> dict:
    started = time.time()
    if not regions:
        try:
            regions = await asyncio.to_thread(list_regions)
        except Exception as e:
            log.error("Account scan could not list regions | err=%r", e)
            return {"instances": [], "scanned": 0, "failed": [], "elapsed": 0.0}

    sem = asyncio.Semaphore(ACCOUNT_SCAN_CONCURRENCY)

    async def _one(r: str):
        async with sem:
            return r, await asyncio.to_thread(_scan_region_sync, r)

    results = await asyncio.gather(*[_one(r) for r in regions])
    instances = []
    failed = []
    for region, rows in results:
        if rows:
            instances.extend(rows)
        elif not rows:
            failed.append(region)

    return {
        "instances": instances,
        "scanned": len(regions),
        "failed": failed,
        "elapsed": round(time.time() - started, 2),
    }
