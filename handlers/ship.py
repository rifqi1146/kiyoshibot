import html
import random
import time

from telegram import Update
from telegram.error import RetryAfter
from telegram.ext import ContextTypes

from database.ship_db import (
    get_users_pool,
    set_ship_last_time,
    get_ship_last_time,
    add_user,
    touch_user,
    _ship_db_init,
)

SHIP_COOLDOWN = 60 * 60 * 24  # 24 jam

SHIP_MESSAGES = [
    "🥰 You two look comfortable with each other",
    "💗 Soft vibes, you just get each other",
    "🌸 You match in a natural way",
    "💞 Like calming each other down without realizing it",
    "✨ You come alive more when you're together",
    "🫶 There's a sense of safety there",
    "🌷 Conversation between you always flows",
    "💫 The energy feels warm",
    "🤍 Simple but you can feel it",
    "🌼 You clearly support each other",
]

SHIP_ENDING = [
    "Hope you always get along 🤍",
    "It'd be cute if it were real 🥹",
    "Wishing you the best ✨",
    "Who knows, might be a sign 🌸",
    "Take it slow 💗",
    "Enjoy the moment 🫶",
]

_MEMBER_CACHE: dict[tuple[int, int], tuple[bool, float]] = {}
_MEMBER_CACHE_TTL = 600.0
_MAX_MEMBER_CACHE = 5000
_MAX_VERIFY = 30


def tag(u):
    name = html.escape(str(u.get("name") or "Unknown"))
    return f'<a href="tg://user?id={u["id"]}">{name}</a>'


def _bot_id(bot) -> int | None:
    try:
        return int(bot.id)
    except Exception:
        return None


