import asyncio
import logging

from database import vps_db

from . import aws
from .state import prune

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SEC = 3600

TERMINATED_RETENTION_DAYS = 7


async def sweep_expired_once() -> int:
    vps_db.init_db()
    expired = await asyncio.to_thread(vps_db.list_expired_instances)
    if not expired:
        return 0

    terminated = 0
    for inst in expired:
        try:
            await aws.terminate_vps(
                inst["instance_id"], region=inst.get("region"), account=inst.get("account")
            )
            vps_db.terminate_instance_db(inst["vps_id"], reason="expired")
            vps_db.log_audit(inst["vps_id"], "TERMINATE_EXPIRED", None, "auto sweep")
            terminated += 1
            log.info("VPS expired and terminated | vps_id=%s aws=%s", inst["vps_id"], inst["instance_id"])
        except Exception as e:
            log.error("VPS expire terminate failed | vps_id=%s err=%r", inst["vps_id"], e)
    if terminated:
        from . import service

        service.invalidate_account_scan()
    return terminated


async def purge_old_records() -> dict:
    try:
        res = await asyncio.to_thread(vps_db.purge_old_terminated, TERMINATED_RETENTION_DAYS)
        if res.get("instances") or res.get("invoices"):
            log.info(
                "VPS history purged | instances=%s invoices=%s retention=%sd",
                res.get("instances", 0),
                res.get("invoices", 0),
                TERMINATED_RETENTION_DAYS,
            )
        return res
    except Exception as e:
        log.error("VPS history purge failed | err=%r", e)
        return {"instances": 0, "invoices": 0}


async def expiry_sweep_job(context):
    try:
        count = await sweep_expired_once()
        if count:
            log.info("VPS expiry sweep terminated %s instance(s)", count)
    except Exception as e:
        log.error("VPS expiry sweep failed | err=%r", e)
    try:
        await purge_old_records()
    except Exception:
        pass
    finally:
        prune()


def start_vps_sweeper(app):
    jq = getattr(app, "job_queue", None)
    if not jq:
        log.warning("VPS sweeper skipped: job_queue not available")
        return
    for job in jq.get_jobs_by_name("vps_expiry_sweep"):
        job.schedule_removal()
    jq.run_repeating(
        expiry_sweep_job,
        interval=SWEEP_INTERVAL_SEC,
        first=60,
        name="vps_expiry_sweep",
    )
    log.info("VPS expiry sweeper scheduled every %ss", SWEEP_INTERVAL_SEC)
