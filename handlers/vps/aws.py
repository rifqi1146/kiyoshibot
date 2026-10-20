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


def _aws_env(region: str | None = None, account: str | None = None) -> dict:
    env = dict(os.environ)
    env.setdefault("AWS_DEFAULT_REGION", region or DEFAULT_REGION)
    for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(k, None)
    from . import accounts as accounts_store

    creds = accounts_store.account_creds(account)
    env.update(creds)
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


def _run_aws_cli(
    args: list[str],
    region: str | None = None,
    timeout: int = 60,
    account: str | None = None,
) -> dict | list | str:
    cmd = ["aws", "--region", region or DEFAULT_REGION, "--output", "json"] + args
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_aws_env(region, account),
    )
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


_REGION_SOURCE_CACHE: dict = {"regions": [], "ts": 0.0, "owners": {}}
REGION_SOURCE_TTL = 3600


def _list_regions_for_account(account: str | None) -> list[str]:
    res = _run_aws_cli([
        "ec2", "describe-regions",
        "--filters", "Name=opt-in-status,Values=opt-in-not-required,opted-in",
        "--query", "Regions[].RegionName",
    ], region=DEFAULT_REGION, timeout=45, account=account)
    if isinstance(res, list):
        return [str(r) for r in res]
    return []


def list_regions() -> list[str]:
    return list(_region_sources().get("regions", []))


def _region_sources(force: bool = False) -> dict:
    from . import accounts as accounts_store

    now = time.time()
    if not force and _REGION_SOURCE_CACHE["regions"] and (now - _REGION_SOURCE_CACHE["ts"]) < REGION_SOURCE_TTL:
        return _REGION_SOURCE_CACHE

    entries = accounts_store.all_accounts()

    try:
        base_regions = sorted(_list_regions_for_account(None))
        base_ok = True
    except Exception as e:
        log.warning("Default profile region listing failed | err=%r", e)
        base_regions = []
        base_ok = False
    accounts_store.set_base_account_regions(base_regions)

    owners: dict[str, list[str]] = {}
    account_rows = []
    if base_regions:
        account_rows.append(
            {
                "name": accounts_store.DEFAULT_ACCOUNT_NAME,
                "regions": base_regions,
                "ok": base_ok,
                "source": "aws",
            }
        )
        for r in base_regions:
            owners.setdefault(r, []).append(accounts_store.DEFAULT_ACCOUNT_NAME)

    for acc in entries:
        name = acc["name"]
        configured = list(accounts_store.regions_for_account(name))
        if configured:
            regions = sorted(configured)
            ok = True
            source = "configured"
        else:
            try:
                regions = sorted(_list_regions_for_account(name))
                ok = True
                source = "aws"
            except Exception as e:
                log.warning("Region listing failed | account=%s err=%r", name, e)
                regions = []
                ok = False
                source = "none"
        account_rows.append({"name": name, "regions": regions, "ok": ok, "source": source})
        for r in regions:
            owners.setdefault(r, []).append(name)

    merged = sorted(owners.keys())
    ordered = {r: accounts_store.resolve_chain(r, merged) for r in merged}

    out = {
        "regions": merged,
        "owners": ordered,
        "ts": now,
        "accounts": account_rows,
    }
    _REGION_SOURCE_CACHE.update(out)
    return out


def invalidate_region_sources() -> None:
    _REGION_SOURCE_CACHE.update({"regions": [], "ts": 0.0, "owners": {}})


def region_chain(region: str) -> list[str]:
    from . import accounts as accounts_store

    owners = _region_sources().get("owners") or {}
    r = (region or "").strip().lower()
    if r in owners:
        return list(owners[r])
    return accounts_store.resolve_chain(r, list(_region_sources().get("regions") or []))


def account_for_region(region: str) -> str | None:
    chain = region_chain(region)
    return chain[0] if chain else None


def _vpc_for_region(region: str, account: str | None = None) -> str:
    res = _run_aws_cli([
        "ec2", "describe-vpcs",
        "--filters", "Name=isDefault,Values=true",
        "--query", "Vpcs[0].VpcId",
    ], region=region, timeout=45, account=account)
    if isinstance(res, str) and res:
        return res
    if isinstance(res, list) and res:
        return str(res[0])
    raise RuntimeError(f"No default VPC found in {region}")


