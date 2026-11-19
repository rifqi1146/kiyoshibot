# Media Downloader & Video Scrapers

KiyoshiBot menyediakan fitur unduh video, audio, gambar, dan galeri otomatis dari banyak platform tanpa watermark dan tanpa iklan.

## Perintah Utama Downloader
- `/dl <url>`: Download media dari link yang dikirim (bisa pilih resolusi & format video/MP3).
- `/dl <url> --details`: Unduh media langsung beserta metadata lengkap postingan (TikTok, Instagram, X/Twitter, Reddit, Pinterest, Threads) sebagai caption Rich Message dalam satu pesan.
- Auto-Download: Jika fitur `/autodl enable` aktif di grup, cukup kirim link langsung tanpa mengetik `/dl` (tambahkan `--details` pada link TikTok, Instagram, X, Reddit, Pinterest, atau Threads untuk mengirim media beserta metadata lengkap).
- Batch Download: Kirim 2-5 link sekaligus dalam satu pesan untuk download otomatis sebagai satu album.
- `/asupan`: Kirim video acak FYP TikTok (khusus chat/grup yang mengaktifkan `/asupann enable`).

## Flag --details (Media + Metadata Lengkap)
- Gunakan `/dl <url> --details` atau kirim `<url> --details` langsung di chat/grup dengan Auto-Download aktif.
- Platform yang didukung: **TikTok** (kecuali Douyin), **Instagram**, **X / Twitter**, **Reddit**, **Pinterest**, dan **Threads**.
- Bot mengunduh media langsung lewat scraper internal dan mengirimkannya bersama metadata lengkap postingan (statistik engagement, info kreator/author, spesifikasi video/stream, audio, dan pengaturan post) dalam **satu Rich Message**.
- Postingan teks saja (tanpa media) tetap menampilkan laporan metadata lengkap tanpa mengunduh file.
- Tetap menghormati gate konten dewasa (`adult_gate` pada X/Reddit), `/spoiler enable`, dan mode **Silent Download** (hanya memberi reaksi `😍` tanpa pesan status teks).

## Platform Sosial Media
- Instagram: Download Reels, Post, Carousel (multi-slide paralel), dan Story lewat `/igstory <username>`, plus info profil lewat `/igstalk <username>`.
- TikTok: Download video tanpa watermark, slideshow multi-foto, dan audio MP3. Link Douyin (`douyin.com`) juga didukung.
- YouTube: Download Video (MP4) dan Audio (MP3), termasuk YouTube Shorts, YouTube Music, dan post komunitas.
- Twitter / X, Facebook, Threads, Reddit, Pinterest: Download video, foto, dan galeri langsung. Threads mendukung multi-media.
- CapCut: Download template video CapCut.
- Bunkr dan Pawchive: Download file video/album dari link file-sharing.

## Gate Konten Dewasa Otomatis (X/Twitter & Reddit)
- Media yang ditandai sensitif/dewasa (NSFW) oleh platform di Twitter/X dan Reddit dibatasi hanya untuk pengguna Premium.
- Deteksi otomatis dari flag resmi platform (`possibly_sensitive`, `ext_sensitive_media_warning`, `over_18`).
- Konten normal (non-dewasa) tetap bisa didownload GRATIS oleh semua pengguna, termasuk user biasa.
- Pengguna premium di grup wajib mengaktifkan `/nsfw enable`; di chat privat otomatis diizinkan.
- Media sensitif di grup yang mengaktifkan `/spoiler enable` otomatis dikirim dengan blur/spoiler Telegram.

## Gate Domain Premium (NSFW Premium)
Selain konten dewasa otomatis di sosmed, ada juga domain khusus dewasa yang hanya bisa diakses pengguna Premium (dan wajib NSFW aktif di grup). Daftar domain lengkap bisa dilihat lewat `/premiumbenefit`.

## Downloader Situs Dewasa (Premium + NSFW)
- Domain seperti PornHub, XVideos, XNXX, Nekopoi, Simontok, PunishWorld, CosXplay, Becekku, LendirQu, Bunkr, DarknessPorn, FemdomVC, BDSMLust, AsianGirl, Heavy-R, Cosplaytele, Pawchive hanya untuk pengguna Premium.

## Downloader Asia & Moviebox (Premium tanpa NSFW)
- `/drakorid`: Browse dan download Drama Korea, China, dan Film Asia dari situs Drakor.id.
- `/anichin`: Browse dan download Donghua (animasi China) sub Indo dari Anichin.
- `/moviebox`: Browse dan download Film dan TV Series internasional dari MovieBox (ribuan judul, subtitle tersedia).
- Domain `drakorid.co`, `anichin.moe`, `officialmoviebox.com`, `moviebox.ph`, `moviebox.co` bisa diakses pengguna premium tanpa syarat NSFW grup.

## Pengaturan Pribadi Pengguna (/settings)
- `/settings`: Buka menu interaktif untuk mengatur preferensi download pribadi Anda (berlaku di semua chat/grup, tersimpan permanen per akun pengguna).
- Pilihan pengaturan yang tersedia di `/settings`:
  - **AutoDL All Groups**: Aktifkan/nonaktifkan auto-download untuk link yang Anda kirim di SEMUA grup (bahkan di grup yang belum mengaktifkan `/autodl`).
  - **Downloader Format**: Format unduh default saat mendeteksi link — `Ask` (tanya dulu setiap link), `Video` (langsung unduh video), atau `MP3` (langsung ekstrak audio).
  - **YouTube Resolution**: Kualitas default video YouTube — `Ask (Default)` (pilih manual), `360p`, `480p`, `720p`, atau `1080p ⭐️` (otomatis ambil resolusi tersebut tanpa menampilkan picker).
  - **Music Output Format**: Format audio saat mengunduh lagu via `/music` — `FLAC` (kualitas tinggi lossless) atau `MP3`.
  - **TikTok Slideshow**: Pilihan hasil untuk konten slideshow foto TikTok — `Ask` (tanya format), `Images` (kirim album foto), `Video` (jadikan video slideshow bergerak), atau `Audio Only` (lagunya saja).
  - **Silent Download**: Mode senyap saat mendownload media (termasuk saat memakai `--details`). Jika aktif (ON), bot hanya memberi reaksi `😍` pada pesan tanpa mengirim atau mengedit pesan status progres — hasil langsung dikirim begitu selesai.

## Tips Penggunaan
- Semua downloader otomatis mendukung pilih resolusi (kualitas rendah sampai tinggi sesuai sumber) dan opsi ekstrak audio MP3.
- Unduhan besar (>2GB) otomatis ditolak demi stabilitas server dan limit Telegram.
- Semua result dikirim langsung ke chat; jika di grup ada fitur spoiler, media premium otomatis dispoiler.
