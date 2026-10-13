# Fitur Welcome & Verifikasi Member Baru

Pengaturan pesan sambutan (welcome message) dan verifikasi anti-spam member baru di grup:

## Perintah Welcome (/wlc)
- `/wlc enable`: Aktifkan pesan sambutan (welcome) di grup.
- `/wlc disable`: Matikan pesan sambutan di grup.
- `/wlc status`: Cek status aktif pesan welcome saat ini.

## Alur Verifikasi & Keamanan Member Baru
- Saat member baru bergabung, bot mengirim pesan sambutan dengan tombol verifikasi ke Private Message (PM).
- Member baru wajib menyelesaikan verifikasi CAPTCHA (Cloudflare Turnstile) dalam waktu 5 menit.
- Jika tidak verifikasi dalam 5 menit, bot otomatis mengeluarkan (kick sementara 45 detik) member tersebut untuk mencegah bot spam.
- Bot juga mengecek database global CAS (Combatting Auto Spam) secara otomatis.