def _subnets_for_region(region: str, vpc_id: str, account: str | None = None) -> list[str]:
    res = _run_aws_cli([
        "ec2", "describe-subnets",
        "--filters", f"Name=vpc-id,Values={vpc_id}",
        "--query", "Subnets[?MapPublicIpOnLaunch==`true`].SubnetId",
    ], region=region, timeout=45, account=account)
    if isinstance(res, list) and res:
        return [str(s) for s in res]
    raise RuntimeError(f"No public subnets found in {region}")


def _find_ssh_sg(region: str, vpc_id: str, account: str | None = None) -> str | None:
    try:
        res = _run_aws_cli([
            "ec2", "describe-security-groups",
            "--filters", f"Name=vpc-id,Values={vpc_id}",
            "--query", "SecurityGroups[].GroupId",
        ], region=region, timeout=45, account=account)
        ids = [str(s) for s in res] if isinstance(res, list) else []
        if not ids:
            return None
        detail = _run_aws_cli([
            "ec2", "describe-security-groups",
            "--group-ids"] + ids,
            region=region, timeout=45, account=account,
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


def create_security_group(region: str, vpc_id: str, account: str | None = None) -> str:
    res = _run_aws_cli([
        "ec2", "create-security-group",
        "--group-name", SG_NAME,
        "--description", SG_DESCRIPTION,
        "--vpc-id", vpc_id,
    ], region=region, timeout=45, account=account)
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
    ], region=region, timeout=45, account=account)
    return sg_id


def resolve_network(region: str, account: str | None = None) -> tuple[str, list[str], str]:
    vpc_id = _vpc_for_region(region, account)
    subnets = _subnets_for_region(region, vpc_id, account)
    sg_id = _find_ssh_sg(region, vpc_id, account)
    if not sg_id:
        sg_id = create_security_group(region, vpc_id, account)
    return vpc_id, subnets, sg_id


def resolve_ami(region: str, os_id: str, account: str | None = None) -> str:
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
    ], region=region, timeout=60, account=account)
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
    account: str | None = None,
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
            ], region=region, timeout=90, account=account)
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
    if "not eligible for free tier" in low:
        return (
            f"Your AWS account in {region} is on the Free plan, which only allows "
            "Free Tier eligible instance types. Upgrade the AWS account plan to "
            "Launch a larger plan, or pick a smaller plan."
        )
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


def _describe_instance_sync(region: str, instance_id: str, account: str | None = None) -> dict | None:
    try:
        res = _run_aws_cli([
            "ec2", "describe-instances",
            "--instance-ids", instance_id,
        ], region=region, timeout=30, account=account)
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


def _terminate_instance_sync(region: str, instance_id: str, account: str | None = None) -> bool:
    try:
        res = _run_aws_cli([
            "ec2", "terminate-instances",
            "--instance-ids", instance_id,
        ], region=region, timeout=30, account=account)
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
    account: str | None = None,
) -> tuple[dict, str]:
    from database import vps_db

    password = generate_root_password(16)
    user_data = build_cloud_config(password)

    tier_for_type = None
    for t in TIERS_SNAPSHOT():
        if t["instance_type"] == instance_type:
            tier_for_type = t
            break
    prefer = list((tier_for_type or {}).get("fallback_types") or [])

    chain = [account] if account else region_chain(region)
    if not chain:
        raise RuntimeError(
            f"No AWS account is available for {region}. "
            "Please choose a different region."
        )

    last_err: Exception | None = None
    tried: list[str] = []
    for acc in chain:
        attempt_type = instance_type
        own_sg: str | None = None
        for _round in range(2):
            try:
                vpc_id, subnets, sg_id = await asyncio.to_thread(resolve_network, region, acc)
                vps_db.save_region_cache(
                    region, vpc_id=vpc_id, subnet_ids=subnets, sg_id=sg_id, available=True
                )
                if not own_sg:
                    own_sg = await asyncio.to_thread(
                        create_vps_security_group, region, vpc_id, tag_name, acc
                    )
                image_id = await asyncio.to_thread(resolve_ami, region, os_id, acc)
                res = await asyncio.to_thread(
                    _launch_instance_sync,
                    region=region,
                    subnets=subnets,
                    account=acc,
                    sg_id=own_sg,
                    image_id=image_id,
                    instance_type=attempt_type,
                    disk_gb=disk_gb,
                    user_data=user_data,
                    tag_name=tag_name,
                )
                res["account"] = acc
                res["sg_id"] = own_sg
                res["requested_instance_type"] = instance_type
                res["instance_type"] = attempt_type
                if attempt_type != instance_type:
                    res["downgraded"] = True
                    log.warning(
                        "Launched on free-tier eligible type | region=%s account=%s "
                        "requested=%s used=%s",
                        region, acc, instance_type, attempt_type,
                    )
                return res, password
            except Exception as e:
                low = str(e).lower()
                if "not eligible for free tier" in low and _round == 0:
                    allowed = await asyncio.to_thread(_free_tier_eligible, region, acc)
                    fallback = _free_tier_fallback(attempt_type, allowed or set(), prefer)
                    if fallback and fallback != attempt_type:
                        log.warning(
                            "Free plan detected, retrying with eligible type | region=%s account=%s "
                            "requested=%s fallback=%s",
                            region, acc, attempt_type, fallback,
                        )
                        attempt_type = fallback
                        continue
                tried.append(acc)
                last_err = e
                await asyncio.to_thread(delete_vps_security_group, own_sg, region, acc)
                log.warning(
                    "Launch attempt failed | region=%s account=%s type=%s err=%r",
                    region, acc, attempt_type, e,
                )
                break

    detail = str(last_err)
    if len(tried) > 1:
        detail = f"Tried accounts {', '.join(tried)}. {detail}"
    raise RuntimeError(_humanize_launch_error(region, detail))


