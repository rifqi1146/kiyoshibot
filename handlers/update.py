import html,asyncio,subprocess,traceback
from telegram import Update
from telegram.ext import ContextTypes
from utils.config import OWNER_ID

def _run(cmd):
    return subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)

def get_changelog():
    try:
        log=_run(["git","log","HEAD..origin/main","--pretty=format:%s"])
        if not log.stdout.strip():
            return None
        lines=log.stdout.strip().splitlines()
        return "\n".join(f"• {html.escape(line)}" for line in lines)
    except Exception:
        return None

async def update_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message
    user=update.effective_user
    if not msg or not user or user.id not in OWNER_ID:
        return
    status=await msg.reply_text("<b>Checking for updates...</b>",parse_mode="HTML")
    try:
        await asyncio.to_thread(subprocess.run,["git","fetch"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        check=await asyncio.to_thread(_run,["git","status","-uno"])
        if "behind" not in check.stdout:
            return await status.edit_text("<b>The bot is already up to date.</b>",parse_mode="HTML")
        changelog=await asyncio.to_thread(get_changelog)
        await status.edit_text("<b>Updating bot...</b>",parse_mode="HTML")
        pull=await asyncio.to_thread(_run,["git","pull"])
        if pull.returncode!=0:
            err=html.escape((pull.stderr or pull.stdout or "Unknown git pull error").strip())[:3500]
            return await status.edit_text(f"<b>Git pull failed</b>\n\n<code>{err}</code>",parse_mode="HTML")
        await status.edit_text("<b>Update successful!</b>\n\n<i>Reloading modules...</i>",parse_mode="HTML")
        from handlers.reload import hot_reload
        result=await hot_reload(context.application)
        text="<b>Update successful!</b>\n\n"
        if changelog:
            text+="📝 <b>Changelog:</b>\n"+changelog+"\n\n"
        else:
            text+="📝 <i>No changelog.</i>\n\n"
        text+=(
            "<b>Reload complete</b>\n\n"
            f"Modules: <code>{result.get('modules',0)}</code>\n"
            f"Handlers: <code>{result.get('handlers',0)}</code>\n\n"
            "<i>Handlers, utils, database, and RAG contexts refreshed.</i>"
        )
        await status.edit_text(text,parse_mode="HTML")
    except Exception as e:
        err=html.escape("".join(traceback.format_exception_only(type(e),e)).strip())[:3500]
        await status.edit_text(f"<b>Update failed</b>\n\n<code>{err}</code>",parse_mode="HTML")