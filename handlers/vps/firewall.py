import asyncio
import logging
import re
import time

from telegram import Update
from telegram.ext import ContextTypes

from database import vps_db

from . import aws, render
from .callbacks import NOTICE_LOADING, _answer, _render_and_edit
from .constants import DEFAULT_REGION

log = logging.getLogger(__name__)

FIREWALL_PREFIX = "fwr"
PROTECTED_PORTS = {22}
FIREWALL_SESSIONS: dict[tuple[int, int], dict] = {}
PENDING_TTL = 600

PORT_RE = re.compile(r"^\d{1,5}(-\d{1,5})?$")


def _owned_instance(user_id: int, vps_id: str):
    inst = vps_db.get_instance(vps_id)
    if not inst:
        return None, "Instance not found."
    if int(user_id) != int(inst["user_id"]):
        return None, "This is not your instance."
    if inst["status"] not in ("active", "replaced"):
        return None, "This server is no longer active."
    if not inst.get("instance_id"):
        return None, "This server has no AWS instance id. Contact support."
    return inst, None


def _ensure_sg(inst: dict) -> str | None:
    if inst.get("sg_id"):
        return inst["sg_id"]
    try:
        sg = aws.instance_security_group_sync(
            inst["instance_id"],
            inst.get("region") or DEFAULT_REGION,
            inst.get("account"),
        )
    except Exception as e:
        log.error("Firewall sg lookup failed | vps=%s err=%r", inst.get("vps_id"), e)
        return None
    if sg:
        vps_db.set_instance_sg(inst["vps_id"], sg)
        inst["sg_id"] = sg
        log.info("Firewall sg attached | vps=%s sg=%s", inst.get("vps_id"), sg)
    return sg


async def _ensure_sg_async(inst: dict) -> str | None:
    if inst.get("sg_id"):
        return inst["sg_id"]
    instance_id = inst.get("instance_id")
    if not instance_id:
        return None
    sg = await aws.instance_security_group(
        instance_id,
        inst.get("region") or DEFAULT_REGION,
        inst.get("account"),
    )
    if sg:
        inst["sg_id"] = sg
        log.info("Firewall sg resolved | vps=%s sg=%s", inst.get("vps_id"), sg)
    return sg


async def _attach_or_create(params: dict) -> str:
    inst = params["inst"]
    sg = inst.get("sg_id") or await _ensure_sg_async(inst)
    if sg:
        return sg

    region = inst.get("region") or DEFAULT_REGION
    account = inst.get("account")
    log.info("Firewall creating own sg | vps=%s region=%s account=%s",
             inst.get("vps_id"), region, account)
    sg = await asyncio.to_thread(
        aws.ensure_vps_security_group,
        region,
        inst["vps_id"],
        inst.get("instance_id"),
        account,
    )
    vps_db.set_instance_sg(inst["vps_id"], sg)
    inst["sg_id"] = sg
    vps_db.log_audit(inst["vps_id"], "FIREWALL_SG_ATTACHED", inst.get("user_id"), sg)
    return sg


async def _prepare(q, user_id: int, vps_id: str, action: str):
    inst, err = await asyncio.to_thread(_owned_instance, user_id, vps_id)
    if err:
        log.warning("Firewall denied | user=%s vps=%s action=%s reason=%s",
                    user_id, vps_id, action, err)
        await _answer(q, err, show_alert=True)
        return None
    if inst.get("sg_id"):
        return inst
    sg = await _attach_or_create({"inst": inst})
    inst["sg_id"] = sg
    return inst


async def _load_rules(inst: dict) -> dict:
    return await aws.list_firewall_rules(
        inst["sg_id"],
        inst.get("region") or DEFAULT_REGION,
        inst.get("account"),
    )


async def _open_menu(q, inst: dict):
    log.info("Firewall menu open | vps=%s sg=%s region=%s account=%s",
             inst.get("vps_id"), inst.get("sg_id"), inst.get("region"), inst.get("account"))
    rules = await _load_rules(inst)
    log.info("Firewall rules loaded | vps=%s in=%d out=%d",
             inst.get("vps_id"), len(rules.get("in") or []), len(rules.get("out") or []))
    rich, plain = render.firewall_content(inst, rules)
    return await _render_and_edit(q, rich, plain, render.firewall_keyboard(inst["vps_id"]))


def _normalize_ports(raw: str) -> tuple[int, int] | None:
    s = (raw or "").strip()
    if not PORT_RE.match(s):
        return None
    if "-" in s:
        a, b = s.split("-", 1)
        fr, to = int(a), int(b)
    else:
        fr = to = int(s)
    if fr < 1 or to > 65535 or fr > to:
        return None
    return fr, to


def _touches_ssh(fr: int, to: int) -> bool:
    return fr <= 22 <= to


