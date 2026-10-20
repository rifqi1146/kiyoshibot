import asyncio
import html
import io
import logging
import secrets
import time

from telegram import Update
from telegram.ext import ContextTypes

from database import vps_db
from utils import axiom
from utils.config import AXIOM_BASE_URL, OWNER_ID

from . import aws, notify, render, service
from .constants import DEFAULT_REGION, FEE_BPS, WARRANTY_DAYS, get_os, get_tier, normalize_tier
from .rich import edit_rich

log = logging.getLogger(__name__)


def _is_owner(user_id: int) -> bool:
    return int(user_id) in OWNER_ID


async def _answer(q, text: str | None = None, show_alert: bool = False, url: str | None = None):
    try:
        await q.answer(text, show_alert=show_alert, url=url)
    except Exception:
        pass


async def _start_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE, tier_id: str, days: int, region: str, os_id: str):
    q = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if not q or not user or not chat:
        return

    t = get_tier(tier_id)
    o = get_os(os_id)
    if not t or not o:
        return await _answer(q, "❌ Invalid plan or operating system.", show_alert=True)
    if not region:
        return await _answer(q, "❌ Invalid region.", show_alert=True)

    base = render._price_for(tier_id, days)
    gross = service.calculate_gross_amount(base, FEE_BPS)

    existing = vps_db.active_invoice_for_user(user.id)
    if existing:
        vps_db.set_invoice_status(existing["short_id"], "CANCELLED")
        service.stop_polling_job(context, existing["short_id"])

    await _answer(q, "🔄 Creating QRIS invoice...")
    try:
        data = await axiom.create_qris(gross)
    except Exception as e:
        log.error("VPS create QRIS failed | err=%r", e)
        return await _answer(q, f"❌ Failed to create QRIS: {e}", show_alert=True)

    now = time.time()
    short_id = secrets.token_hex(6)
    deadline = now + 300

    inv = vps_db.save_invoice(
        short_id=short_id,
        user_id=user.id,
        chat_id=chat.id,
        qris_id=data["qris_id"],
        trx_id=data.get("trx_id") or f"VPS-{short_id.upper()}",
        tier=tier_id,
        os_image=os_id,
        region=region,
        days=days,
        base_amount=base,
        gross_amount=gross,
        created_at=now,
        deadline=deadline,
        account=aws.account_for_region(region),
    )

    rich, plain = render.invoice_content(inv, status="PENDING", remaining=300)
    markup = render.invoice_keyboard(short_id, data["qr_url"])

    photo_input = None
    if data.get("qris_string"):
        try:
            photo_input = io.BytesIO(service.render_qr_png(data["qris_string"]))
        except Exception:
            pass
    if photo_input is None:
        photo_input = f"{AXIOM_BASE_URL}/qr/{data['qris_id']}?format=raw"

    sent = None
    sent_is_photo = True
    try:
        sent = await context.bot.send_photo(
            chat_id=chat.id,
            photo=photo_input,
            caption=plain,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except Exception as e:
        log.warning("VPS send_photo failed | err=%r", e)
        sent_is_photo = False
        try:
            sent = await context.bot.send_message(
                chat_id=chat.id,
                text=plain,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception as e2:
            log.error("VPS invoice send failed | err=%r", e2)

    if sent:
        vps_db.set_invoice_message_id(short_id, sent.message_id, has_photo=sent_is_photo)
        service.schedule_polling_job(context.application, short_id, chat.id, sent.message_id, user.id)


async def _render_and_edit(q, rich: str, plain: str, markup):
    if q.message is None:
        return
    await edit_rich(q.get_bot(), q.message.chat_id, q.message.message_id, rich, plain, markup)


async def vps_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    user = update.effective_user
    if not user:
        return

    parts = q.data.split(":")
    if len(parts) < 2 or parts[0] != "vps":
        return
    action = parts[1]

    vps_db.init_db()

    if action == "close":
        try:
            await q.message.delete()
        except Exception:
            pass
        return await _answer(q)

    if action == "noop":
        return await _answer(q)

    if action == "pm":
        if len(parts) < 3:
            return await _answer(q)
        start_arg = parts[2]
        uname = getattr(context.bot, "username", None) or ""
        if not uname:
            return await _answer(q, "Bot username is unavailable right now.", show_alert=True)
        return await _answer(q, "Opening private chat...", url=f"https://t.me/{uname}?start={start_arg}")

    if action == "menu":
        rich, plain = render.main_menu_content()
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.main_menu_keyboard())

    if action == "packages":
        rich, plain = render.packages_content()
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.packages_keyboard())

    if action == "tier":
        if len(parts) < 3:
            return
        tier_id = parts[2]
        rich, plain = render.duration_content(tier_id)
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.duration_keyboard(tier_id))

    if action == "region":
        if len(parts) < 4:
            return
        tier_id = parts[2]
        days = int(parts[3])
        page = int(parts[4]) if len(parts) > 4 else 0
        regions = await service.get_available_regions()
        rich, plain = render.region_content(tier_id, days, page, regions)
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.region_keyboard(tier_id, days, page, regions))

    if action == "os":
        if len(parts) < 5:
            return
        tier_id = parts[2]
        days = int(parts[3])
        region = parts[4]
        rich, plain = render.os_content(tier_id, days, region)
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.os_keyboard(tier_id, days, region))

    if action == "checkout":
        if len(parts) < 6:
            return
        tier_id = parts[2]
        days = int(parts[3])
        region = parts[4]
        os_id = parts[5]
        return await _start_checkout(update, context, tier_id, days, region, os_id)

    if action == "myvps":
        instances = await asyncio.to_thread(vps_db.list_user_instances, user.id)
        active = [i for i in instances if i["status"] in ("active", "replaced")]
        rich, plain = render.myvps_content(active)
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.myvps_keyboard(active))

    if action == "detail":
        if len(parts) < 3:
            return
        inst = vps_db.get_instance(parts[2])
        if not inst:
            return await _answer(q, "Instance not found.", show_alert=True)
        if int(user.id) != int(inst["user_id"]):
            return await _answer(q, "This is not your instance.", show_alert=True)
        rich, plain = render.instance_detail_content(inst)
        warranty_active = inst["status"] == "active" and time.time() < inst["warranty_until"] and not inst["warranty_claimed"]
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.instance_detail_keyboard(inst["vps_id"], inst["status"], warranty_active))

    if action == "restart":
        if len(parts) < 3:
            return
        inst = vps_db.get_instance(parts[2])
        if not inst:
            return await _answer(q, "Instance not found.", show_alert=True)
        if int(user.id) != int(inst["user_id"]):
            return await _answer(q, "This is not your instance.", show_alert=True)
        await _answer(q, "Requesting restart...")
        info = await aws.get_instance_info(
            inst["instance_id"], region=inst.get("region"), account=inst.get("account")
        )
        state = (info or {}).get("state", "unknown")
        vps_db.log_audit(inst["vps_id"], "RESTART_REQUEST", user.id, f"aws_state={state}")
        return await _answer(q, f"AWS state: {state}. Restart via SSH or contact support.", show_alert=True)

    if action == "warranty":
        if len(parts) < 3:
            return
        inst = vps_db.get_instance(parts[2])
        if not inst:
            return await _answer(q, "Instance not found.", show_alert=True)
        if int(user.id) != int(inst["user_id"]):
            return await _answer(q, "This is not your instance.", show_alert=True)
        if inst["status"] != "active" or inst["warranty_claimed"]:
            return await _answer(q, "Warranty is no longer available.", show_alert=True)
        if time.time() >= inst["warranty_until"]:
            return await _answer(q, "The warranty period has ended.", show_alert=True)

        rich, plain = render.warranty_confirm_content(inst)
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.warranty_confirm_keyboard(inst["vps_id"]))

    if action == "wconfirm":
        if len(parts) < 3:
            return
        inst = vps_db.get_instance(parts[2])
        if not inst:
            return await _answer(q, "Instance not found.", show_alert=True)
        if int(user.id) != int(inst["user_id"]):
            return await _answer(q, "This is not your instance.", show_alert=True)
        if inst["status"] != "active" or inst["warranty_claimed"]:
            return await _answer(q, "Warranty is no longer available.", show_alert=True)
        if time.time() >= inst["warranty_until"]:
            return await _answer(q, "The warranty period has ended.", show_alert=True)

        await _answer(q, "Testing server...")
        rich, plain = render.warranty_progress_content(inst)
        await _render_and_edit(q, rich, plain, None)

        healthy = await aws.check_vps_health(inst["ip_address"])
        if healthy:
            vps_db.log_audit(inst["vps_id"], "WARRANTY_CHECK", user.id, "healthy")
            rich, plain = render.warranty_healthy_content(inst)
            return await _render_and_edit(
                q, rich, plain, render.instance_detail_keyboard(inst["vps_id"], inst["status"], False)
            )

        vps_db.log_audit(inst["vps_id"], "WARRANTY_CLAIM", user.id, "unhealthy")

        t = get_tier(inst["tier"])
        o = get_os(inst["os_image"])
        if not t or not o:
            rich, plain = render.warranty_failed_content(inst, "Invalid plan or operating system.")
            return await _render_and_edit(
                q, rich, plain, render.instance_detail_keyboard(inst["vps_id"], inst["status"], False)
            )

        rich, plain = render.warranty_building_content(inst)
        await _render_and_edit(q, rich, plain, None)

        region = inst.get("region") or DEFAULT_REGION
        old_terminated = False
        try:
            new_vps_id = vps_db.next_vps_id(user.id)
            res, password = await aws.launch_vps(
                region=region,
                os_id=o["id"],
                instance_type=t["instance_type"],
                disk_gb=t["disk_gb"],
                tag_name=new_vps_id,
                account=inst.get("account"),
            )
            account_used = res.get("account") or inst.get("account")
            ip = await aws.wait_for_public_ip(
                res["instance_id"], region=region, max_attempts=25, delay=3.0, account=account_used
            )
            if ip:
                await aws.wait_for_ssh(ip, max_wait_sec=180, interval=5.0)

            now = time.time()
            remaining = max(0.0, inst["expires_at"] - now)
            new_inst = vps_db.save_instance(
                vps_id=new_vps_id,
                user_id=user.id,
                instance_id=res["instance_id"],
                instance_type=t["instance_type"],
                region=region,
                tier=inst["tier"],
                os_image=inst["os_image"],
                disk_gb=t["disk_gb"],
                ip_address=ip or "",
                root_password=password,
                price=0,
                days=int(remaining / 86400) or 1,
                created_at=now,
                expires_at=inst["expires_at"],
                warranty_until=now + (WARRANTY_DAYS * 86400),
                account=account_used,
            )
            vps_db.mark_warranty_claimed(inst["vps_id"], replaced_by=new_vps_id)
            await aws.terminate_vps(
                inst["instance_id"], region=region, account=inst.get("account")
            )
            old_terminated = True
            service.invalidate_account_scan()

            rich, plain = render.provisioned_content(new_inst)
            await _render_and_edit(
                q, rich, plain, render.instance_detail_keyboard(new_vps_id, "active", True)
            )

            try:
                buyer_name, buyer_username = await notify.buyer_label(context.bot, user.id)
                await notify.notify_owner(
                    context.bot,
                    new_inst,
                    source="warranty",
                    buyer_name=buyer_name,
                    buyer_username=buyer_username,
                    note=f"Replaces {inst['vps_id']} ({inst.get('instance_id')}) after a failed warranty check",
                )
            except Exception as notice_err:
                log.warning("Owner warranty notice failed | vps_id=%s err=%r", new_vps_id, notice_err)

            return await _answer(q, "Replacement server is ready.", show_alert=True)
        except Exception as e:
            log.error("Warranty replacement failed | err=%r", e)
            vps_db.log_audit(inst["vps_id"], "WARRANTY_REPLACE_FAILED", user.id, str(e))
            detail = str(e)
            if old_terminated:
                detail = f"{detail} (old server already terminated)"
            rich, plain = render.warranty_failed_content(inst, detail)
            return await _render_and_edit(
                q, rich, plain, render.instance_detail_keyboard(inst["vps_id"], inst["status"], False)
            )

    if action == "ref":
        if len(parts) < 3:
            return
        inv = vps_db.get_invoice(parts[2])
        if not inv:
            return await _answer(q, "Invoice not found.", show_alert=True)
        if int(user.id) != int(inv["user_id"]):
            return await _answer(q, "This is not your invoice.", show_alert=True)

        if inv["status"] in ("PAID", "PROVISIONING"):
            return await _answer(q, "Payment is already confirmed. Server is being provisioned.", show_alert=True)

        if inv["status"] in ("EXPIRED", "CANCELLED"):
            return await _answer(q, f"Invoice has {inv['status'].lower()}.", show_alert=True)

        try:
            res = await axiom.check_payment(qris_id=inv["qris_id"], trx_id=inv["trx_id"])
        except Exception as e:
            return await _answer(q, f"Failed to check status: {e}", show_alert=True)

        if res.get("paid") or res.get("status") == "PAID":
            service.stop_polling_job(context, inv["short_id"])
            await _answer(q, "Payment confirmed. Preparing your VPS...", show_alert=True)
            await service.provision_instance_for_invoice(context, inv)
            return

        remaining = int(inv["deadline"] - time.time())
        rem_m, rem_s = divmod(max(0, remaining), 60)
        return await _answer(q, f"Status PENDING. Time left {rem_m:02d}:{rem_s:02d}.", show_alert=False)

    if action == "cancel":
        if len(parts) < 3:
            return
        inv = vps_db.get_invoice(parts[2])
        if not inv:
            return await _answer(q, "Invoice not found.", show_alert=True)
        if int(user.id) != int(inv["user_id"]):
            return await _answer(q, "This is not your invoice.", show_alert=True)
        vps_db.set_invoice_status(inv["short_id"], "CANCELLED")
        service.stop_polling_job(context, inv["short_id"])
        try:
            await q.message.edit_caption(
                caption="<b>PAYMENT CANCELLED</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return await _answer(q, "Invoice cancelled.")


async def vpsowner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    user = update.effective_user
    if not user:
        return
    if not _is_owner(user.id):
        return await _answer(q, "Access denied.", show_alert=True)

    parts = q.data.split(":")
    if len(parts) < 2 or parts[0] != "vpsown":
        return
    action = parts[1]

    vps_db.init_db()

    if action == "close":
        try:
            await q.message.delete()
        except Exception:
            pass
        return await _answer(q)

    if action == "menu":
        await _answer(q)
        scan, _ = await service.get_account_scan()
        live_instances = scan.get("instances", [])
        regions_live = len({i.get("region") for i in live_instances})
        summary = {
            "count": len(live_instances),
            "regions": regions_live,
            "accounts": scan.get("accounts") or {},
        }
        rich, plain = render.owner_menu_content(live_summary=summary)
        return await _render_and_edit(q, rich, plain, render.owner_menu_keyboard())

    if action == "accounts":
        show_secrets = len(parts) > 2 and parts[2] == "1"
        rich, plain = render.accounts_content(show_secrets=show_secrets)
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.accounts_keyboard(show_secrets))

    if action == "accounts_secret":
        show_secrets = len(parts) > 2 and parts[2] == "1"
        rich, plain = render.accounts_content(show_secrets=show_secrets)
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.accounts_keyboard(show_secrets))

    if action == "accounts_rescan":
        await _answer(q, "Rescanning all accounts...")
        await asyncio.to_thread(aws.invalidate_region_sources)
        await asyncio.to_thread(aws._region_sources, True)
        service.invalidate_account_scan()
        rich, plain = render.accounts_content(show_secrets=False)
        return await _render_and_edit(q, rich, plain, render.accounts_keyboard(False))

    if action == "account":
        if len(parts) < 3:
            return
        name = parts[2]
        show_secrets = len(parts) > 3 and parts[3] == "1"
        rich, plain = render.account_detail_content(name, show_secrets=show_secrets)
        await _answer(q)
        return await _render_and_edit(
            q, rich, plain, render.account_detail_keyboard(name, show_secrets)
        )

    if action == "account_secret":
        if len(parts) < 3:
            return
        name = parts[2]
        show_secrets = len(parts) > 3 and parts[3] == "1"
        rich, plain = render.account_detail_content(name, show_secrets=show_secrets)
        await _answer(q)
        return await _render_and_edit(
            q, rich, plain, render.account_detail_keyboard(name, show_secrets)
        )

    if action == "account_toggle":
        if len(parts) < 3:
            return
        name = parts[2]
        from . import accounts as accounts_store

        cfg = accounts_store.get_account(name) or {}
        new_state = not bool(cfg.get("enabled", True))
        await asyncio.to_thread(accounts_store.set_account_enabled, name, new_state)
        await asyncio.to_thread(aws.invalidate_region_sources)
        service.invalidate_account_scan()
        await _answer(q, f"{name}: {'enabled' if new_state else 'disabled'}")
        rich, plain = render.account_detail_content(name, show_secrets=False)
        return await _render_and_edit(
            q, rich, plain, render.account_detail_keyboard(name, False)
        )

    if action == "stats":
        await _answer(q)
        scan, _ = await service.get_account_scan()
        live_instances = scan.get("instances", [])
        regions_live = len({i.get("region") for i in live_instances})
        summary = {"count": len(live_instances), "regions": regions_live}
        rich, plain = render.owner_menu_content(live_summary=summary)
        return await _render_and_edit(q, rich, plain, render.owner_menu_keyboard())

    if action == "list":
        show_terminated = len(parts) > 2 and parts[2] == "1"
        await _answer(q)
        db_instances = await asyncio.to_thread(vps_db.list_all_instances)
        scan, _ = await service.get_account_scan()
        merged = render.merge_with_live(db_instances, scan.get("instances", []), show_terminated=show_terminated)
        rich, plain = render.owner_list_content(merged, scan)
        return await _render_and_edit(q, rich, plain, render.owner_list_keyboard(merged, show_terminated))

    if action == "rescan":
        show_terminated = len(parts) > 2 and parts[2] == "1"
        await _answer(q, "Rescanning AWS...")
        db_instances = await asyncio.to_thread(vps_db.list_all_instances)
        scan, _ = await service.get_account_scan(force=True)
        merged = render.merge_with_live(db_instances, scan.get("instances", []), show_terminated=show_terminated)
        rich, plain = render.owner_list_content(merged, scan)
        return await _render_and_edit(q, rich, plain, render.owner_list_keyboard(merged, show_terminated))

    if action == "live":
        if len(parts) < 4:
            return
        instance_id = parts[2]
        region = parts[3]
        account = parts[4] if len(parts) > 4 else None
        await _answer(q, "Fetching live info...")
        info = await aws.get_instance_info(instance_id, region=region, account=account)
        rich, plain = render.owner_live_info_content(instance_id, region, info, account)
        state = (info or {}).get("state", "unknown")
        return await _render_and_edit(
            q, rich, plain, render.owner_live_keyboard(instance_id, region, state, account)
        )

    if action == "liveterm":
        if len(parts) < 4:
            return
        instance_id = parts[2]
        region = parts[3]
        account = parts[4] if len(parts) > 4 else None
        await _answer(q, "Terminating AWS instance...")
        await aws.terminate_vps(instance_id, region=region, account=account)
        vps_db.log_audit(
            None, "TERMINATE_UNMANAGED", user.id, f"aws={instance_id} region={region} account={account}"
        )
        service.invalidate_account_scan()
        info = await aws.get_instance_info(instance_id, region=region, account=account)
        rich, plain = render.owner_live_info_content(instance_id, region, info, account)
        state = (info or {}).get("state", "terminated")
        return await _render_and_edit(
            q, rich, plain, render.owner_live_keyboard(instance_id, region, state, account)
        )

    if action == "info":
        if len(parts) < 3:
            return
        inst = vps_db.get_instance(parts[2])
        if not inst:
            return await _answer(q, "Instance not found.", show_alert=True)
        rich, plain = render.owner_info_content(inst)
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.owner_instance_keyboard(inst["vps_id"], inst["status"]))

    if action == "term":
        if len(parts) < 3:
            return
        inst = vps_db.get_instance(parts[2])
        if not inst:
            return await _answer(q, "Instance not found.", show_alert=True)
        await _answer(q, "Terminating instance...")
        await aws.terminate_vps(
            inst["instance_id"], region=inst.get("region"), account=inst.get("account")
        )
        vps_db.terminate_instance_db(inst["vps_id"], reason=f"owner_terminate:{user.id}")
        service.invalidate_account_scan()
        vps_db.log_audit(inst["vps_id"], "TERMINATE_OWNER", user.id)
        rich, plain = render.owner_info_content(vps_db.get_instance(inst["vps_id"]))
        return await _render_and_edit(q, rich, plain, render.owner_instance_keyboard(inst["vps_id"], "terminated"))

    if action == "renew":
        if len(parts) < 4:
            return
        inst = vps_db.get_instance(parts[2])
        if not inst:
            return await _answer(q, "Instance not found.", show_alert=True)
        days = int(parts[3])
        updated = vps_db.renew_instance_db(inst["vps_id"], days)
        vps_db.log_audit(inst["vps_id"], "RENEW", user.id, f"+{days}d")
        await _answer(q, f"Renewed for {days} days.")
        rich, plain = render.owner_info_content(updated)
        return await _render_and_edit(q, rich, plain, render.owner_instance_keyboard(updated["vps_id"], updated["status"]))

    if action == "price":
        rich, plain = render.owner_price_content()
        await _answer(q)
        return await _render_and_edit(q, rich, plain, render.owner_price_keyboard())

    if action == "price_tier":
        if len(parts) < 3:
            return
        tier_id = normalize_tier(parts[2])
        t = get_tier(tier_id)
        pricing = vps_db.get_all_pricing()
        pr = pricing.get(tier_id, {})
        if not t:
            return await _answer(q, "Invalid plan.", show_alert=True)
        rich = (
            f"<h1>Set Price — {html.escape(t['label'])}</h1>"
            "<p>Send one of these commands to update pricing:</p>"
            f"<pre><code>/vpsprice {html.escape(tier_id)} 30d &lt;amount&gt;</code></pre>"
            f"<pre><code>/vpsprice {html.escape(tier_id)} 14d &lt;amount&gt;</code></pre>"
            f"<pre><code>/vpsprice {html.escape(tier_id)} 7d &lt;amount&gt;</code></pre>"
            f"<p>Current prices: 30 days <b>{render.fmt_rp(pr.get('price_30d', 0))}</b>, "
            f"14 days <b>{render.fmt_rp(pr.get('price_14d', 0))}</b>, "
            f"7 days <b>{render.fmt_rp(pr.get('price_7d', 0))}</b></p>"
        )
        plain = (
            f"<b>Set Price — {t['label']}</b>\n\n"
            f"30 days: {render.fmt_rp(pr.get('price_30d', 0))}\n"
            f"14 days: {render.fmt_rp(pr.get('price_14d', 0))}\n"
            f"7 days: {render.fmt_rp(pr.get('price_7d', 0))}\n\n"
            f"Use /vpsprice {tier_id} 30d <amount>, /vpsprice {tier_id} 14d <amount>, "
            f"or /vpsprice {tier_id} 7d <amount>"
        )
        return await _render_and_edit(q, rich, plain, render.owner_price_keyboard())
