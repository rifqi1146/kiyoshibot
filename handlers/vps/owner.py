import asyncio
import logging
import re
import time

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from database import vps_db
from handlers.moderation.helpers import resolve_target_user_id
from utils.config import OWNER_ID
from utils.menu_lock import message_id_from_send, register_message_owner

from . import aws, notify, render, service
from .callbacks import NOTICE_LOADING, _answer
from .constants import DEFAULT_REGION, MAX_DAYS, OWNER_PREFIX, WARRANTY_DAYS, get_tier
from .rich import edit_rich, send_rich

log = logging.getLogger(__name__)


def _is_owner(user_id: int) -> bool:
    return int(user_id) in OWNER_ID


async def _chat_action(msg):
    if msg is None:
        return
    try:
        await msg.get_bot().send_chat_action(
            chat_id=msg.chat_id, action=ChatAction.TYPING
        )
    except Exception as e:
        log.debug("Chat action failed | err=%r", e)


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
    await _chat_action(msg)
    ok = await aws.terminate_vps(
        inst["instance_id"], region=inst.get("region"), account=inst.get("account")
    )
    if inst.get("sg_id"):
        await asyncio.to_thread(
            aws.delete_vps_security_group, inst["sg_id"], inst.get("region"), inst.get("account")
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
    "<code>/vpsgrant &lt;user|@username|reply&gt;</code> then pick plan, duration, "
    "region and OS from the buttons.\n"
    "Shortcut: <code>/vpsgrant &lt;user&gt; 1g 30 30d ap-southeast-5</code>"
)

GRANT_TIERS = {"1g", "2g", "4g", "8g"}
REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d$")


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


async def _grant_scope(q):
    if q is None:
        log.warning("Grant scope called without callback query")
        return None
    user = getattr(q, "from_user", None)
    if user is None or int(user.id) not in OWNER_ID:
        log.warning(
            "Grant denied | data=%r user=%s reason=not_owner",
            getattr(q, "data", None), getattr(user, "id", None),
        )
        await _answer(q, "Access denied.", show_alert=True)
        return None
    if q.message is None:
        return None
    chat_id = int(q.message.chat_id)
    message_id = int(q.message.message_id)
    key = (chat_id, message_id)
    if key in render.GRANT_STATE:
        return key

    owned = [
        (cand, store)
        for cand, store in render.GRANT_STATE.items()
        if cand[0] == chat_id and int(store.get("owner_id") or 0) == int(user.id)
    ]
    for cand, store in owned:
        anchor = store.get("anchor")
        if anchor and (int(anchor[0]), int(anchor[1])) == key:
            return cand
    if len(owned) == 1:
        cand, store = owned[0]
        store["anchor"] = key
        render.GRANT_STATE[key] = render.GRANT_STATE.pop(cand)
        return key

    await _answer(q, "This grant screen expired. Send /vpsgrant again.", show_alert=True)
    return None


async def _grant_notice(msg, text: str = ""):
    return msg


async def _grant_clear(note):
    return None


async def _grant_fail(note):
    return None


async def _grant_edit(q, key, rich, plain, keyboard):
    store = render.GRANT_STATE[key]
    store["touched"] = time.time()
    if q.message is not None:
        store["anchor"] = (int(q.message.chat_id), int(q.message.message_id))
    return await edit_rich(
        q.get_bot(),
        q.message.chat_id,
        q.message.message_id,
        rich,
        plain,
        keyboard,
    )


async def _edit_store(store, rich, plain, keyboard):
    msg = store.get("message")
    if msg is None:
        return
    try:
        await edit_rich(
            msg.get_bot(),
            msg.chat_id,
            msg.message_id,
            rich,
            plain,
            keyboard,
        )
    except Exception as e:
        log.warning("Grant screen edit failed | err=%r", e)


async def _deliver_to_recipient(context, inst, owner_id: int) -> bool:
    try:
        owner_chat = await context.bot.get_chat(owner_id)
        label = " ".join(
            x for x in [getattr(owner_chat, "first_name", None), getattr(owner_chat, "last_name", None)] if x
        ) or "the owner"
    except Exception:
        label = "the owner"
    rich, plain = render.grant_recipient_content(inst, label)
    try:
        sent = await send_rich(
            context.bot,
            inst["user_id"],
            None,
            rich,
            plain,
            render.grant_recipient_keyboard(),
        )
        register_message_owner(inst["user_id"], message_id_from_send(sent), inst["user_id"])
        return True
    except Exception as e:
        log.warning("Grant delivery failed | user=%s err=%r", inst["user_id"], e)
        return False