async def _apply_rule(q, user_id: int, inst: dict, direction: str, fr: int, to: int):
    if _touches_ssh(fr, to):
        return await _answer(
            q, "Port 22 (SSH) is always open and cannot be changed.", show_alert=True
        )

    region = inst.get("region") or DEFAULT_REGION
    account = inst.get("account")
    try:
        rule_id = await aws.add_firewall_rule(
            inst["sg_id"], region, direction, "tcp", fr, to, "0.0.0.0/0", account
        )
    except Exception as e:
        msg = str(e)
        if "InvalidPermission.Duplicate" in msg:
            return await _answer(q, "That rule already exists.", show_alert=True)
        log.error("Firewall add failed | vps=%s err=%r", inst["vps_id"], e)
        return await _answer(q, f"Failed: {msg[:160]}", show_alert=True)

    vps_db.add_firewall_rule(inst["vps_id"], direction, "tcp", fr, to, "0.0.0.0/0", rule_id)
    vps_db.log_audit(
        inst["vps_id"], "FIREWALL_ADD", user_id, f"{direction} tcp {fr}-{to} 0.0.0.0/0"
    )
    return await _open_menu(q, inst)


async def firewall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await _firewall_callback(update, context)
    except Exception:
        log.exception("Firewall callback crashed")
        q = update.callback_query
        if q:
            try:
                await q.answer("Firewall error. Try again or contact support.", show_alert=True)
            except Exception:
                pass


async def _firewall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not q or not q.data or not user:
        return

    parts = [p for p in str(q.data or "").split(":") if p != ""]
    if len(parts) < 3:
        log.error("Firewall callback malformed | data=%r", q.data)
        return await _answer(q, "Malformed firewall button.", show_alert=True)
    vps_id = parts[1]
    action = parts[2]

    inst = await _prepare(q, user.id, vps_id, action)
    if not inst:
        return
    await _answer(q, NOTICE_LOADING)

    if action == "menu":
        return await _open_menu(q, inst)

    if action == "add":
        direction = parts[3] if len(parts) > 3 else "in"
        rich, plain = render.firewall_pick_content(inst, direction)
        return await _render_and_edit(
            q, rich, plain, render.firewall_pick_keyboard(vps_id, direction)
        )

    if action == "preset":
        if len(parts) < 5:
            return None
        direction = parts[3]
        port = int(parts[4])
        return await _apply_rule(q, user.id, inst, direction, port, port)

    if action in ("custom", "range"):
        direction = parts[3] if len(parts) > 3 else "in"
        label = "Inbound" if direction == "in" else "Outbound"
        if action == "range":
            hint = (
                "Send the port range, for example:\n"
                "<code>8000-8100</code>"
            )
        else:
            hint = (
                "Send the port number, for example:\n"
                "<code>8080</code>"
            )
        sent = await q.message.reply_text(
            f"<b>🛡️ {label} rule — {vps_id}</b>\n\n"
            f"Protocol: <b>TCP</b>\n\n{hint}\n\n"
            "Open to all IP addresses (0.0.0.0/0).\n\n"
            "<i>Reply to this message with the port.</i>",
            parse_mode="HTML",
        )
        _create_prompt(sent, q.message.chat_id, user.id, vps_id, direction, action == "range")
        return sent

    if action == "del":
        if len(parts) > 3 and parts[3] == "menu":
            rules = vps_db.list_firewall_rules(vps_id)
            removable = [
                r for r in rules
                if not (r["protocol"] == "tcp" and r["from_port"] == 22 and r["to_port"] == 22)
            ]
            if not removable:
                return await _answer(q, "No removable rules yet.", show_alert=True)
            for r in removable:
                r["_text"] = render._rule_text({
                    "protocol": r["protocol"],
                    "from_port": r["from_port"],
                    "to_port": r["to_port"],
                    "cidr": r["cidr"],
                })
            return await _render_and_edit(
                q,
                f"<h1>🛡️ Remove a Rule — {render.html.escape(str(vps_id))}</h1>"
                "<p>Pick a rule to remove.</p>",
                f"<b>🛡️ Remove a Rule — {vps_id}</b>\n\nPick a rule to remove.",
                render.firewall_delete_keyboard(vps_id, removable),
            )

    if action == "rm":
        if len(parts) < 4:
            return None
        rule = vps_db.delete_firewall_rule(int(parts[3]))
        if not rule:
            return await _answer(q, "Rule not found.", show_alert=True)
        if rule["protocol"] == "tcp" and rule["from_port"] == 22 and rule["to_port"] == 22:
            vps_db.add_firewall_rule(
                vps_id, rule["direction"], rule["protocol"],
                rule["from_port"], rule["to_port"], rule["cidr"], rule.get("aws_rule_id"),
            )
            return await _answer(q, "Port 22 cannot be removed.", show_alert=True)

        await aws.remove_firewall_rule(
            inst["sg_id"],
            inst.get("region") or DEFAULT_REGION,
            rule["direction"],
            rule["protocol"],
            rule["from_port"],
            rule["to_port"],
            rule["cidr"],
            inst.get("account"),
        )
        vps_db.log_audit(
            vps_id,
            "FIREWALL_REMOVE",
            user.id,
            f"{rule['direction']} {rule['protocol']} {rule['from_port']}-{rule['to_port']} {rule['cidr']}",
        )
        return await _open_menu(q, inst)

    return None