async def wait_for_public_ip(
    instance_id: str,
    region: str = DEFAULT_REGION,
    max_attempts: int = 20,
    delay: float = 3.0,
    account: str | None = None,
) -> str | None:
    for _ in range(max_attempts):
        data = await asyncio.to_thread(_describe_instance_sync, region, instance_id, account)
        if data and data.get("public_ip"):
            return data["public_ip"]
        await asyncio.sleep(delay)
    return None


async def terminate_vps(
    instance_id: str,
    region: str = DEFAULT_REGION,
    account: str | None = None,
) -> bool:
    return await asyncio.to_thread(_terminate_instance_sync, region, instance_id, account)


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


async def get_instance_info(
    instance_id: str,
    region: str = DEFAULT_REGION,
    account: str | None = None,
) -> dict | None:
    return await asyncio.to_thread(_describe_instance_sync, region, instance_id, account)


ACCOUNT_SCAN_CONCURRENCY = 10
ACCOUNT_SCAN_TIMEOUT = 25
_LIVE_STATE_FILTER = "Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped"


def _scan_region_sync(region: str, account: str | None = None) -> dict:
    try:
        res = _run_aws_cli([
            "ec2", "describe-instances",
            "--filters", _LIVE_STATE_FILTER,
            "--query",
            "Reservations[].Instances[].[InstanceId,InstanceType,State.Name,PublicIpAddress,"
            "Placement.AvailabilityZone,Tags[?Key==`Name`].Value|[0],LaunchTime,ImageId,"
            "Tags[?Key==`ManagedBy`].Value|[0]]",
        ], region=region, timeout=ACCOUNT_SCAN_TIMEOUT, account=account)
    except Exception as e:
        msg = str(e)
        denied = "unauthorizedoperation" in msg.lower() or "not authorized" in msg.lower()
        if not denied:
            log.warning("Account scan failed | region=%s account=%s err=%r", region, account, e)
        return {"rows": [], "region": region, "account": account, "denied": denied, "error": msg}

    raw = res if isinstance(res, list) else []
    out = []
    for row in raw:
        if not isinstance(row, list) or not row:
            continue
        out.append({
            "region": region,
            "account": account,
            "instance_id": row[0] if len(row) > 0 else None,
            "instance_type": row[1] if len(row) > 1 else None,
            "state": row[2] if len(row) > 2 else None,
            "public_ip": row[3] if len(row) > 3 else None,
            "availability_zone": row[4] if len(row) > 4 else None,
            "name": row[5] if len(row) > 5 else None,
            "launch_time": row[6] if len(row) > 6 else None,
            "image_id": row[7] if len(row) > 7 else None,
            "managed_by": row[8] if len(row) > 8 else None,
        })
    return {"rows": out, "region": region, "account": account, "denied": False, "error": None}


