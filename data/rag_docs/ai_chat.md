# Fitur AI & Chatbot Assisten

KiyoshiBot dilengkapi beberapa model AI canggih yang bisa diajak ngobrol, tanya jawab, dan dipakai untuk membantu coding atau riset dari dalam Telegram.

## AI Assistant
- `/ask <pertanyaan>`: Tanya AI berbasis OpenCode dengan kemampuan web-search grounding (Firecrawl / Jina) untuk informasi terkini, berita, harga, skor, atau rilis.
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
- `/mode loli`: lucu, imut, bergaya anime.

Catatan: mengganti mode akan mereset riwayat memori percakapan Caca user tersebut.
