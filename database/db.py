import sqlite3
import os
import logging
import threading
from contextlib import contextmanager
from typing import Generator

log = logging.getLogger(__name__)

# ── Thread-local connection pool ────────────────────────────────────────────
# Sebelumnya SETIAP operasi membuka koneksi baru (open fd + 3 PRAGMA) lalu
# menutupnya. Di hot path (user_settings per pesan auto-detect, collector per
# pesan grup) itu overhead besar.
#
# Sekarang koneksi di-cache per (thread, db_path). Karena banyak pemanggil
# memanggil `con.close()` manual, koneksi dibungkus class yang `.close()`-nya
# di-override menjadi: rollback transaksi menggantung (menjaga semantik lama
# di mana close menggugurkan transaksi yang belum di-commit) + recycle koneksi
# kembali ke pool, BUKAN menutup fd.
#
# sqlite3 default check_same_thread=True aman: koneksi hanya pernah dipakai
# oleh thread yang membuatnya (key pool = thread-local).

_local = threading.local()


def _get_thread_pool() -> dict[str, sqlite3.Connection]:
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = {}
        _local.pool = pool
    return pool


class _PooledConnection(sqlite3.Connection):
    """Connection yang `close()`-nya mendaur ulang, bukan menutup.

    Menjaga kompatibilitas dengan semua pemanggil lama yang memanggil
    `con.close()` secara manual. Transaksi yang belum di-commit di-rollback
    supaya pemakai berikutnya pada thread yang sama tidak mewarisi state kotor
    (persis perilaku close() sqlite3 yang asli).
    """

    def close(self):  # type: ignore[override]
        try:
            if self.in_transaction:
                self.rollback()
        except sqlite3.Error:
            pass
        # Reset row_factory supaya tidak "bocor" ke pemakai berikutnya pada
        # thread yang sama (beberapa modul men-set row_factory=sqlite3.Row).
        try:
            self.row_factory = None
        except Exception:
            pass
        # Jangan tutup fd — koneksi tetap hidup di thread-local pool.


def _open_pooled(db_path: str) -> sqlite3.Connection:
    pool = _get_thread_pool()
    con = pool.get(db_path)
    if con is not None:
        try:
            con.execute("SELECT 1")
            return con
        except sqlite3.Error:
            # Koneksi mati (mis. file dihapus/dipindah) — buang dan buka ulang.
            pool.pop(db_path, None)
            try:
                sqlite3.Connection.close(con)
            except sqlite3.Error:
                pass
    con = sqlite3.connect(db_path, timeout=30.0, factory=_PooledConnection)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=30000;")
    except sqlite3.Error as e:
        log.warning(f"Failed to set PRAGMA for {db_path}: {e}")
    pool[db_path] = con
    return con


def get_connection(db_path: str) -> sqlite3.Connection:
    """
    Returns a pooled connection to the SQLite database.
    `.close()` pada koneksi ini mendaur ulang koneksi (rollback + kembali ke
    pool), bukan menutupnya, sehingga pemanggilan berikutnya pada thread yang
    sama tidak membayar biaya open + PRAGMA lagi.
    Ensures the directory for db_path exists.
    """
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    return _open_pooled(db_path)


@contextmanager
def db_session(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager for database connections.
    Connection is recycled (not closed) at the end.
    """
    con = get_connection(db_path)
    try:
        yield con
    finally:
        # Rollback transaksi menggantung (jika ada) & recycle koneksi.
        con.close()
