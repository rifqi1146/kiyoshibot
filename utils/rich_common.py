"""Util bersama untuk pengiriman Rich Message (Bot API 10.3).

`is_message_not_modified(error)` — deteksi error Telegram
`Bad Request: message is not modified: specified new message content and reply
markup are exactly the same as a current content and reply markup of the message`.

Kenapa penting: semua helper `edit_rich`/`edit_search_rich` punya fallback ke
HTML biasa untuk server Bot API lama. Kalau error "not modified" (yang artinya
pesan SUDAH benar, cuma kontennya identik) ikut memicu fallback, pesan rich yang
sudah benar malah DIGANTI jadi plain HTML — rich message "hilang" padahal user
cuma menekan tombol yang menghasilkan layar sama (mis. `Menu` dua kali,
`back`/`nav` ke halaman yang sama). Error itu harus dianggap no-op sukses.
"""


def is_message_not_modified(error) -> bool:
    text = str(error or "").lower()
    return "not modified" in text