async def _grant_launch(context, key):
    store = render.GRANT_STATE[key]
    target_uid = int(store["target_id"])
    tier_id = store["tier"]
    days = int(store["days"])
    region = store["region"]
    os_id = store["os"]
    owner_id = int(store["owner_id"])

    t = get_tier(tier_id)
    await _edit_store(store, *render.grant_progress_content(target_uid, tier_id, days, region, os_id), None)
    note = await _grant_notice(store.get("message"))

    vps_id = ""
    created_instance = None
    created_account = None
    try:
        vps_id = vps_db.next_vps_id(target_uid)
        res, password = await aws.launch_vps(
            region=region,
            os_id=os_id,
            instance_type=t["instance_type"],
            disk_gb=t["disk_gb"],
            tag_name=vps_id,
        )
        created_instance = res["instance_id"]
        created_account = res.get("account")
        ip = await aws.wait_for_public_ip(
            res["instance_id"], region=region, max_attempts=25, delay=3.0, account=created_account
        )
        if ip:
            await _grant_clear(note)
            note = await _grant_notice(store.get("message"))
            await aws.wait_for_ssh(ip, max_wait_sec=180, interval=5.0)

        now = time.time()
        inst = vps_db.save_instance(
            vps_id=vps_id,
            user_id=target_uid,
            instance_id=res["instance_id"],
            instance_type=t["instance_type"],
            region=region,
            tier=tier_id,
            os_image=os_id,
            disk_gb=t["disk_gb"],
            ip_address=ip or "",
            root_password=password,
            price=0,
            days=days,
            created_at=now,
            expires_at=now + (days * 86400),
            warranty_until=now + (WARRANTY_DAYS * 86400),
            account=created_account,
            sg_id=res.get("sg_id"),
        )
        vps_db.log_audit(
            vps_id,
            "GRANT",
            owner_id,
            f"target={target_uid} tier={tier_id} region={region} days={days} account={created_account}",
        )
        service.invalidate_account_scan()

        await _grant_clear(note)
        delivered = await _deliver_to_recipient(context, inst, owner_id)

        render.GRANT_STATE.pop(key, None)
        await _edit_store(
            store, *render.grant_result_content(inst, delivered), render.grant_result_keyboard(inst)
        )

        try:
            target_name, target_username = await notify.buyer_label(context.bot, target_uid)
            await notify.notify_owner(
                context.bot,
                inst,
                source="grant",
                buyer_name=target_name,
                buyer_username=target_username,
                note=f"Granted by owner {owner_id}",
            )
        except Exception as e:
            log.warning("Owner grant notice failed | vps_id=%s err=%r", vps_id, e)
    except Exception as e:
        log.error("Grant VPS failed | err=%r", e)
        await _grant_clear(note)
        cleaned = ""
        if created_instance:
            try:
                await aws.terminate_vps(created_instance, region=region, account=created_account)
                cleaned = f" Orphan instance {created_instance} was terminated."
                vps_db.log_audit(
                    vps_id or None,
                    "GRANT_ROLLBACK",
                    owner_id,
                    f"aws={created_instance} region={region} account={created_account} err={str(e)[:200]}",
                )
            except Exception as term_err:
                log.error("Grant rollback failed | aws=%s err=%r", created_instance, term_err)
                cleaned = f" Orphan instance {created_instance} in {region} could not be terminated."
        service.invalidate_account_scan()
        render.GRANT_STATE.pop(key, None)
        await _edit_store(
            store,
            *render.grant_failed_content(target_uid, f"{e}.{cleaned}"),
            render.grant_failed_keyboard(target_uid),
        )


async def grant_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await _grant_callback(update, context)
    except Exception as e:
        log.exception("Grant callback crashed | data=%r err=%r", getattr(q, "data", None), e)
        if q:
            try:
                await q.answer("Grant error. Try again.", show_alert=True)
            except Exception:
                pass


