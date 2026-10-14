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
- `/ban [durasi] <user_id|@username|reply> [alasan]`: Ban anggota. Tanpa durasi = permanen. Durasi didukung seperti `7d` (hari), `1h` (jam), `10m` (menit). Contoh: `/ban 7d @user toxic`, atau reply pesan lalu ketik `/ban 7d toxic`.
- `/unban <user_id|@username|reply>`: Lepas ban anggota.
- `/mute [durasi] <user_id|@username|reply> [alasan]`: Bisukan anggota, bisa sementara atau permanen. Contoh: `/mute 10m @user spam`.
- `/unmute <user_id|@username|reply>`: Buka bisu anggota.
- `/kick <user_id|@username|reply> [alasan]`: Tendang anggota dari grup. Bot otomatis meng-unban setelah 45 detik, jadi sifatnya sementara.
- `/promote <user_id|@username|reply> [gelar]`: Jadikan anggota sebagai admin, boleh sekaligus memberi gelar custom (mis. `/promote @user Mod Tampan`).
- `/demote <user_id|@username|reply>`: Cabut status admin anggota.
- `/tag`: Tag semua anggota grup sekaligus (mention massal).
- `/untag`: Hapus pesan tag massal terakhir.
- Catatan: semua aksi moderasi bisa menargetkan user lewat reply pesan (tidak perlu ketik ID/username), dan alasan bersifat opsional.

## Moderasi Otomatis (/moderation)
- `/moderation enable|disable|status`: Aktifkan/matikan moderasi otomatis di grup — bot menindak spam, link, dan pelanggaran lain secara otomatis.
- Hanya admin grup atau owner bot yang dapat mengubahnya.

## Blacklist Bot (/blacklist)
Khusus owner bot. Memblokir user atau grup dari penggunaan bot.
- `/blacklist add <user_id|@username> [alasan]`: Masukkan user ke daftar hitam.
- `/blacklist remove <user_id|@username>`: Hapus user dari daftar hitam.
- `/blacklist status <user_id|@username>`: Cek status blacklist user.
- `/blacklist list`: Tampilkan seluruh user yang di-blacklist.
- `/blacklist group add [group_id|@username] [alasan]`: Blacklist sebuah grup sehingga bot berhenti melayaninya. Di dalam grup, `group_id` boleh dikosongkan untuk menargetkan grup saat ini.
- `/blacklist group remove [group_id|@username]`: Hapus grup dari daftar hitam.
- `/blacklist group status [group_id|@username]`: Cek status blacklist grup.
- `/blacklist group list`: Tampilkan seluruh grup yang di-blacklist.

## Pengaturan Pengguna (User Settings)
- `/settings`: Menu pengaturan pribadi per akun (berlaku di semua chat/grup). Berisi 6 opsi:
  - **AutoDL All Groups**: auto-download link di semua grup.
  - **Downloader Format**: format default `Ask` / `Video` / `MP3`.
  - **YouTube Resolution**: `Ask (Default)` / `360p` / `480p` / `720p` / `1080p`.
  - **Music Format**: `FLAC` atau `MP3`.
  - **TikTok Slideshow**: `Ask` / `Images` / `Video` / `Audio Only`.
  - **Silent Download**: unduh tanpa pesan progres berkala.
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