async def scan_account_instances(regions: list[str] | None = None) -> dict:
    started = time.time()
    from . import accounts as accounts_store

    sources = await asyncio.to_thread(_region_sources)
    per_account = {
        row["name"]: list(row.get("regions") or [])
        for row in sources.get("accounts", [])
        if row.get("regions")
    }

    if regions:
        wanted = {r.strip().lower() for r in regions if r}
        per_account = {n: [r for r in rs if r in wanted] for n, rs in per_account.items()}

    plan = [(r, acc) for acc, rs in per_account.items() for r in rs]

    active_accounts = [acc for acc in per_account if acc]

    if not plan:
        try:
            regions = await asyncio.to_thread(list_regions)
        except Exception as e:
            log.error("Account scan could not list regions | err=%r", e)
            return {
                "instances": [],
                "scanned": 0,
                "failed": [],
                "denied": [],
                "accounts": {},
                "elapsed": 0.0,
            }
        plan = [(r, None) for r in regions]

    def _cred(acc: str | None) -> str | None:
        return None if acc == accounts_store.DEFAULT_ACCOUNT_NAME else acc

    sem = asyncio.Semaphore(ACCOUNT_SCAN_CONCURRENCY)

    async def _one(region: str, account: str | None):
        async with sem:
            return await asyncio.to_thread(_scan_region_sync, region, _cred(account))

    results = await asyncio.gather(*[_one(r, a) for r, a in plan])

    account_by_region: dict[str, str] = {}
    for acc, rs in per_account.items():
        for r in rs:
            account_by_region.setdefault(r, acc)

    instances: list[dict] = []
    seen_pairs: set[tuple] = set()
    failed: list[str] = []
    denied: list[str] = []
    per_account_hits: dict[str, int] = {a: 0 for a in active_accounts}

    for res in results:
        region = res["region"]
        raw_account = res["account"]
        account = raw_account or account_by_region.get(region) or accounts_store.DEFAULT_ACCOUNT_NAME
        label = f"{account}:{region}"
        if res["denied"]:
            denied.append(label)
        for row in res["rows"]:
            row["account"] = account
            key = (row.get("instance_id"), account)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            instances.append(row)
            per_account_hits[account] = per_account_hits.get(account, 0) + 1
        if not res["rows"] and not res["denied"]:
            failed.append(label)

    return {
        "instances": instances,
        "scanned": len(plan),
        "failed": failed,
        "denied": denied,
        "accounts": per_account_hits,
        "elapsed": round(time.time() - started, 2),
    }


def _run_with_creds(args: list[str], region: str, creds: dict, timeout: int = 45) -> dict | list | str:
    env = dict(os.environ)
    env.setdefault("AWS_DEFAULT_REGION", region)
    for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(k, None)
    env.update(creds)
    cmd = ["aws", "--region", region, "--output", "json"] + args
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if res.returncode != 0:
        err = (res.stderr or res.stdout).strip()
        raise RuntimeError(err)
    out = res.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except Exception:
        return out


STS_VERIFY_REGION = "us-east-1"


async def verify_account_credentials(
    access_key: str,
    secret_key: str,
    region: str | None = None,
    session_token: str = "",
) -> tuple[bool, str]:
    creds = {"AWS_ACCESS_KEY_ID": access_key, "AWS_SECRET_ACCESS_KEY": secret_key}
    if session_token:
        creds["AWS_SESSION_TOKEN"] = session_token

    def _check(where: str) -> tuple[bool, str]:
        try:
            res = _run_with_creds(
                ["sts", "get-caller-identity", "--query", "[Account,Arn]", "--output", "text"],
                where,
                creds,
                timeout=30,
            )
        except Exception as e:
            return False, str(e)[:300]
        if isinstance(res, str) and res.strip():
            return True, res.strip().replace("\t", " / ")
        return False, "Empty identity response"

    def _run() -> tuple[bool, str]:
        order = [STS_VERIFY_REGION]
        if region and region not in order:
            order.append(region)
        for where in order:
            ok, detail = _check(where)
            if ok:
                return ok, detail
            low = (detail or "").lower()
            if any(x in low for x in ("accessdenied", "not authorized", "unauthorizedoperation")):
                return ok, detail
        return False, detail

    return await asyncio.to_thread(_run)


