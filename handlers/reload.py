import sys
import html
import importlib
import logging
import traceback
import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from utils.config import OWNER_ID

log=logging.getLogger(__name__)
_PREFIXES=("handlers","utils","database","rag")
_SPECIAL_LAST=("handlers.commands","handlers.messages","handlers.callbacks")
_SKIP_MODULES={__name__}

def _is_reloadable(name,module):
    if name in _SKIP_MODULES:
        return False
    if not any(name==p or name.startswith(p+".") for p in _PREFIXES):
        return False
    path=getattr(module,"__file__",None)
    return bool(path and path.endswith(".py"))

def _module_priority(name):
    if name.startswith("utils"):
        return 0
    if name.startswith("database"):
        return 1
    if name.startswith("rag"):
        return 2
    if name in _SPECIAL_LAST:
        return 9
    if name.startswith("handlers"):
        return 3
    return 8

def _collect_modules():
    items=[]
    for name,module in list(sys.modules.items()):
        if _is_reloadable(name,module):
            items.append(name)
    return sorted(set(items),key=lambda n:(_module_priority(n),n.count("."),n))

async def _pre_reload_cleanup(app):
    try:
        from utils.http import close_http_session
        await close_http_session()
    except Exception as e:
        log.warning("Reload cleanup http failed | err=%r",e)
    try:
        from handlers.dl.mtproto_uploader import shutdown_mtproto_uploader
        await shutdown_mtproto_uploader(app)
    except Exception as e:
        log.warning("Reload cleanup mtproto failed | err=%r",e)

def _reload_modules():
    importlib.invalidate_caches()
    reloaded=[]
    for name in _collect_modules():
        module=sys.modules.get(name)
        if not module:
            continue
        importlib.reload(module)
        reloaded.append(name)
    return reloaded

def _snapshot_handlers(app):
    return {group:list(handlers) for group,handlers in app.handlers.items()}

def _restore_handlers(app,snapshot):
    app.handlers.clear()
    for group,handlers in snapshot.items():
        app.handlers[group]=handlers

def _register_all(app):
    from handlers.commands import register_commands
    from handlers.messages import register_messages
    from handlers.callbacks import register_callbacks
    app.handlers.clear()
    register_commands(app)
    register_messages(app)
    register_callbacks(app)

async def _refresh_caca_cache():
    try:
        from database import caca_db
        await caca_db.init()
        log.info("Reload caca DB/cache refreshed")
    except Exception as e:
        log.warning("Reload caca DB/cache failed | err=%r",e)
    try:
        from utils import caca_memory
        await caca_memory.init()
        log.info("Reload caca memory refreshed")
    except Exception as e:
        log.warning("Reload caca memory failed | err=%r",e)

async def _refresh_gemini_memory_cache():
    try:
        from utils import gemini_memory
        await gemini_memory.init()
        await gemini_memory.cleanup()
        log.info("Reload Gemini memory refreshed")
    except Exception as e:
        log.warning("Reload Gemini memory failed | err=%r",e)
    try:
        from utils import groq_memory
        await groq_memory.init()
        await groq_memory.cleanup()
        log.info("Reload Groq memory refreshed")
    except Exception as e:
        log.warning("Reload Groq memory failed | err=%r",e)

async def _refresh_runtime_caches(app):
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except Exception:
        pass
    try:
        from handlers import welcome
        welcome.init_welcome_db()
        welcome.WELCOME_ENABLED_CHATS=welcome.load_welcome_chats()
        welcome.VERIFIED_USERS=welcome.load_verified()
    except Exception as e:
        log.warning("Reload welcome cache failed | err=%r",e)
    try:
        from database.blacklist_db import init as blacklist_init
        blacklist_init()
    except Exception as e:
        log.warning("Reload blacklist db failed | err=%r",e)
    try:
        from handlers.asupan import load_asupan_groups,load_autodel_groups
        load_asupan_groups()
        load_autodel_groups()
    except Exception as e:
        log.warning("Reload asupan cache failed | err=%r",e)
    try:
        from handlers.nsfw import nsfw_db_init
        nsfw_db_init()
    except Exception as e:
        log.warning("Reload nsfw db failed | err=%r",e)
    try:
        from database import premium
        premium.init()
    except Exception as e:
        log.warning("Reload premium cache failed | err=%r",e)
    await _refresh_caca_cache()
    await _refresh_gemini_memory_cache()
    try:
        from rag.loader import load_local_contexts
        contexts=load_local_contexts()
        for mod_name in ("handlers.gemini","handlers.groq"):
            mod=sys.modules.get(mod_name)
            if mod and hasattr(mod,"LOCAL_CONTEXTS"):
                setattr(mod,"LOCAL_CONTEXTS",contexts)
    except Exception as e:
        log.warning("Reload rag contexts failed | err=%r",e)

async def hot_reload(app):
    lock=app.bot_data.setdefault("_reload_lock",asyncio.Lock())
    if lock.locked():
        raise RuntimeError("Reload already running")
    async with lock:
        snapshot=_snapshot_handlers(app)
        try:
            await _pre_reload_cleanup(app)
            reloaded=_reload_modules()
            _register_all(app)
            await _refresh_runtime_caches(app)
            handler_count=sum(len(v) for v in app.handlers.values())
            log.info("Reload complete | modules=%s handlers=%s",len(reloaded),handler_count)
            return {"modules":len(reloaded),"handlers":handler_count}
        except Exception:
            _restore_handlers(app,snapshot)
            log.exception("Hot reload failed")
            raise

def reload_summary_text(result:dict)->str:
    return (
        "<b>Reload complete</b>\n\n"
        f"Modules: <code>{result.get('modules',0)}</code>\n"
        f"Handlers: <code>{result.get('handlers',0)}</code>\n\n"
        "<i>Handlers, utils, database, AI memory, and RAG contexts refreshed.</i>"
    )

async def reload_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user
    msg=update.effective_message
    if not user or user.id not in OWNER_ID or not msg:
        return
    status=await msg.reply_text("<b>Reloading modules...</b>",parse_mode="HTML")
    try:
        result=await hot_reload(context.application)
        await status.edit_text(reload_summary_text(result),parse_mode="HTML")
    except Exception as e:
        err=html.escape("".join(traceback.format_exception_only(type(e),e)).strip())[:3000]
        await status.edit_text(f"<b>Reload failed</b>\n\n<code>{err}</code>\n\n<i>Old handlers restored.</i>",parse_mode="HTML")