async def _grant_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    parts = [p for p in q.data.split(":") if p]
    if len(parts) < 2 or parts[0] != OWNER_PREFIX:
        log.warning("Grant callback ignored | wrong prefix data=%r", q.data)
        return
    action = parts[1]

    if action == "noop":
        return await _answer(q)

    key = await _grant_scope(q)
    if key is None:
        log.warning(
            "Grant callback rejected | action=%s data=%s user=%s",
            action, q.data, getattr(q.from_user, "id", None),
        )
        return
    store = render.GRANT_STATE[key]
    log.info(
        "Grant callback | action=%s data=%s user=%s chat=%s",
        action, q.data, getattr(q.from_user, "id", None), getattr(q.message, "chat_id", None),
    )

    if action == "gcancel":
        render.GRANT_STATE.pop(key, None)
        await _answer(q, "Grant cancelled.")
        return await _edit_store(
            {"message": q.message, "owner_id": q.from_user.id},
            *render.grant_closed_content(),
            None,
        )

    await _answer(q, NOTICE_LOADING)
    target_id = int(store["target_id"])

    if action == "gtier":
        if len(parts) >= 4 and parts[3] in GRANT_TIERS:
            store["tier"] = parts[3]
            store["days"] = 30
            return await _grant_edit(
                q, key,
                *render.grant_days_content(target_id, store["tier"]),
                render.grant_days_keyboard(target_id, store["tier"]),
            )
        return await _grant_edit(
            q, key,
            *render.grant_tier_content(store["username"], target_id),
            render.grant_tier_keyboard(target_id),
        )

    if action == "gdays":
        if len(parts) < 5:
            return
        store["tier"] = parts[3] if parts[3] in GRANT_TIERS else store["tier"]
        days = int(parts[4])
        store["days"] = days
        note = await _grant_notice(q.message)
        try:
            regions = await service.get_available_regions()
        finally:
            await _grant_clear(note)
        if not regions:
            return await _edit_store(
                store,
                *render.grant_failed_content(target_id, "No AWS region is available right now."),
                render.grant_failed_keyboard(target_id),
            )
        return await _grant_edit(
            q, key,
            *render.grant_region_content(target_id, store["tier"], days, 0, regions),
            render.grant_region_keyboard(target_id, store["tier"], days, 0, regions),
        )

    if action == "gregion":
        if len(parts) < 6:
            return
        days = int(parts[4])
        page = int(parts[5])
        store["days"] = days
        note = await _grant_notice(q.message)
        try:
            regions = await service.get_available_regions()
        finally:
            await _grant_clear(note)
        return await _grant_edit(
            q, key,
            *render.grant_region_content(target_id, store["tier"], days, page, regions),
            render.grant_region_keyboard(target_id, store["tier"], days, page, regions),
        )

    if action == "gos":
        if len(parts) < 6:
            return
        days = int(parts[4])
        region = parts[5]
        page = int(parts[6]) if len(parts) > 6 else 0
        store["days"] = days
        store["region"] = region
        account = aws.account_for_region(region)
        note = await _grant_notice(q.message)
        try:
            os_list = await aws.available_os_images(region, account)
        finally:
            await _grant_clear(note)
        if not os_list:
            os_list = list(render.OS_IMAGES)
        return await _grant_edit(
            q, key,
            *render.grant_os_content(target_id, store["tier"], days, region, os_list),
            render.grant_os_keyboard(target_id, store["tier"], days, region, os_list, page),
        )

    if action == "ggo":
        if len(parts) < 7:
            return
        days = int(parts[4])
        region = parts[5]
        os_id = parts[6]
        store["days"] = days
        store["region"] = region
        store["os"] = os_id
        return await _grant_edit(
            q, key,
            *render.grant_confirm_content(target_id, store["tier"], days, region, os_id),
            render.grant_confirm_keyboard(target_id, store["tier"], days, region, os_id),
        )

    if action == "ggrant":
        if len(parts) < 7:
            log.warning("Grant deploy skipped | malformed data=%r", q.data)
            return
        store["days"] = int(parts[4])
        store["region"] = parts[5]
        store["os"] = parts[6]
        store["message"] = q.message
        return await _grant_launch(context, key)

    log.warning("Grant action unknown | action=%s data=%s", action, q.data)
    return None


async def vpsgrant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return

    args = [a for a in (context.args or []) if (a or "").strip()]
    if not args and not (msg.reply_to_message and msg.reply_to_message.from_user):
        return await msg.reply_text(USAGE_TEXT, parse_mode="HTML")

    target_token = None
    tier_id = None
    days = None
    region = None
    for a in args:
        low = a.strip().lower()
        if get_tier(low) and tier_id is None:
            tier_id = get_tier(low)["id"]
            continue
        if target_token is None and not get_tier(low) and _parse_grant_days(a) is None and not REGION_RE.match(low):
            target_token = a.strip()
            continue
        if days is None and _parse_grant_days(a) is not None and tier_id is not None:
            days = _parse_grant_days(a)
            continue
        if region is None and REGION_RE.match(low):
            region = low
            continue

    target_uid = await resolve_target_user_id(update, context, target_token)
    if not target_uid:
        return await msg.reply_text(
            "<b>Could not resolve that user.</b>\n"
            "That username must have been seen by the bot before, or use a numeric ID / reply.",
            parse_mode="HTML",
        )

    try:
        target_chat = await context.bot.get_chat(target_uid)
    except Exception:
        target_chat = None
    username = (
        getattr(target_chat, "username", None)
        or getattr(target_chat, "first_name", None)
        or str(target_uid)
    )

    key = (int(chat.id), int(msg.message_id))
    render.GRANT_STATE[key] = {
        "owner_id": int(user.id),
        "target_id": int(target_uid),
        "username": username,
        "tier": tier_id or "1g",
        "days": days or 30,
        "region": region or DEFAULT_REGION,
        "os": "ubuntu-24.04",
        "created_at": time.time(),
        "touched": time.time(),
    }
    render.purge_grant_state()

    if tier_id and days and region:
        t = get_tier(tier_id)
        await _chat_action(msg)
        status = await msg.reply_text(
            f"<b>Deploying {t['label']} for {target_uid} in {region}...</b>",
            parse_mode="HTML",
        )
        render.GRANT_STATE[key]["message"] = status
        return await _grant_launch(context, key)

    rich, plain = render.grant_tier_content(username, int(target_uid))
    render.GRANT_STATE[key]["anchor"] = (int(chat.id), int(msg.message_id))
    sent = await send_rich(
        context.bot,
        chat.id,
        msg.message_id,
        rich,
        plain,
        render.grant_tier_keyboard(int(target_uid)),
    )
    register_message_owner(chat.id, message_id_from_send(sent), user.id)