async def probe_account(account: str, regions: list[str]) -> list[dict]:
    sem = asyncio.Semaphore(ACCOUNT_SCAN_CONCURRENCY)

    async def _one(region: str):
        async with sem:
            return await asyncio.to_thread(_scan_region_sync, region, account)

    results = await asyncio.gather(*[_one(r) for r in regions])
    out = []
    for res in results:
        out.append(
            {
                "region": res["region"],
                "ok": not res["denied"] and res["error"] is None,
                "denied": bool(res["denied"]),
                "count": len(res["rows"]),
                "error": res["error"],
            }
        )
    return out


async def discover_account_regions(
    access_key: str,
    secret_key: str,
    candidates: list[str] | None = None,
    session_token: str = "",
) -> list[str]:
    creds = {"AWS_ACCESS_KEY_ID": access_key, "AWS_SECRET_ACCESS_KEY": secret_key}
    if session_token:
        creds["AWS_SESSION_TOKEN"] = session_token

    def _regions() -> list[str]:
        try:
            res = _run_with_creds(
                [
                    "ec2", "describe-regions",
                    "--filters", "Name=opt-in-status,Values=opt-in-not-required,opted-in",
                    "--query", "Regions[].RegionName",
                ],
                STS_VERIFY_REGION,
                creds,
            )
        except Exception:
            return []
        return [str(r) for r in res] if isinstance(res, list) else []

    listed = await asyncio.to_thread(_regions)
    probe_list = sorted(set(listed or candidates or []))
    if not probe_list:
        return []

    sem = asyncio.Semaphore(ACCOUNT_SCAN_CONCURRENCY)

    def _can(region: str) -> bool:
        try:
            _run_with_creds(
                ["ec2", "describe-vpcs", "--query", "Vpcs[0].VpcId"],
                region,
                creds,
                timeout=20,
            )
            return True
        except Exception:
            return False

    async def _one(region: str) -> tuple[str, bool]:
        async with sem:
            return region, await asyncio.to_thread(_can, region)

    results = await asyncio.gather(*[_one(r) for r in probe_list])
    ok = sorted(r for r, allowed in results if allowed)
    if ok:
        return ok
    return sorted(listed)


FREE_TIER_ELIGIBLE_CACHE: dict[tuple[str, str], set[str]] = {}


def _free_tier_eligible(region: str, account: str | None) -> set[str] | None:
    key = (region, str(account or ""))
    if key in FREE_TIER_ELIGIBLE_CACHE:
        return FREE_TIER_ELIGIBLE_CACHE[key]
    try:
        res = _run_aws_cli([
            "ec2", "describe-instance-types",
            "--filters", "Name=free-tier-eligible,Values=true",
            "--query", "InstanceTypes[].InstanceType",
        ], region=region, timeout=30, account=account)
    except Exception as e:
        log.warning("Free tier probe failed | region=%s account=%s err=%r", region, account, e)
        return None
    types = {str(t) for t in res} if isinstance(res, list) else set()
    FREE_TIER_ELIGIBLE_CACHE[key] = types
    return types


def _free_tier_fallback(instance_type: str, allowed: set[str], prefer: list[str] | None = None) -> str | None:
    for candidate in (prefer or []) + [instance_type]:
        if candidate in allowed:
            return candidate
    return None


OS_AVAILABILITY_CACHE: dict[tuple[str, str], bool] = {}


def os_available(region: str, os_id: str, account: str | None = None) -> bool:
    key = (region, os_id)
    if key in OS_AVAILABILITY_CACHE:
        return OS_AVAILABILITY_CACHE[key]
    try:
        resolve_ami(region, os_id, account)
        OS_AVAILABILITY_CACHE[key] = True
    except Exception:
        OS_AVAILABILITY_CACHE[key] = False
    return OS_AVAILABILITY_CACHE[key]


async def available_os_images(region: str, account: str | None = None) -> list[dict]:
    from .constants import OS_IMAGES

    sem = asyncio.Semaphore(8)

    async def _one(o: dict):
        async with sem:
            ok = await asyncio.to_thread(os_available, region, o["id"], account)
            return o, ok

    results = await asyncio.gather(*[_one(o) for o in OS_IMAGES])
    return [o for o, ok in results if ok]



def TIERS_SNAPSHOT() -> list[dict]:
    from .constants import TIERS

    return TIERS


