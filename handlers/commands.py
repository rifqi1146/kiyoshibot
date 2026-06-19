from telegram.ext import CommandHandler

from handlers.anime import anime_cmd
from handlers.backup import backup_cmd, restore_cmd, autobackup_cmd
from handlers.blacklist import blacklist_cmd
from handlers.broadcast import broadcast_cmd
from handlers.cookies import cookies_cmd
from handlers.dl.router import dl_cmd, autodl_cmd
from handlers.donate import donate_cmd
from handlers.fasttelethon import fasttelethon_cmd
from handlers.gemini import ai_cmd
from handlers.getsticker import getsticker_cmd
from handlers.groq import groq_query
from handlers.groups import groups_cmd
from handlers.gsearch import gsearch_cmd
from handlers.help import help_cmd
from handlers.helpowner import helpowner_cmd
from handlers.kang import kang_cmd
from handlers.kurs import kurs_cmd
from handlers.manga import manga_cmd
from handlers.aitext import aitext_cmd
from handlers.music import music_cmd
from handlers.networking import whoisdomain_cmd, ip_cmd, domain_cmd, net_cmd
from handlers.nobg import nobg_cmd
from handlers.nsfw import nsfw_cmd
from handlers.susunkata import susunkata_cmd
from handlers.ping import ping_cmd
from handlers.dl.tiktok.livetiktok import recordlive_cmd, stoprecord_cmd, statusrecord_cmd
from handlers.premium import premium_cmd
from handlers.quiz import quiz_cmd
from handlers.quoteanime import quoteanime_cmd
from handlers.quotly import q_cmd
from handlers.reload import reload_cmd
from handlers.reminder import reminder_cmd
from handlers.resi import resi_cmd
from handlers.restart import restart_cmd
from handlers.setting import setting_cmd
from handlers.ship import ship_cmd
from handlers.speedtest import speedtest_cmd
from handlers.start import start_cmd
from handlers.stats import stats_cmd
from handlers.translate import tr_cmd, trlist_cmd
from handlers.update import update_cmd
from handlers.upscale import upscale_cmd
from handlers.waifu import waifu_cmd
from handlers.weather import weather_cmd
from handlers.welcome import wlc_cmd, start_verify_pm
from handlers.aiimagedetector import aiimagedetector_cmd
from handlers.uploadengine import uploadengine_cmd

from handlers.asupan import (
    asupan_cmd,
    asupann_cmd,
    autodel_cmd,
)

from handlers.caca import (
    cacaa_cmd,
    meta_query,
    mode_cmd,
)

from handlers.moderation import (
    addsudo_cmd,
    ban_cmd,
    demote_cmd,
    kick_cmd,
    moderation_cmd,
    mute_cmd,
    promote_cmd,
    rmsudo_cmd,
    sudolist_cmd,
    tag_cmd,
    unban_cmd,
    unmute_cmd,
    untag_cmd,
)

COMMAND_HANDLERS = [
    ("addsudo", addsudo_cmd, False),
    ("aitext", aitext_cmd, False),
    ("anime", anime_cmd, False),
    ("aidetect", aiimagedetector_cmd, False),
    ("ask", ai_cmd, False),
    ("asupan", asupan_cmd, False),
    ("asupann", asupann_cmd, False),
    ("autobackup", autobackup_cmd, True),
    ("autodel", autodel_cmd, True),
    ("autodl", autodl_cmd, False),
    ("backup", backup_cmd, True),
    ("ban", ban_cmd, False),
    ("blacklist", blacklist_cmd, False),
    ("broadcast", broadcast_cmd, False),
    ("caca", meta_query, False),
    ("cacaa", cacaa_cmd, False),
    ("cookies", cookies_cmd, False),
    ("demote", demote_cmd, False),
    ("dl", dl_cmd, False),
    ("domain", domain_cmd, True),
    ("donate", donate_cmd, False),
    ("fasttelethon", fasttelethon_cmd, False),
    ("getsticker", getsticker_cmd, False),
    ("groq", groq_query, False),
    ("groups", groups_cmd, False),
    ("gsearch", gsearch_cmd, False),
    ("help", help_cmd, True),
    ("helpowner", helpowner_cmd, True),
    ("ip", ip_cmd, True),
    ("kang", kang_cmd, False),
    ("kick", kick_cmd, False),
    ("kurs", kurs_cmd, False),
    ("manga", manga_cmd, False),
    ("menu", help_cmd, True),
    ("mode", mode_cmd, False),
    ("moderation", moderation_cmd, False),
    ("music", music_cmd, False),
    ("mute", mute_cmd, False),
    ("net", net_cmd, False),
    ("nobg", nobg_cmd, False),
    ("nsfw", nsfw_cmd, False),
    ("ping", ping_cmd, True),
    ("premium", premium_cmd, False),
    ("promote", promote_cmd, False),
    ("q", q_cmd, False),
    ("quiz", quiz_cmd, False),
    ("quoteanime", quoteanime_cmd, False),
    ("reload", reload_cmd, False),
    ("reminder", reminder_cmd, False),
    ("restart", restart_cmd, False),
    ("reboot", restart_cmd, False),
    ("restore", restore_cmd, True),
    ("resi", resi_cmd, False),
    ("rmsudo", rmsudo_cmd, False),
    ("susunkata", susunkata_cmd, False),
    ("settings", setting_cmd, False),
    ("ship", ship_cmd, True),
    ("speedtest", speedtest_cmd, False),
    ("start", start_cmd, True),
    ("start", start_verify_pm, False),
    ("stats", stats_cmd, False),
    ("sudolist", sudolist_cmd, False),
    ("tag", tag_cmd, False),
    ("tr", tr_cmd, True),
    ("trlist", trlist_cmd, True),
    ("unban", unban_cmd, False),
    ("uploadengine", uploadengine_cmd, False),
    ("unmute", unmute_cmd, False),
    ("untag", untag_cmd, False),
    ("upscale", upscale_cmd, False),
    ("update", update_cmd, False),
    ("waifu", waifu_cmd, False),
    ("weather", weather_cmd, False),
    ("whoisdomain", whoisdomain_cmd, True),
    ("recordlive", recordlive_cmd, False),
    ("stoprecord", stoprecord_cmd, False),
    ("statusrecord", statusrecord_cmd, False),
]

def register_commands(app):
    for name, handler, blocking in COMMAND_HANDLERS:
        app.add_handler(
            CommandHandler(name, handler, block=blocking),
            group=-1
        )