"""Command `/firmware` — Cari dan download official Xiaomi/Redmi/POCO firmware dari XM Firmware Updater.

FLOW
----
1. `/firmware` tanpa argumen:
   -> Menampilkan Xiaomi Firmware Explorer Menu (Bot API 10.3 Rich Message)
      berisi daftar perangkat populer (POCO F4, F3, X3, X6 Pro, dll) & tombol Search.
2. `/firmware <query>`:
   -> Cocokkan query ke 300+ database perangkat Xiaomi.
   -> Jika query ambigu (misal "poco f4"): tampilkan pilihan perangkat.
   -> Jika cocok persis (misal "munch"): langsung muat seluruh versi firmware.
3. Pagination & Tampilan:
   -> 20 item firmware per halaman, maksimal 200 data riwayat rilis.
   -> Filter per-region (Global, Europe EEA, India, China, dll).
   -> Tombol inline langsung mengarah ke CDN GitHub Release (Fastly/Azure) tanpa safelink.
"""

from .commands import firmware_cmd
from .callbacks import firmware_callback

__all__ = ["firmware_cmd", "firmware_callback"]