def _vps_group_name(vps_id: str) -> str:
    return f"kiyoshibot-vps-{str(vps_id).lower()}"[:255]


def _instance_vpc_sync(
    instance_id: str,
    region: str,
    account: str | None = None,
    describe_account: str | None = None,
) -> str | None:
    env = _aws_env(region, describe_account)
    cmd = [
        "aws", "ec2", "describe-instances",
        "--region", region,
        "--instance-ids", instance_id,
        "--query", "Reservations[].Instances[].VpcId",
        "--output", "text",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
    if p.returncode != 0:
        return None
    vpc = (p.stdout or "").strip().split()
    return vpc[0] if vpc else None


def attach_security_group_sync(
    instance_id: str,
    sg_id: str,
    region: str,
    account: str | None = None,
) -> bool:
    env = _aws_env(region, account)

    def describe():
        return subprocess.run(
            [
                "aws", "ec2", "describe-instances",
                "--region", region,
                "--instance-ids", instance_id,
                "--query", "Reservations[].Instances[].[NetworkInterfaces[].NetworkInterfaceId,VpcId][]",
                "--output", "json",
            ],
            capture_output=True, text=True, env=env, timeout=60,
        )

    d = describe()
    if d.returncode != 0:
        log.warning("attach_security_group describe failed | instance=%s err=%s",
                    instance_id, (d.stderr or "").strip()[:200])
        return False
    try:
        payload = json.loads(d.stdout or "[]")
    except Exception:
        payload = []
    enis = []
    for item in payload:
        if isinstance(item, list):
            for sub in item:
                if isinstance(sub, list):
                    enis.extend(x for x in sub if isinstance(x, str) and x.startswith("eni-"))
                elif isinstance(sub, str) and sub.startswith("eni-"):
                    enis.append(sub)
        elif isinstance(item, str) and item.startswith("eni-"):
            enis.append(item)
    if not enis:
        r = subprocess.run(
            [
                "aws", "ec2", "describe-instances",
                "--region", region,
                "--instance-ids", instance_id,
                "--query", "Reservations[].Instances[].NetworkInterfaces[].NetworkInterfaceId",
                "--output", "text",
            ],
            capture_output=True, text=True, env=env, timeout=60,
        )
        enis = (r.stdout or "").split()
    if not enis:
        log.warning("attach_security_group no ENI | instance=%s", instance_id)
        return False

    ok = True
    for eni in enis:
        r = subprocess.run(
            [
                "aws", "ec2", "modify-network-interface-attribute",
                "--region", region,
                "--network-interface-id", eni,
                "--groups", sg_id,
            ],
            capture_output=True, text=True, env=env, timeout=60,
        )
        if r.returncode != 0:
            log.warning("attach_security_group failed | instance=%s eni=%s err=%s",
                        instance_id, eni, (r.stderr or "").strip()[:200])
            ok = False
    return ok


def ensure_vps_security_group(
    region: str,
    vps_id: str,
    instance_id: str | None = None,
    account: str | None = None,
) -> str:
    if not instance_id:
        raise RuntimeError(f"Instance id is required to attach a firewall for {vps_id}")

    probe_account = None if account in (None, "default") else account
    rows = _run_aws_cli([
        "ec2", "describe-instances",
        "--instance-ids", instance_id,
        "--query", "Reservations[].Instances[].VpcId",
    ], region=region, timeout=60, account=probe_account)
    vpc_list = rows if isinstance(rows, list) else ([rows] if rows else [])
    vpc_id = next((v for v in vpc_list if isinstance(v, str) and v.startswith("vpc-")), None)
    if not vpc_id:
        raise RuntimeError(f"Cannot resolve the VPC for instance {instance_id} in {region}")

    rows = _run_aws_cli([
        "ec2", "describe-security-groups",
        "--filters",
        f"Name=group-name,Values={_vps_group_name(vps_id)}",
        f"Name=vpc-id,Values={vpc_id}",
        "--query", "SecurityGroups[].GroupId",
    ], region=region, timeout=45, account=probe_account)
    ids = rows if isinstance(rows, list) else ([rows] if rows else [])
    for gid in ids:
        if not gid:
            continue
        if attach_security_group_sync(instance_id, gid, region, account):
            log.info("ensure_sg reused existing | vps=%s sg=%s", vps_id, gid)
            return gid
        log.warning("ensure_sg attach existing failed | vps=%s sg=%s", vps_id, gid)

    sg = create_vps_security_group(region, vpc_id, vps_id, account=account)
    if not attach_security_group_sync(instance_id, sg, region, account):
        raise RuntimeError(
            f"Firewall {sg} was created but could not be attached to {instance_id}"
        )
    return sg


async def create_vps_security_group_async(
    region: str,
    vpc_id: str,
    vps_id: str,
    account: str | None = None,
) -> str:
    return await asyncio.to_thread(create_vps_security_group, region, vpc_id, vps_id, account)


async def attach_security_group_async(
    instance_id: str,
    sg_id: str,
    region: str,
    account: str | None = None,
) -> bool:
    return await asyncio.to_thread(attach_security_group_sync, instance_id, sg_id, region, account)


async def add_firewall_rule(
    sg_id: str,
    region: str,
    direction: str,
    proto: str,
    from_port: int,
    to_port: int,
    cidr: str,
    account: str | None = None,
) -> str | None:
    return await asyncio.to_thread(
        add_firewall_rule_sync, sg_id, region, direction, proto, from_port, to_port, cidr, account
    )


async def remove_firewall_rule(
    sg_id: str,
    region: str,
    direction: str,
    proto: str,
    from_port: int,
    to_port: int,
    cidr: str,
    account: str | None = None,
) -> bool:
    return await asyncio.to_thread(
        remove_firewall_rule_sync, sg_id, region, direction, proto, from_port, to_port, cidr, account
    )


async def list_firewall_rules(
    sg_id: str,
    region: str,
    account: str | None = None,
) -> dict:
    return await asyncio.to_thread(list_firewall_rules_sync, sg_id, region, account)


async def instance_security_group(instance_id: str, region: str, account: str | None = None):
    return await asyncio.to_thread(instance_security_group_sync, instance_id, region, account)


def create_vps_security_group(
    region: str,
    vpc_id: str,
    vps_id: str,
    account: str | None = None,
) -> str:
    res = _run_aws_cli([
        "ec2", "create-security-group",
        "--group-name", _vps_group_name(vps_id),
        "--description", f"KiyoshiBot VPS firewall for {vps_id}",
        "--vpc-id", vpc_id,
        "--tag-specifications",
        json.dumps([
            {
                "ResourceType": "security-group",
                "Tags": [
                    {"Key": "ManagedBy", "Value": "kiyoshibot"},
                    {"Key": "Name", "Value": _vps_group_name(vps_id)},
                    {"Key": "VpsId", "Value": str(vps_id)},
                ],
            }
        ]),
    ], region=region, timeout=45, account=account)
    sg_id = res.get("GroupId") if isinstance(res, dict) else None
    if not sg_id:
        raise RuntimeError(f"Failed to create firewall for {vps_id} in {region}")
    _run_aws_cli([
        "ec2", "authorize-security-group-ingress",
        "--group-id", sg_id,
        "--ip-permissions",
        json.dumps([
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}],
            }
        ]),
    ], region=region, timeout=45, account=account)
    return sg_id