def cancel_pending(chat_id: int, user_id: int) -> bool:
    found = False
    for key, value in list(FIREWALL_SESSIONS.items()):
        if key[0] == int(chat_id) and int(value.get("user_id", 0)) == int(user_id):
            FIREWALL_SESSIONS.pop(key, None)
            found = True
    return found


def _create_prompt(sent, chat_id: int, user_id: int, vps_id: str, direction: str, is_range: bool):
    mid = getattr(sent, "message_id", None)
    if not mid:
        log.error("Firewall prompt tanpa message_id | vps=%s", vps_id)
        return
    FIREWALL_SESSIONS[(int(chat_id), int(mid))] = {
        "user_id": int(user_id),
        "vps_id": vps_id,
        "direction": direction,
        "range": is_range,
        "created_at": time.time(),
    }
    log.info("Firewall prompt dibuat | chat=%s msg=%s vps=%s dir=%s",
             chat_id, mid, vps_id, direction)


def _pending_by_message(chat_id: int, reply_mid):
    if not reply_mid:
        return None
    session = FIREWALL_SESSIONS.get((int(chat_id), int(reply_mid)))
    if not session:
        return None
    return (int(chat_id), int(reply_mid)), session


def _cleanup_sessions():
    now = time.time()
    for key, value in list(FIREWALL_SESSIONS.items()):
        if now - float(value.get("created_at", 0)) > PENDING_TTL:
            FIREWALL_SESSIONS.pop(key, None)


async def firewall_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.ext import ApplicationHandlerStop

    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    reply = getattr(msg, "reply_to_message", None)
    if not reply:
        return

    raw_text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
    chat_id = int(msg.chat_id)
    _cleanup_sessions()
    found = _pending_by_message(chat_id, reply.message_id)
    if not found:
        return
    key, pending = found
    if int(pending.get("user_id", 0)) != int(user.id):
        return
    if not raw_text:
        log.info("Firewall input tanpa teks | chat=%s user=%s type=%s",
                 chat_id, user.id, type(msg).__name__)
        await msg.reply_text(
            "<b>Send the port as text.</b> Example: <code>8080</code>",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    log.info("Firewall input | chat=%s user=%s vps=%s text=%r",
             chat_id, user.id, pending.get("vps_id"), raw_text[:40])

    if time.time() - float(pending.get("created_at", 0)) > PENDING_TTL:
        FIREWALL_SESSIONS.pop(key, None)
        await msg.reply_text(
            "<b>Firewall input timed out.</b> Open the Firewall menu and try again.",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    text = raw_text
    if text.lower() in ("/cancel", "cancel"):
        FIREWALL_SESSIONS.pop(key, None)
        await msg.reply_text("<b>Cancelled.</b>", parse_mode="HTML")
        raise ApplicationHandlerStop

    ports = _normalize_ports(text)
    if not ports:
        await msg.reply_text(
            "<b>Invalid port.</b> Send <code>8080</code> or a range like "
            "<code>8000-8100</code>.",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    if _touches_ssh(ports[0], ports[1]):
        await msg.reply_text(
            "<b>Port 22 (SSH) is always open and cannot be changed.</b> "
            "Choose a different port.",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    FIREWALL_SESSIONS.pop(key, None)
    inst, err = await asyncio.to_thread(_owned_instance, user.id, pending["vps_id"])
    if err:
        await msg.reply_text(f"<b>{err}</b>", parse_mode="HTML")
        raise ApplicationHandlerStop

    region = inst.get("region") or DEFAULT_REGION
    account = inst.get("account")
    sg_id = inst.get("sg_id") or await _ensure_sg_async(inst)
    if not sg_id:
        await msg.reply_text(
            "<b>Firewall unavailable.</b> Could not resolve the security group. Contact support.",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop
    try:
        rule_id = await aws.add_firewall_rule(
            sg_id, region, pending["direction"], "tcp",
            ports[0], ports[1], "0.0.0.0/0", account,
        )
    except Exception as e:
        msg_text = str(e)
        if "InvalidPermission.Duplicate" in msg_text:
            await msg.reply_text("<b>That rule already exists.</b>", parse_mode="HTML")
        else:
            await msg.reply_text(
                f"<b>Failed:</b> <code>{msg_text[:200]}</code>", parse_mode="HTML"
            )
        raise ApplicationHandlerStop

    vps_db.add_firewall_rule(
        inst["vps_id"], pending["direction"], "tcp",
        ports[0], ports[1], "0.0.0.0/0", rule_id,
    )
    vps_db.log_audit(
        inst["vps_id"],
        "FIREWALL_ADD",
        user.id,
        f"{pending['direction']} tcp {ports[0]}-{ports[1]} 0.0.0.0/0",
    )
    rules = await _load_rules(inst)
    rich, plain = render.firewall_content(inst, rules)
    await msg.reply_text(
        plain, parse_mode="HTML", reply_markup=render.firewall_keyboard(inst["vps_id"])
    )
    raise ApplicationHandlerStop
