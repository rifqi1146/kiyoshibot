from telegram.ext import CallbackQueryHandler

from handlers.help import help_callback
from handlers.gsearch import gsearch_callback
from handlers.dl.router import dl_callback, dlask_callback, dlres_callback, dlengine_callback, tiktok_slideshow_callback
from handlers.asupan import asupan_callback
from handlers.helpowner import helpowner_callback
from handlers.reminder import reminder_cancel_cb
from handlers.waifu import waifu_next_cb, waifu_pref_cb
from handlers.music import music_callback
from handlers.quiz import quiz_callback
from handlers.broadcast import broadcast_callback
from handlers.setting import setting_callback
from handlers.manga import manga_callback
from handlers.blacklist import blacklist_callback_gate

def register_callbacks(app):
    app.add_handler(
        CallbackQueryHandler(blacklist_callback_gate, pattern=r".*"),
        group=-99,
    )
    app.add_handler(CallbackQueryHandler(help_callback, pattern=r"^help:"))
    app.add_handler(CallbackQueryHandler(gsearch_callback, pattern=r"^gsearch:"))
    app.add_handler(CallbackQueryHandler(dlask_callback, pattern=r"^dlask:"))
    app.add_handler(CallbackQueryHandler(dlres_callback, pattern=r"^dlres:"))
    app.add_handler(CallbackQueryHandler(tiktok_slideshow_callback, pattern=r"^ttslide:"))
    app.add_handler(CallbackQueryHandler(dl_callback, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(asupan_callback, pattern=r"^asupan:"))
    app.add_handler(CallbackQueryHandler(helpowner_callback, pattern=r"^helpowner:"))
    app.add_handler(CallbackQueryHandler(reminder_cancel_cb, pattern=r"^reminder:"))
    app.add_handler(CallbackQueryHandler(waifu_next_cb, pattern=r"^waifu:-?\d+:\d+:next$"))
    app.add_handler(CallbackQueryHandler(waifu_pref_cb, pattern=r"^waifu:-?\d+:\d+:pref$"))
    app.add_handler(CallbackQueryHandler(music_callback, pattern=r"^music_(download|page|cancel):"))
    app.add_handler(CallbackQueryHandler(quiz_callback, pattern=r"^quizans:"))
    app.add_handler(CallbackQueryHandler(broadcast_callback, pattern=r"^broadcast:"))
    app.add_handler(CallbackQueryHandler(setting_callback, pattern=r"^setting:"))
    app.add_handler(CallbackQueryHandler(manga_callback, pattern="^(readmanga_|switchch_|nav_|msearch_|detailmanga_|ignore|close_manga|nhsearch_|nhdetail_|nhread_|nhnav_|maiddet_|maidread_|maidnav_)"))
    app.add_handler(CallbackQueryHandler(dlengine_callback, pattern=r"^dlengine:"))
    
    