def delete_vps_security_group(
    sg_id: str,
    region: str,
    account: str | None = None,
    attempts: int = 6,
    delay: float = 5.0,
) -> bool:
    if not sg_id:
        return False
    last = None
    for i in range(max(1, attempts)):
        try:
            _run_aws_cli([
                "ec2", "delete-security-group",
                "--group-id", sg_id,
            ], region=region, timeout=45, account=account)
            return True
        except Exception as e:
            last = e
            low = str(e).lower()
            if "invalidgroup.notfound" in low or "does not exist" in low:
                return True
            if "dependencyviolation" in low and i < attempts - 1:
                time.sleep(delay)
                continue
            break
    log.warning("Delete SG failed | sg=%s region=%s err=%r", sg_id, region, last)
    return False


def _port_permission(proto: str, from_port: int, to_port: int, cidr: str, description: str) -> dict:
    if proto == "all":
        return {
            "IpProtocol": "-1",
            "IpRanges": [{"CidrIp": cidr, "Description": description}],
        }
    if proto == "icmp":
        return {
            "IpProtocol": "icmp",
            "FromPort": -1,
            "ToPort": -1,
            "IpRanges": [{"CidrIp": cidr, "Description": description}],
        }
    return {
        "IpProtocol": proto,
        "FromPort": int(from_port),
        "ToPort": int(to_port),
        "IpRanges": [{"CidrIp": cidr, "Description": description}],
    }