async def _is_chat_member(bot, chat_id: int, user_id: int) -> bool:
    key = (int(chat_id), int(user_id))
    now = time.time()
    cached = _MEMBER_CACHE.get(key)
    if cached and (now - cached[1]) < _MEMBER_CACHE_TTL:
        return cached[0]
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        ok = m.status not in ("left", "kicked")
    except RetryAfter:
        if cached:
            return cached[0]
        ok = True
    except Exception:
        ok = False
    if len(_MEMBER_CACHE) > _MAX_MEMBER_CACHE:
        # LRU-ish eviction: buang entri tertua (insertion order) alih-alih .clear()
        # total yang memicu cache stampede setelah flush.
        overflow = len(_MEMBER_CACHE) - _MAX_MEMBER_CACHE
        for k in list(_MEMBER_CACHE.keys())[: max(1, overflow + _MAX_MEMBER_CACHE // 10)]:
            _MEMBER_CACHE.pop(k, None)
    _MEMBER_CACHE[key] = (ok, now)
    if ok:
        try:
            touch_user(chat_id, user_id)
        except Exception:
            pass
    return ok


async def _ensure_pool_has_members(bot, chat_id: int, pool: list[dict]) -> list[dict]:
    have = [p for p in pool if p.get("id") is not None]
    if len(have) >= 2:
        return pool
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception:
        return pool
    seen = set()
    for p in pool:
        try:
            seen.add(int(p["id"]))
        except Exception:
            pass
    for a in admins or []:
        u = getattr(a, "user", None)
        if not u or getattr(u, "is_bot", False):
            continue
        try:
            uid = int(u.id)
        except Exception:
            continue
        if uid == _bot_id(bot):
            continue
        try:
            add_user(chat_id, u)
        except Exception:
            pass
        if uid not in seen:
            seen.add(uid)
            pool.append({"id": uid, "name": str(u.first_name or "Unknown")})
    return pool


async def _pick_two_active(bot, chat_id: int, pool: list[dict]) -> list[dict]:
    cands = [p for p in pool if p.get("id") is not None]
    random.shuffle(cands)
    picked = []
    for p in cands[:_MAX_VERIFY]:
        try:
            uid = int(p["id"])
        except Exception:
            continue
        if await _is_chat_member(bot, chat_id, uid):
            picked.append({"id": uid, "name": str(p.get("name") or "Unknown")})
            if len(picked) == 2:
                break
    return picked


async def _pick_one_active(
    bot, chat_id: int, pool: list[dict], exclude: set[int]
) -> dict | None:
    cands = []
    for p in pool:
        try:
            uid = int(p["id"])
        except Exception:
            continue
        if uid in exclude:
            continue
        cands.append(p)
    random.shuffle(cands)
    for p in cands[:_MAX_VERIFY]:
        try:
            uid = int(p["id"])
        except Exception:
            continue
        if await _is_chat_member(bot, chat_id, uid):
            return {"id": uid, "name": str(p.get("name") or "Unknown")}
    return None


def format_remaining(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


async def ship_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    if chat.type == "private" or int(chat.id) > 0:
        return await msg.reply_text("❌ Ship feature can only be used in groups.")

    now = int(time.time())
    last_time = get_ship_last_time(chat.id)

    if now - last_time < SHIP_COOLDOWN:
        remain = SHIP_COOLDOWN - (now - last_time)
        return await msg.reply_text(
            f"⏳ <b>Ship is still on cooldown</b>\n\n"
            f"Next couple can be picked in:\n"
            f"<code>{format_remaining(remain)}</code>",
            parse_mode="HTML",
        )

    bid = _bot_id(context.bot)
    sender = msg.from_user
    sender_info = None
    if sender and not getattr(sender, "is_bot", False):
        try:
            add_user(chat.id, sender)
        except Exception:
            pass
        try:
            sid = int(sender.id)
        except Exception:
            sid = None
        if sid is not None and sid != bid:
            sender_info = {"id": sid, "name": str(sender.first_name or "Unknown")}

    targets: list[dict] = []
    seen_targets: set[int] = set()

    async def _push_candidate(u) -> None:
        if not u or getattr(u, "is_bot", False):
            return
        try:
            uid = int(u.id)
        except Exception:
            return
        if bid is not None and uid == bid:
            return
        if uid in seen_targets:
            return
        if not await _is_chat_member(context.bot, chat.id, uid):
            return
        try:
            add_user(chat.id, u)
        except Exception:
            pass
        seen_targets.add(uid)
        targets.append({"id": uid, "name": str(u.first_name or "Unknown")})

    if msg.reply_to_message and msg.reply_to_message.from_user:
        await _push_candidate(msg.reply_to_message.from_user)

    for ent in msg.entities or []:
        if ent.type == "text_mention" and ent.user:
            await _push_candidate(ent.user)

    users: list[dict] = []
    if len(targets) >= 2:
        users = targets[:2]
    elif len(targets) == 1:
        t = targets[0]
        if sender_info and t["id"] != sender_info["id"]:
            users = [sender_info, t]
        else:
            who = sender_info or t
            pool = get_users_pool(chat.id)
            pool = await _ensure_pool_has_members(context.bot, chat.id, pool)
            partner = await _pick_one_active(
                context.bot, chat.id, pool, {int(who["id"])}
            )
            if not partner:
                if len([p for p in pool if p.get("id") is not None]) < 2:
                    return await msg.reply_text("❌ Not enough people to ship.")
                return await msg.reply_text(
                    "❌ Could not find 2 active members to ship."
                )
            users = [who, partner]
    else:
        pool = get_users_pool(chat.id)
        pool = await _ensure_pool_has_members(context.bot, chat.id, pool)
        if len([p for p in pool if p.get("id") is not None]) < 2:
            return await msg.reply_text("❌ Not enough people to ship.")
        picked = await _pick_two_active(context.bot, chat.id, pool)
        if len(picked) < 2:
            return await msg.reply_text(
                "❌ Could not find 2 active members to ship."
            )
        users = picked

    u1, u2 = users[:2]

    percent = random.randint(50, 100)
    msg_text = random.choice(SHIP_MESSAGES)
    ending = random.choice(SHIP_ENDING)

    text = (
        f"💖 <b>SHIP RESULT</b>\n\n"
        f"👤 {tag(u1)}\n"
        f"👤 {tag(u2)}\n\n"
        f"❤️ <b>Love Meter:</b> <code>{percent}%</code>\n\n"
        f"{msg_text}\n"
        f"<i>{ending}</i>"
    )

    await msg.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    set_ship_last_time(chat.id, now)


try:
    _ship_db_init()
except Exception:
    pass
