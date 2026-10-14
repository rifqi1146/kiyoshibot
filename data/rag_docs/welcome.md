# Fitur Welcome & Verifikasi Member Baru

Fitur pesan sambutan (welcome message) dan sistem verifikasi anti-spam otomatis untuk melindungi grup dari serangan user bot/spammer.

## Perintah Welcome (/wlc)
- `/wlc enable`: Aktifkan pesan sambutan member baru di grup.
- `/wlc disable`: Matikan pesan sambutan di grup.
- `/wlc status`: Cek status aktif atau tidaknya fitur welcome di grup saat ini.

## Alur Verifikasi & Keamanan Anti-Spam
- Saat member baru bergabung ke grup, bot otomatis mengirim pesan sambutan dengan tombol verifikasi menuju Private Message (PM) bot.
- Di PM bot, member baru wajib menyelesaikan verifikasi CAPTCHA (tersedia metode Cloudflare Turnstile dan hCaptcha) dalam batas waktu 5 menit.
- Jika verifikasi berhasil, member mendapatkan akses chat penuh di grup.
- Jika member tidak menyelesaikan verifikasi dalam waktu 5 menit, bot akan secara otomatis mengeluarkan (kick sementara 45 detik) member tersebut untuk mencegah bot spam.
- Bot juga terhubung langsung dengan sistem pertahanan database global CAS (Combatting Auto Spam) untuk mendeteksi akun spammer yang sudah terdata secara global.