def instance_security_group_sync(instance_id: str, region: str, account: str | None = None) -> str | None:
    env = _aws_env(region, account)
    cmd = [
        "aws", "ec2", "describe-instances",
        "--region", region,
        "--instance-ids", instance_id,
        "--query", "Reservations[].Instances[].SecurityGroups[].GroupId",
        "--output", "text",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
    except Exception as e:
        log.warning("instance_security_group_sync failed | instance=%s err=%r", instance_id, e)
        return None
    if p.returncode != 0:
        log.warning("instance_security_group_sync error | instance=%s err=%s",
                    instance_id, (p.stderr or "").strip()[:200])
        return None
    ids = (p.stdout or "").split()
    if not ids:
        return None
    for gid in ids:
        try:
            count = _instance_count_for_sg(gid, region, account)
        except Exception:
            count = 999
        if count <= 1:
            return gid
    return ids[0]


def _instance_count_for_sg(sg_id: str, region: str, account: str | None = None) -> int:
    env = _aws_env(region, account)
    cmd = [
        "aws", "ec2", "describe-instances",
        "--region", region,
        "--filters", f"Name=instance.group-id,Values={sg_id}",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
        "--query", "length(Reservations[].Instances[])",
        "--output", "text",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
    if p.returncode != 0:
        return 999
    try:
        return int((p.stdout or "0").strip() or 0)
    except Exception:
        return 999


def add_firewall_rule_sync(
    sg_id: str,
    region: str,
    direction: str,
    proto: str,
    from_port: int,
    to_port: int,
    cidr: str,
    account: str | None = None,
) -> str | None:
    perm = _port_permission(proto, from_port, to_port, cidr, "kiyoshibot user rule")
    verb = "authorize-security-group-ingress" if direction == "in" else "authorize-security-group-egress"
    res = _run_aws_cli([
        "ec2", verb,
        "--group-id", sg_id,
        "--ip-permissions", json.dumps([perm]),
    ], region=region, timeout=45, account=account)
    rules = res.get("SecurityGroupRules", []) if isinstance(res, dict) else []
    if rules:
        return str(rules[0].get("SecurityGroupRuleId") or "") or None
    return None


def remove_firewall_rule_sync(
    sg_id: str,
    region: str,
    direction: str,
    proto: str,
    from_port: int,
    to_port: int,
    cidr: str,
    account: str | None = None,
) -> bool:
    perm = _port_permission(proto, from_port, to_port, cidr, "kiyoshibot user rule")
    verb = "revoke-security-group-ingress" if direction == "in" else "revoke-security-group-egress"
    try:
        _run_aws_cli([
            "ec2", verb,
            "--group-id", sg_id,
            "--ip-permissions", json.dumps([perm]),
        ], region=region, timeout=45, account=account)
        return True
    except Exception as e:
        low = str(e).lower()
        if "invalidpermission" in low or "notfound" in low or "does not exist" in low:
            return True
        log.warning("Revoke firewall rule failed | sg=%s err=%r", sg_id, e)
        return False


def list_firewall_rules_sync(
    sg_id: str,
    region: str,
    account: str | None = None,
) -> dict:
    res = _run_aws_cli([
        "ec2", "describe-security-groups",
        "--group-ids", sg_id,
    ], region=region, timeout=45, account=account)
    groups = res.get("SecurityGroups", []) if isinstance(res, dict) else []
    if not groups:
        return {"in": [], "out": []}

    def _norm(perms) -> list[dict]:
        out = []
        for p in perms or []:
            proto = str(p.get("IpProtocol") or "-1")
            fr = p.get("FromPort")
            to = p.get("ToPort")
            for rng in p.get("IpRanges", []) or []:
                out.append({
                    "proto": proto,
                    "from_port": fr,
                    "to_port": to,
                    "cidr": rng.get("CidrIp"),
                })
            for rng in p.get("Ipv6Ranges", []) or []:
                out.append({
                    "proto": proto,
                    "from_port": fr,
                    "to_port": to,
                    "cidr": rng.get("CidrIpv6"),
                })
        return out

    g = groups[0]
    return {"in": _norm(g.get("IpPermissions")), "out": _norm(g.get("IpPermissionsEgress"))}

