# Fitur AI & Chatbot Assisten

KiyoshiBot dilengkapi beberapa model AI canggih yang bisa diajak ngobrol, tanya jawab, dan dipakai untuk membantu coding atau riset dari dalam Telegram.

## AI Assistant
- `/ask <pertanyaan>`: Tanya AI dengan kemampuan web-search grounding (Firecrawl / Jina) untuk informasi terkini, berita, harga, skor, atau rilis.
- `/groq <pertanyaan>`: Tanya AI dengan respon super cepat via Groq Llama untuk pertanyaan umum.
- Cara lanjut chat: reply pesan terakhir bot (untuk `/ask` dan `/groq`), tidak perlu ketik command lagi.
- Konteks asli: tanggal dan waktu (WIB) selalu disuntik ke AI, jadi bot tahu hari ini tanggal berapa.
- Bot ini 100% BEBAS IKLAN. AI tidak akan pernah menawarkan iklan, sponsor, atau link promo pihak ketiga.

## Caca Chatbot (Roleplay)
- `/caca <teks>`: Ngobrol dengan Caca, chatbot roleplay bergaya manja, playful, dan menyenangkan.
- Caca bisa melihat dan membaca gambar/foto serta stiker statis (WebP). Cukup kirim foto dengan caption `/caca <teks>`, reply foto dengan `/caca <teks>`, atau reply pesan Caca langsung dengan foto/stiker. Stiker bergerak (animasi/video) tidak didukung.
- Caca otomatis mencari info terbaru di web (Firecrawl / Jina) saat pertanyaannya butuh data aktual — misalnya harga, berita, spesifikasi, jadwal, atau rilis. Tidak perlu perintah khusus.
- Caca juga tahu fitur-fitur KiyoshiBot. Kalau ditanya soal fitur bot, Caca menjawab sesuai karakternya sendiri (bukan mode bantuan teknis).
- Cara lanjut ngobrol: reply pesan terakhir Caca, tidak perlu ketik `/caca` lagi.
- Di grup, Caca hanya aktif jika admin sudah mengaktifkannya dengan `/cacaa enable`.
- Caca menggunakan sistem Credit berbasis token pemakaian AI. Top-up credit melalui `/cacatopup` atau `/buycaca`, dan cek saldo melalui `/cacasaldo`.

## Caca & AI Moderation di Grup
- Caca punya tool moderasi nyata di grup: mute, unmute, ban, unban, kick, promote, demote, dan tag anggota.
- Cara pakai: tinggal minta langsung ke Caca, misalnya `/caca mute @user 2 jam`, `/caca buka mute dia`, atau `/caca tendang si spammer`. Tidak perlu ngetik `/mute` atau `/unmute` manual.
- `/ask` juga bisa menjalankan aksi moderasi yang sama di grup saat diminta admin.
- Syarat: `/moderation enable` sudah aktif di grup, pemintanya admin grup atau owner/sudo bot, dan bot punya hak admin yang dibutuhkan.
- Target bisa `@username`, user_id, atau reply pesan anggota. Kalau tidak menyebut siapa, sistem pakai anggota yang di-reply atau target terakhir di grup itu.
- Kalau tool aktif, Caca TIDAK menyuruh user mengetik command slash sendiri — permintaan langsung dieksekusi lalu dikonfirmasi dengan gaya Caca.
- Admin grup, sudo, owner bot, dan bot sendiri tidak bisa di-mute/ban/kick.
- Pembayaran via QRIS otomatis. Setiap top-up memiliki masa berlaku (7-30 hari) per transaksi (multi-lot FEFO).

## Ganti Persona Caca (/mode)
Fitur ini terbuka untuk pengguna yang memiliki saldo credit Caca aktif (atau saat akses Caca gratis sedang aktif). Mengubah kepribadian Caca:
- `/mode` (tanpa argumen): lihat persona yang sedang aktif.
- `/mode default`: santai, ramah, dan playful.
- `/mode toxic`: kasar dan suka menghina.
- `/mode sarkas`: sinis dan menyindir tajam.
- `/mode yandere`: posesif dan obsesif.
- `/mode cabul`: liar dan provokatif.
- `/mode bokep`: vulgar dan nakal.
- `/mode masokis`: patuh, pasrah, dan penurut.
- `/mode slave`: tunduk pada pacar sebagai Master, manja tapi ngeselin.
- `/mode loli`: lucu, imut, bergaya anime.

Catatan: mengganti mode akan mereset riwayat memori percakapan Caca user tersebut.
Modes `cabul`, `bokep`, `masokis`, dan `slave` dijalankan pada model eksplisit (`CACA_MODEL_EXPLICIT`), terpisah dari mode lain, supaya tidak dilunakkan oleh safety model.
