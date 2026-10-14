# Pengaturan Grup, Moderasi & Admin

Perintah pengaturan grup dan moderasi. Hanya dapat digunakan oleh Admin grup atau Owner bot.

## Pengaturan Fitur Grup
- `/asupann enable|disable|status`: Aktifkan/matikan fitur kirim asupan otomatis di grup.
- `/autodel enable|disable|status`: Hapus otomatis pesan asupan setelah dikirim (agar grup tidak kotor).
- `/autodl enable|disable|status`: Deteksi dan auto-download setiap link yang dikirim anggota.
- `/cacaa enable|disable|status`: Aktifkan/matikan chatbot Caca untuk merespon semua obrolan di grup.
- `/nsfw enable|disable|status`: Aktifkan/matikan izin konten dewasa dan downloader 18+ di grup.
- `/spoiler enable|disable|status`: Kirim media premium/NSFW dengan efek blur (spoiler) Telegram.
- `/wlc enable|disable|status`: Aktifkan/matikan pesan sambutan (welcome message) untuk member baru.

## Moderasi Anggota
- `/kick`: Tendang anggota dari grup (ban 45 detik lalu otomatis di-unban).
- `/ban` dan `/unban`: Ban permanen atau lepas ban anggota grup.
- `/mute` dan `/unmute`: Bisukan atau buka bisu anggota grup.
- `/promote` dan `/demote`: Jadikan anggota sebagai admin atau cabut status adminnya.
- `/tag`: Tag semua anggota grup sekaligus (mention massal).
- `/untag`: Hapus pesan tag massal terakhir.
- `/moderation`: Buka menu konfigurasi moderasi (aksi otomatis untuk spam, link, dll).
- `/blacklist`: Kelola daftar hitam bot (user/grup yang diblokir dari bot).

## Pengaturan Pengguna (User Settings)
- `/settings`: Menu pengaturan pribadi (mode download silent, format default, dsb).
- `/uploadengine`: Pilih mesin upload file (MTProto / Pyrofork) untuk downloader.
- `/fasttelethon`: Aktifkan/matikan engine upload cepat FastTelethon.

## Command Khusus Owner Bot
Perintah berikut hanya dapat digunakan oleh owner bot (@HirohitoKiyoshi):
- `/broadcast`: Kirim pesan massal ke semua pengguna bot.
- `/backup` dan `/restore`: Buat atau pulihkan backup database bot.
- `/autobackup`: Atur backup otomatis berkala (setiap 6 jam).
- `/reload`: Muat ulang (hot reload) seluruh handler, modul, dan RAG tanpa restart.
- `/restart`: Restart penuh proses bot.
- `/update`: Tarik pembaruan terbaru dari repository Git secara otomatis.
- `/premium`: Kelola status premium user secara manual.
- `/addsudo` dan `/rmsudo`: Tambah atau hapus user dari daftar sudo.
- `/sudolist`: Lihat daftar user sudo aktif.
- `/cookies`: Unggah file cookies (format Netscape) untuk kebutuhan downloader.
- `/groups`: Lihat daftar semua grup tempat bot berada.
- `/helpowner`: Buka menu khusus perintah owner.
