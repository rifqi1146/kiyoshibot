import json
import time
import asyncio
import html
import random
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from utils.http import get_http_session
from utils.config import (
    GROQ_MODEL,
    GROQ_BASE,
    GROQ_KEY,
)

QUIZ_TIMEOUT = 30
QUIZ_TOTAL = 10

_ACTIVE_QUIZ: dict[int, dict] = {}

_EMOS = ["🧠", "🎯", "🔥", "✨", "📚"]
def _emo():
    return random.choice(_EMOS)

_QUESTION_STYLES = [
    "definisi konsep",
    "sebab dan akibat",
    "logika sederhana",
    "kasus singkat",
    "fakta unik",
    "perbandingan",
    "tebakan ilmiah ringan",
]

def _quiz_keyboard(chat_id: int, qidx: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("A", callback_data=f"quizans:{chat_id}:{qidx}:A"),
            InlineKeyboardButton("B", callback_data=f"quizans:{chat_id}:{qidx}:B"),
        ],
        [
            InlineKeyboardButton("C", callback_data=f"quizans:{chat_id}:{qidx}:C"),
            InlineKeyboardButton("D", callback_data=f"quizans:{chat_id}:{qidx}:D"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

def _render_question(q: dict, no: int) -> str:
    return (
        f"{_emo()} <b>QUIZ {no}/{QUIZ_TOTAL}</b>\n\n"
        f"<b>{html.escape(q['question'])}</b>\n\n"
        f"A. {html.escape(q['options']['A'])}\n"
        f"B. {html.escape(q['options']['B'])}\n"
        f"C. {html.escape(q['options']['C'])}\n"
        f"D. {html.escape(q['options']['D'])}\n\n"
        f"<i>Tap A / B / C / D ({QUIZ_TIMEOUT} detik)</i>"
    )

def _strip_codeblock(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.strip()
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    if s.endswith("```"):
        s = s[:-3].strip()
    return s

async def _generate_question_bank() -> list:
    seed = random.randint(100000, 999999)
    style = random.choice(_QUESTION_STYLES)

    prompt = (
        f"[SEED:{seed}]\n"
        f"Gaya soal: {style}\n\n"
        "Buatkan 10 soal pilihan ganda tingkat umum.\n"
        "Topik acak dari:\n"
        "- Pengetahuan umum\n"
        "- Ilmu pengetahuan sosial\n"
        "- Teknologi\n"
        "- Ilmu pengetahuan alam\n"
        "- Sejarah\n"
        "- Politik\n\n"
        "Gunakan Bahasa Indonesia.\n\n"
        "Format WAJIB JSON:\n"
        "[\n"
        "  {\n"
        '    "question": "...",\n'
        '    "options": {\n'
        '      "A": "...",\n'
        '      "B": "...",\n'
        '      "C": "...",\n'
        '      "D": "..."\n'
        "    },\n"
        '    "answer": "A"\n'
        "  }\n"
        "]\n\n"
        "Jawaban random A/B/C/D.\n"
        "Jangan beri teks lain selain JSON."
    )

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "Kamu adalah pembuat soal quiz profesional."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.95,
        "max_tokens": 2048,
    }

    session = await get_http_session()
    async with session.post(
        f"{GROQ_BASE}/chat/completions",
        json=payload,
        headers={
            "Authorization": f"Bearer {GROQ_KEY}",
            "Content-Type": "application/json",
        },
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError("Failed to generate quiz questions")
        data = await resp.json()

    raw = _strip_codeblock(data["choices"][0]["message"]["content"])
    bank = json.loads(raw)

    if not isinstance(bank, list) or len(bank) < QUIZ_TOTAL:
        raise RuntimeError("Invalid question bank")

    out = []
    for it in bank:
        if not isinstance(it, dict):
            continue
        q = it.get("question")
        opt = it.get("options") or {}
        ans = (it.get("answer") or "").strip().upper()

        if not isinstance(q, str) or not q.strip():
            continue
        if not isinstance(opt, dict):
            continue
        if not all(k in opt for k in ("A", "B", "C", "D")):
            continue
        if ans not in ("A", "B", "C", "D"):
            continue

        out.append({"question": q, "options": opt, "answer": ans})

    if len(out) < QUIZ_TOTAL:
        raise RuntimeError("Invalid question bank")

    return out[:QUIZ_TOTAL]

async def _send_or_edit_question(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz: dict):
    chat_id = quiz["chat_id"]
    qidx = quiz["current"]
    q = quiz["bank"][qidx]
    no = qidx + 1

    text = _render_question(q, no)
    kb = _quiz_keyboard(chat_id, qidx)

    quiz["start"] = time.time()
    quiz["answered"] = set()
    quiz["lock"] = False

    if quiz.get("timeout_task"):
        try:
            quiz["timeout_task"].cancel()
        except Exception:
            pass

    async def _timeout_guard():
        await asyncio.sleep(QUIZ_TIMEOUT + 0.2)

        live = _ACTIVE_QUIZ.get(chat_id)
        if not live or live is not quiz:
            return
        if quiz.get("current") != qidx:
            return
        if quiz.get("lock"):
            return

        quiz["lock"] = True

        if quiz["current"] >= QUIZ_TOTAL - 1:
            _ACTIVE_QUIZ.pop(chat_id, None)

            if quiz.get("timeout_task"):
                try:
                    quiz["timeout_task"].cancel()
                except Exception:
                    pass

            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=quiz["message_id"],
                    reply_markup=None,
                )
            except Exception:
                pass

            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=quiz["message_id"],
                    text="<b>Quiz finished!</b>\n\nCalculating scores...",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            await asyncio.sleep(2)
            return await _end_quiz(context, quiz)

        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=quiz["message_id"],
                text="<b>Time's up!</b>\n\nNext question...",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass

        await asyncio.sleep(1)

        quiz["current"] += 1
        quiz["lock"] = False
        await _send_or_edit_question(update, context, quiz)

    quiz["timeout_task"] = context.application.create_task(_timeout_guard())

    if quiz.get("message_id"):
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=quiz["message_id"],
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass

    if update.message:
        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb)

    quiz["message_id"] = sent.message_id

async def _end_quiz(context: ContextTypes.DEFAULT_TYPE, quiz: dict):
    chat_id = quiz["chat_id"]
    msg_id = quiz.get("message_id")

    scores = quiz["scores"]
    if not scores:
        text = "Quiz finished. Nobody answered."
    else:
        ranking = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        lines = ["🏆 <b>QUIZ RESULTS</b>\n"]
        for i, (uid, score) in enumerate(ranking, 1):
            try:
                member = await context.bot.get_chat_member(chat_id, uid)
                name = html.escape(member.user.full_name or "User")
                lines.append(f"{i}. <a href='tg://user?id={uid}'>{name}</a> — <b>{score}</b> pts")
            except Exception:
                lines.append(f"{i}. <code>{uid}</code> — <b>{score}</b> pts")

        text = "\n".join(lines)

    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat_id = update.effective_chat.id
    if chat_id in _ACTIVE_QUIZ:
        return await msg.reply_text("Quiz masih berjalan!")

    try:
        bank = await _generate_question_bank()
    except Exception:
        return await msg.reply_text("Failed to create quiz questions (Groq error).")

    quiz = {
        "chat_id": chat_id,
        "current": 0,
        "scores": {},
        "bank": bank,
        "message_id": None,
        "start": time.time(),
        "answered": set(),
        "timeout_task": None,
        "lock": False,
    }

    _ACTIVE_QUIZ[chat_id] = quiz
    await _send_or_edit_question(update, context, quiz)

async def quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return

    try:
        _, chat_id_s, qidx_s, chosen = q.data.split(":", 3)
        chat_id = int(chat_id_s)
        qidx = int(qidx_s)
        chosen = chosen.strip().upper()
    except Exception:
        return await q.answer("Invalid data", show_alert=True)

    quiz = _ACTIVE_QUIZ.get(chat_id)
    if not quiz:
        return await q.answer("Quiz is already over", show_alert=True)

    if q.message is None or q.message.message_id != quiz.get("message_id"):
        return await q.answer("This button is no longer valid", show_alert=True)

    if qidx != quiz["current"]:
        return await q.answer("Itu pertanyaan lama 😄", show_alert=True)

    if chosen not in ("A", "B", "C", "D"):
        return await q.answer("Invalid choice", show_alert=True)

    uid = q.from_user.id
    if uid in quiz["answered"]:
        return await q.answer("Lu udah jawab 😤", show_alert=True)

    if time.time() - quiz["start"] > QUIZ_TIMEOUT:
        return await q.answer("Time's up!", show_alert=True)

    quiz["answered"].add(uid)

    curq = quiz["bank"][quiz["current"]]
    correct = curq["answer"]

    if chosen == correct:
        quiz["scores"][uid] = quiz["scores"].get(uid, 0) + 1
        await q.answer("✅ Correct!", show_alert=False)
    else:
        await q.answer(f"❌ Incorrect. Answer: {correct}", show_alert=False)

    if quiz.get("lock"):
        return
    quiz["lock"] = True

    if quiz["current"] >= QUIZ_TOTAL - 1:
        _ACTIVE_QUIZ.pop(chat_id, None)

        if quiz.get("timeout_task"):
            try:
                quiz["timeout_task"].cancel()
            except Exception:
                pass

        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=quiz["message_id"],
                reply_markup=None,
            )
        except Exception:
            pass
        
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=quiz["message_id"],
                text="<b>Quiz finished!</b>\n\nCalculating scores...",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        
        await asyncio.sleep(2)
        return await _end_quiz(context, quiz)

    quiz["current"] += 1
    quiz["lock"] = False
    await _send_or_edit_question(update, context, quiz)
    