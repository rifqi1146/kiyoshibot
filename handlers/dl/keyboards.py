from telegram import InlineKeyboardMarkup, InlineKeyboardButton


def dl_keyboard(dl_id: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Video", callback_data=f"dl:{dl_id}:video"),
                InlineKeyboardButton("MP3", callback_data=f"dl:{dl_id}:mp3"),
            ],
            [InlineKeyboardButton("Cancel", callback_data=f"dl:{dl_id}:cancel")],
        ]
    )


def yt_engine_keyboard(dl_id: str):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Use yt-dlp", callback_data=f"dlengine:{dl_id}:ytdlp")],
            [InlineKeyboardButton("Cancel", callback_data=f"dl:{dl_id}:cancel")],
        ]
    )


def res_keyboard(dl_id: str, res_list: list[dict]):
    rows = []
    for r in res_list:
        h = int(r.get("height") or 0)
        if not h:
            continue
        rows.append([InlineKeyboardButton(f"{h}p", callback_data=f"dlres:{dl_id}:{h}")])

    rows.append([InlineKeyboardButton("Cancel", callback_data=f"dl:{dl_id}:cancel")])
    return InlineKeyboardMarkup(rows)


def autodl_detect_keyboard(dl_id: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Download", callback_data=f"dlask:{dl_id}:go"),
                InlineKeyboardButton("Close", callback_data=f"dlask:{dl_id}:close"),
            ]
        ]
    )
    
def tiktok_slideshow_keyboard(dl_id:str,has_audio:bool=True):
    rows=[
        [
            InlineKeyboardButton("Images",callback_data=f"ttslide:{dl_id}:images"),
            InlineKeyboardButton("Video",callback_data=f"ttslide:{dl_id}:video"),
        ]
    ]
    if has_audio:
        rows.append([InlineKeyboardButton("Audio",callback_data=f"ttslide:{dl_id}:audio")])
    rows.append([InlineKeyboardButton("Cancel",callback_data=f"ttslide:{dl_id}:cancel")])
    return InlineKeyboardMarkup(rows)