import time
import logging

from database.db import db_session

ASUPAN_DB_PATH = "data/asupan.sqlite3"

log = logging.getLogger(__name__)


def _asupan_db_init():
    with db_session(ASUPAN_DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS asupan_groups (
                source_file TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                added_at REAL NOT NULL,
                PRIMARY KEY (source_file, chat_id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS asupan_autodel (
                source_file TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (source_file, chat_id)
            )
            """
        )
        con.commit()


def _db_load_enabled(table: str) -> set[int]:
    with db_session(ASUPAN_DB_PATH) as con:
        if table == "asupan_autodel":
            cur = con.execute("SELECT chat_id FROM asupan_autodel WHERE enabled=1")
            rows = cur.fetchall()
            if rows:
                return {int(r[0]) for r in rows if r and r[0] is not None}

            cur = con.execute("SELECT chat_id FROM asupan_autodel")
            rows = cur.fetchall()
            return {int(r[0]) for r in rows if r and r[0] is not None}

        cur = con.execute("SELECT chat_id FROM asupan_groups")
        rows = cur.fetchall()
        return {int(r[0]) for r in rows if r and r[0] is not None}


def _db_set_enabled(table: str, values: set[int]):
    with db_session(ASUPAN_DB_PATH) as con:
        try:
            con.execute("BEGIN")
            now = time.time()
            src = "runtime"

            if table == "asupan_autodel":
                con.execute("UPDATE asupan_autodel SET enabled=0, updated_at=?", (now,))
                if values:
                    con.executemany(
                        """
                        INSERT INTO asupan_autodel (source_file, chat_id, enabled, updated_at)
                        VALUES (?, ?, 1, ?)
                        ON CONFLICT(source_file, chat_id) DO UPDATE SET
                          enabled=1,
                          updated_at=excluded.updated_at
                        """,
                        [(src, int(cid), now) for cid in values],
                    )
            else:
                if values:
                    con.executemany(
                        """
                        INSERT INTO asupan_groups (source_file, chat_id, added_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(source_file, chat_id) DO UPDATE SET
                          added_at=excluded.added_at
                        """,
                        [(src, int(cid), now) for cid in values],
                    )

                con.execute(
                    "DELETE FROM asupan_groups WHERE source_file=? AND chat_id NOT IN (%s)"
                    % (",".join("?" * len(values)) if values else "-1"),
                    (src, *[int(cid) for cid in values]) if values else (src,),
                )

            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception as rollback_err:
                log.warning(
                    "Rollback failed while saving %s to %s: %s",
                    table,
                    ASUPAN_DB_PATH,
                    rollback_err,
                )
            raise


def load_asupan_groups():
    from handlers.asupan import state
    try:
        _asupan_db_init()
        loaded = _db_load_enabled("asupan_groups")
        state.ASUPAN_ENABLED_CHATS = loaded
        log.info("Loaded asupan groups: %s chats", len(loaded))
    except Exception:
        log.exception(
            "Failed to load asupan groups from %s; using empty set",
            ASUPAN_DB_PATH,
        )
        state.ASUPAN_ENABLED_CHATS = set()


def save_asupan_groups():
    from handlers.asupan import state
    try:
        _asupan_db_init()
        _db_set_enabled("asupan_groups", state.ASUPAN_ENABLED_CHATS)
        log.info("Saved asupan groups: %s chats", len(state.ASUPAN_ENABLED_CHATS))
    except Exception:
        log.exception(
            "Failed to save asupan groups to %s; state size=%s",
            ASUPAN_DB_PATH,
            len(state.ASUPAN_ENABLED_CHATS),
        )


def is_asupan_enabled(chat_id: int) -> bool:
    from handlers.asupan import state
    return chat_id in state.ASUPAN_ENABLED_CHATS


def load_autodel_groups():
    from handlers.asupan import state
    try:
        _asupan_db_init()
        loaded = _db_load_enabled("asupan_autodel")
        state.AUTODEL_ENABLED_CHATS = loaded
        log.info("Loaded asupan autodel groups: %s chats", len(loaded))
    except Exception:
        log.exception(
            "Failed to load asupan autodel groups from %s; using empty set",
            ASUPAN_DB_PATH,
        )
        state.AUTODEL_ENABLED_CHATS = set()


def save_autodel_groups():
    from handlers.asupan import state
    try:
        _asupan_db_init()
        _db_set_enabled("asupan_autodel", state.AUTODEL_ENABLED_CHATS)
        log.info("Saved asupan autodel groups: %s chats", len(state.AUTODEL_ENABLED_CHATS))
    except Exception:
        log.exception(
            "Failed to save asupan autodel groups to %s; state size=%s",
            ASUPAN_DB_PATH,
            len(state.AUTODEL_ENABLED_CHATS),
        )


def is_autodel_enabled(chat_id: int) -> bool:
    from handlers.asupan import state
    return chat_id in state.AUTODEL_ENABLED_CHATS


def init_asupan_storage():
    try:
        _asupan_db_init()
        log.info("Initialized asupan storage")
    except Exception:
        log.exception("Failed to initialize asupan storage at %s", ASUPAN_DB_PATH)

    try:
        load_asupan_groups()
    except Exception:
        log.exception("Unexpected failure while loading asupan groups during init")

    try:
        load_autodel_groups()
    except Exception:
        log.exception("Unexpected failure while loading autodel groups during init")