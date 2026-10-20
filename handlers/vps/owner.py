import asyncio
import logging
import re
import time

from telegram import Update
from telegram.ext import ContextTypes

from database import vps_db
from handlers.moderation.helpers import resolve_target_user_id
from utils.config import OWNER_ID

from . import aws, notify, render, service
from .constants import DEFAULT_REGION, MAX_DAYS, WARRANTY_DAYS, get_tier
from .rich import send_rich

log = logging.getLogger(__name__)


def _is_owner(user_id: int) -> bool:
    return int(user_id) in OWNER_ID


async def _get_scan_with_optional_note(msg, force: bool = False) -> tuple[dict, bool]:
    from . import state

    cached, ts = state.get_scan_cache()
    now = time.time()
    needs_fetch = force or not cached or (now - ts) >= service.SCAN_CACHE_TTL
    note = None
    if needs_fetch:
        try:
            note = await msg.reply_text("<b>Scanning AWS regions...</b>", parse_mode="HTML")
        except Exception:
            pass

    scan, is_fresh = await service.get_account_scan(force=force)

    if note:
        try:
            await note.delete()
        except Exception:
            pass
    return scan, is_fresh


async def vpsadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return

    vps_db.init_db()
    scan, _ = await _get_scan_with_optional_note(msg)
    live_instances = scan.get("instances", [])
    summary = {
        "count": len(live_instances),
        "regions": len({i.get("region") for i in live_instances}),
    }
    rich, plain = render.owner_menu_content(live_summary=summary)
    await send_rich(context.bot, chat.id, msg.message_id, rich, plain, render.owner_menu_keyboard())


async def vpslist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return

    vps_db.init_db()
    scan, _ = await _get_scan_with_optional_note(msg)
    db_instances = await asyncio.to_thread(vps_db.list_all_instances)
    merged = render.merge_with_live(db_instances, scan.get("instances", []), show_terminated=False)
    rich, plain = render.owner_list_content(merged, scan)
    await send_rich(context.bot, chat.id, msg.message_id, rich, plain, render.owner_list_keyboard(merged, False))


async def vpsstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return

    vps_db.init_db()
    scan, _ = await _get_scan_with_optional_note(msg)
    live_instances = scan.get("instances", [])
    summary = {
        "count": len(live_instances),
        "regions": len({i.get("region") for i in live_instances}),
    }
    rich, plain = render.owner_menu_content(live_summary=summary)
    await send_rich(context.bot, chat.id, msg.message_id, rich, plain, render.owner_menu_keyboard())


async def vpsinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return
    args = context.args or []
    if not args:
        return await msg.reply_text("<b>Usage:</b> <code>/vpsinfo &lt;vps_id&gt;</code>", parse_mode="HTML")

    inst = await asyncio.to_thread(vps_db.get_instance, args[0].strip())
    if not inst:
        return await msg.reply_text("<b>Instance not found.</b>", parse_mode="HTML")

    rich, plain = render.owner_info_content(inst)
    await send_rich(context.bot, chat.id, msg.message_id, rich, plain, render.owner_instance_keyboard(inst["vps_id"], inst["status"]))


async def vpsterminate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return
    args = context.args or []
    if not args:
        return await msg.reply_text("<b>Usage:</b> <code>/vpsterminate &lt;vps_id&gt;</code>", parse_mode="HTML")

    vps_id = args[0].strip()
    inst = await asyncio.to_thread(vps_db.get_instance, vps_id)
    if not inst:
        return await msg.reply_text("<b>Instance not found.</b>", parse_mode="HTML")

    status = await msg.reply_text("<b>Terminating AWS instance...</b>", parse_mode="HTML")
    ok = await aws.terminate_vps(
        inst["instance_id"], region=inst.get("region"), account=inst.get("account")
    )
    vps_db.terminate_instance_db(vps_id, reason=f"owner_terminate:{user.id}")
    vps_db.log_audit(vps_id, "TERMINATE_OWNER", user.id)
    service.invalidate_account_scan()

    text = (
        f"<b>Instance {vps_id.upper()} terminated.</b>\n\n"
        f"AWS ID: <code>{inst['instance_id']}</code>\n"
        f"Region: <code>{inst.get('region')}</code>\n"
        f"AWS status: {'OK' if ok else 'FAILED'}"
    )
    try:
        await status.edit_text(text, parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, parse_mode="HTML")


async def vpsrenew_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return
    args = context.args or []
    if len(args) < 2:
        return await msg.reply_text(
            "<b>Usage:</b> <code>/vpsrenew &lt;vps_id&gt; &lt;days&gt;</code>",
            parse_mode="HTML",
        )
    vps_id = args[0].strip()
    try:
        days = int(args[1])
    except Exception:
        return await msg.reply_text("<b>Days must be an integer.</b>", parse_mode="HTML")
    if days < 1 or days > 365:
        return await msg.reply_text("<b>Days must be between 1 and 365.</b>", parse_mode="HTML")

    inst = await asyncio.to_thread(vps_db.renew_instance_db, vps_id, days)
    if not inst:
        return await msg.reply_text("<b>Instance not found.</b>", parse_mode="HTML")

    vps_db.log_audit(vps_id, "RENEW", user.id, f"+{days}d")
    rich, plain = render.owner_info_content(inst)
    await send_rich(context.bot, chat.id, msg.message_id, rich, plain, render.owner_instance_keyboard(inst["vps_id"], inst["status"]))


