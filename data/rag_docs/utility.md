# Utility, Jaringan & Alat Bantu

Kumpulan perintah utilitas, jaringan, pencarian, dan pengolahan gambar yang tersedia di KiyoshiBot.

## VPS Cloud

Bot menyediakan penjualan VPS instan (AWS EC2) yang aktif otomatis setelah pembayaran QRIS diterima. Semua paket punya garansi penuh 7 hari sejak aktivasi.

- `/vps`: Buka menu paket VPS untuk membeli server baru.
- `/myvps`: Lihat daftar dan detail server VPS milikmu.

Detail layanan:
- Paket tersedia: 1 GB (30 GB SSD), 2 GB (60 GB SSD), 4 GB (100 GB SSD), dan 8 GB RAM (160 GB SSD) — semua 2 vCPU.
- Durasi: 30 hari, 14 hari (2 minggu), atau 7 hari per pembelian, bisa diperpanjang.
- Region: user bebas memilih region datacenter AWS yang tersedia (tersedia 29 region seperti Singapura, Jakarta, Tokyo, Virginia, Frankfurt, dll).
- Sistem operasi: Ubuntu 24.04 LTS, Ubuntu 22.04 LTS, atau Debian 12.
- Akses: login SSH sebagai `root` memakai password (bukan file PEM). PORT 22.
- Password dan IP dikirim ke chat setelah server aktif, sekitar 30 sampai 60 detik setelah pembayaran.
- Pembayaran memakai QRIS dinamis (GoPay, OVO, DANA, ShopeePay, semua bank). Server dibuat otomatis setelah pembayaran terdeteksi.
- Garansi 7 hari: kalau server bermasalah (port SSH tidak merespons), tekan tombol Klaim Garansi untuk membuat server pengganti otomatis.
- Server otomatis dihentikan saat masa aktif habis.

## Jaringan & Informasi Server
- `/ping`: Cek latensi dan respon bot ke Telegram dalam milidetik (ms).

- `/ip <ip/domain>`: Cek detail lokasi geolokasi, ASN, organisasi, dan ISP alamat IP.
- `/net`: Informasi koneksi jaringan all-in-one dari server bot.
- `/domain <domain>`: Cek status IP dan informasi domain.
- `/whoisdomain <domain>`: Lookup WHOIS domain lengkap (registrar, tanggal daftar, expired, nameserver).
- `/speedtest`: Uji kecepatan koneksi internet server bot (download, upload, ping).

## Alat Bantu Harian & Konversi
- `/weather <kota>`: Cek prakiraan cuaca terkini di kota atau wilayah tertentu.
- `/kurs`: Cek kurs mata uang asing terhadap Rupiah (USD, EUR, SGD, JPY, MYR, dll).
- `/firmware <codename/device>`: Cari dan unduh firmware resmi Xiaomi, Redmi, dan POCO dari XM Firmware Updater (300+ perangkat, semua versi dari berbagai region Global/EEA/India/China, tombol download langsung ke CDN GitHub).
- `/resi <no_resi>`: Lacak status paket pengiriman ekspedisi Indonesia (JNE, J&T, SiCepat, Shopee Xpress, Pos Indonesia, Anteraja, TIKI, Wahana, Ninja, Lion Parcel).
- `/tr <kode_bahasa> <teks>`: Terjemahkan teks ke bahasa yang diinginkan (contoh: `/tr id hello` atau reply pesan dengan `/tr id`).
- `/trlist`: Menampilkan daftar kode bahasa yang didukung penerjemah.
- `/gsearch <query>`: Lakukan pencarian cepat di Google Search langsung dari Telegram.

## Gambar, Sticker & Quotly
- `/upscale`: Tingkatkan resolusi dan ketajaman gambar dengan AI (kirim/reply foto dengan `/upscale`).
- `/nobg`: Hapus latar belakang foto secara otomatis (kirim/reply foto dengan `/nobg`).
- `/aidetect`: Deteksi apakah suatu gambar dibuat oleh generator AI (reply foto).
- `/aitext <teks>`: Deteksi probabilitas apakah sebuah teks ditulis oleh AI atau manusia.
- `/q [N] [r] [warna]`: Buat quote sticker Quotly dari pesan chat. Mendukung jumlah `1` sampai `10` pesan ke depan, `-N` (pesan ke belakang), reply context `r`, dan warna latar/hex/gradasi.
- `/qi [N] [r] [warna]`: Buat quote dalam format gambar wallpaper PNG resolusi tinggi.
- `/qs [N] [r] [warna]`: Buat quote dalam format 9:16 untuk story Instagram/WhatsApp.
- `/kang`: Tambahkan sticker orang lain ke sticker pack pribadi milik Anda di bot (reply sticker).
- `/getsticker`: Ekstrak sticker Telegram menjadi file PNG atau animasi WEBM (reply sticker).
- `/waifu`: Kirimkan gambar waifu anime acak berkualitas tinggi.

## Musik & Anime
- `/music <judul>`: Cari dan unduh lagu MP3 lengkap dengan judul, artis, dan cover art album.
- `/anime <judul>`: Cari informasi anime lengkap dari MyAnimeList (sinopsis, skor, status, episode, studio).
- `/quoteanime`: Dapatkan kutipan kata-kata bijak acak dari karakter anime populer.

## Lain-Lain
- `/reminder <waktu> <pesan>`: Pasang pengingat otomatis (contoh: `/reminder 10m makan siang`, `/reminder 1h rapat`).
- `/ship`: Pasangkan dua anggota grup secara acak untuk jodoh seru-seruan.
- `/share`: Bagikan media secara anonim via tautan rahasia PM deep-link (reply media dengan `/share`).
- `/donate`: Info donasi sukarela untuk mendukung kelangsungan server bot.

## Batalkan Input (/cancel)

- `/cancel`: membatalkan input bertahap yang sedang menunggu teks dari user, misalnya port firewall VPS atau kredensial akun AWS.
