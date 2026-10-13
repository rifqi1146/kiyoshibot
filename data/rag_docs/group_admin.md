# Pengaturan Grup & Moderasi

Perintah admin untuk mengelola grup, berlaku bagi admin grup atau owner bot:

## Pengaturan Fitur Grup
- `/asupann enable|disable|status`: Aktifkan/matikan fitur asupan di grup.
- `/autodel enable|disable|status`: Hapus otomatis pesan asupan setelah dikirim.
- `/autodl enable|disable|status`: Deteksi dan download link otomatis.
- `/cacaa enable|disable|status`: Aktifkan/matikan chatbot Caca di grup.
- `/nsfw enable|disable|status`: Aktifkan konten dewasa di grup.
- `/spoiler enable|disable|status`: Kirim media premium dengan spoiler.
- `/wlc enable|disable`: Aktifkan/matikan pesan sambutan (welcome message) member baru.

## Moderasi Anggota
- `/kick`: Tendang anggota (ban 45 detik lalu di-unban otomatis).
- `/ban`, `/unban`: Ban atau lepas ban anggota grup.
- `/mute`, `/unmute`: Bisukan anggota grup.
- `/promote`, `/demote`: Jadikan atau cabut admin grup.
- `/tag`, `/untag`: Tag semua anggota atau hapus tag.
- `/moderation`: Menu konfigurasi moderasi grup.
- `/blacklist`: Kelola daftar blacklist bot.

## Pengaturan Pengguna
- `/settings`: Menu pengaturan pribadi (mode silent download, format default).
- `/uploadengine`: Pilih mesin upload (MTProto / Pyrofork).
- `/fasttelethon`: Aktifkan/matikan engine upload cepat.

Catatan: Perintah bot owner (seperti `/broadcast`, `/backup`, `/reload`, `/restart`, `/premium`) hanya dapat digunakan oleh owner.