USAGE_TEXT = (
    "<b>Usage:</b>\n"
    "<code>/vpsgrant &lt;user|@username|reply&gt; &lt;tier&gt; &lt;days&gt; [region]</code>\n"
    "Tiers: <code>1g</code> <code>2g</code> <code>4g</code> <code>8g</code>\n"
    "Duration: <code>7d</code> <code>14d</code> <code>30d</code> or a raw day count"
)


def _parse_grant_days(raw: str) -> int | None:
    s = (raw or "").strip().lower()
    if not s:
        return None
    if s.endswith("d"):
        s = s[:-1].strip()
    try:
        days = int(s)
    except Exception:
        return None
    if days <= 0 or days > MAX_DAYS:
        return None
    return days


async def vpsgrant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return
    args = [a for a in (context.args or []) if (a or "").strip()]
    if len(args) < 2:
        return await msg.reply_text(USAGE_TEXT, parse_mode="HTML")

    tier_id = ""
    days = None
    target_token = None
    region = DEFAULT_REGION

    for idx, a in enumerate(args):
        if get_tier(a.strip().lower()) and not tier_id:
            tier_id = a.strip().lower()
            continue
        if days is None and _parse_grant_days(a) is not None:
            days = _parse_grant_days(a)
            continue
        if target_token is None and not get_tier(a.strip().lower()):
            target_token = a.strip()
            continue
        if region == DEFAULT_REGION and re.match(r"^[a-z]{2}-[a-z]+-\d$", a.strip().lower()):
            region = a.strip().lower()
            continue

    if not tier_id:
        return await msg.reply_text(
            f"<b>Invalid plan.</b>\n\n{USAGE_TEXT}",
            parse_mode="HTML",
        )
    if days is None:
        return await msg.reply_text(
            f"<b>Invalid duration.</b> Use <code>7d</code>, <code>14d</code>, <code>30d</code> "
            f"or a day count between 1 and {MAX_DAYS}.\n\n{USAGE_TEXT}",
            parse_mode="HTML",
        )
    if not target_token and not (msg.reply_to_message and msg.reply_to_message.from_user):
        return await msg.reply_text(f"<b>Missing target user.</b>\n\n{USAGE_TEXT}", parse_mode="HTML")

    target_uid = await resolve_target_user_id(update, context, target_token)
    if not target_uid:
        return await msg.reply_text(
            "<b>Could not resolve that user.</b>\n"
            "That username must have been seen by the bot before, or use a numeric ID / reply.",
            parse_mode="HTML",
        )

    t = get_tier(tier_id)
    status_msg = await msg.reply_text(
        f"<b>Deploying {t['label']} for user {target_uid} in {region}...</b>",
        parse_mode="HTML",
    )

    vps_id = ""
    created_instance = None
    created_account = None
    try:
        vps_id = vps_db.next_vps_id(target_uid)
        res, password = await aws.launch_vps(
            region=region,
            os_id="ubuntu-24.04",
            instance_type=t["instance_type"],
            disk_gb=t["disk_gb"],
            tag_name=vps_id,
        )
        created_instance = res["instance_id"]
        created_account = res.get("account")
        account_used = res.get("account")
        ip = await aws.wait_for_public_ip(
            res["instance_id"], region=region, max_attempts=25, delay=3.0, account=account_used
        )
        now = time.time()
        inst = vps_db.save_instance(
            vps_id=vps_id,
            user_id=target_uid,
            instance_id=res["instance_id"],
            instance_type=t["instance_type"],
            region=region,
            tier=tier_id,
            os_image="ubuntu-24.04",
            disk_gb=t["disk_gb"],
            ip_address=ip or "",
            root_password=password,
            price=0,
            days=days,
            created_at=now,
            expires_at=now + (days * 86400),
            warranty_until=now + (WARRANTY_DAYS * 86400),
            account=account_used,
        )
        vps_db.log_audit(
            vps_id,
            "GRANT",
            user.id,
            f"target={target_uid} tier={tier_id} region={region} days={days} account={account_used}",
        )
        service.invalidate_account_scan()
        rich, plain = render.provisioned_content(inst)
        await send_rich(context.bot, chat.id, msg.message_id, rich, plain, render.owner_instance_keyboard(vps_id, "active"))

        try:
            target_name, target_username = await notify.buyer_label(context.bot, target_uid)
            await notify.notify_owner(
                context.bot,
                inst,
                source="grant",
                buyer_name=target_name,
                buyer_username=target_username,
                note=f"Granted by owner {user.id}",
            )
        except Exception as e:
            log.warning("Owner grant notice failed | vps_id=%s err=%r", vps_id, e)
    except Exception as e:
        log.error("Grant VPS failed | err=%r", e)
        cleaned = ""
        if created_instance:
            try:
                await aws.terminate_vps(created_instance, region=region, account=created_account)
                cleaned = f"\n\nOrphan instance <code>{created_instance}</code> was terminated."
                vps_db.log_audit(
                    vps_id or None,
                    "GRANT_ROLLBACK",
                    user.id,
                    f"aws={created_instance} region={region} account={created_account} err={str(e)[:200]}",
                )
            except Exception as term_err:
                log.error(
                    "Grant rollback failed | aws=%s err=%r", created_instance, term_err
                )
                cleaned = (
                    f"\n\n⚠️ Orphan instance <code>{created_instance}</code> in "
                    f"<code>{region}</code> could not be terminated automatically. "
                    "Terminate it from the AWS console."
                )
        service.invalidate_account_scan()
        try:
            await status_msg.edit_text(
                f"<b>Failed to create instance:</b>\n<code>{e}</code>{cleaned}",
                parse_mode="HTML",
            )
        except Exception:
            pass

