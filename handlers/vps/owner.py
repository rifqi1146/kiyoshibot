import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from database import vps_db
from utils.config import OWNER_ID

from . import aws, render, service
from .constants import DEFAULT_REGION, WARRANTY_DAYS, get_tier
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
    ok = await aws.terminate_vps(inst["instance_id"], region=inst.get("region"))
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


async def vpsgrant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return
    args = context.args or []
    if len(args) < 3:
        return await msg.reply_text(
            "<b>Usage:</b> <code>/vpsgrant &lt;user_id&gt; &lt;tier&gt; &lt;days&gt; [region]</code>\n"
            "Tiers: <code>vps_1gb</code> <code>vps_2gb</code> <code>vps_4gb</code> <code>vps_8gb</code>",
            parse_mode="HTML",
        )
    try:
        target_uid = int(args[0])
        days = int(args[2])
    except Exception:
        return await msg.reply_text("<b>User ID and days must be numbers.</b>", parse_mode="HTML")

    tier_id = args[1].strip()
    t = get_tier(tier_id)
    if not t:
        return await msg.reply_text("<b>Invalid plan.</b>", parse_mode="HTML")

    region = args[3].strip() if len(args) > 3 else DEFAULT_REGION
    status_msg = await msg.reply_text(
        f"<b>Deploying {t['label']} for user {target_uid} in {region}...</b>",
        parse_mode="HTML",
    )

    try:
        vps_id = vps_db.next_vps_id(target_uid)
        res, password = await aws.launch_vps(
            region=region,
            os_id="ubuntu-24.04",
            instance_type=t["instance_type"],
            disk_gb=t["disk_gb"],
            tag_name=vps_id,
        )
        ip = await aws.wait_for_public_ip(res["instance_id"], region=region, max_attempts=25, delay=3.0)
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
        )
        vps_db.log_audit(vps_id, "GRANT", user.id, f"target={target_uid} tier={tier_id} region={region} days={days}")
        service.invalidate_account_scan()
        rich, plain = render.provisioned_content(inst)
        await send_rich(context.bot, chat.id, msg.message_id, rich, plain, render.owner_instance_keyboard(vps_id, "active"))
    except Exception as e:
        log.error("Grant VPS failed | err=%r", e)
        try:
            await status_msg.edit_text(f"<b>Failed to create instance:</b>\n<code>{e}</code>", parse_mode="HTML")
        except Exception:
            pass
