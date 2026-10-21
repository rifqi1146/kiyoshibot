import os
import sqlite3
import time
import logging
from contextlib import closing

log=logging.getLogger(__name__)
DB_PATH=os.getenv("BLACKLIST_DB_PATH","data/blacklist.sqlite3")
_USER_CACHE=None
_GROUP_CACHE=None

def _db():
    os.makedirs(os.path.dirname(DB_PATH) or ".",exist_ok=True)
    con=sqlite3.connect(DB_PATH,timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    return con

def init():
    global _USER_CACHE,_GROUP_CACHE
    with closing(_db()) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS blacklisted_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                added_by INTEGER,
                added_at INTEGER
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS blacklisted_groups (
                group_id INTEGER PRIMARY KEY,
                title TEXT,
                reason TEXT,
                added_by INTEGER,
                added_at INTEGER
            )
        """)
        con.commit()
        user_rows=con.execute("SELECT user_id FROM blacklisted_users").fetchall()
        group_rows=con.execute("SELECT group_id FROM blacklisted_groups").fetchall()
    _USER_CACHE={int(r[0]) for r in user_rows}
    _GROUP_CACHE={int(r[0]) for r in group_rows}
    log.info("Loaded blacklist cache | users=%s groups=%s",len(_USER_CACHE),len(_GROUP_CACHE))

def _ensure():
    if _USER_CACHE is None or _GROUP_CACHE is None:
        init()

def is_blacklisted(user_id:int)->bool:
    _ensure()
    return int(user_id) in _USER_CACHE

def add_user(user_id:int,reason:str|None=None,added_by:int|None=None):
    _ensure()
    user_id=int(user_id)
    reason=(reason or "").strip()
    added_by=int(added_by or 0)
    added_at=int(time.time())
    with closing(_db()) as con:
        con.execute(
            "INSERT OR REPLACE INTO blacklisted_users(user_id,reason,added_by,added_at) VALUES(?,?,?,?)",
            (user_id,reason,added_by,added_at)
        )
        con.commit()
    _USER_CACHE.add(user_id)
    log.info("User blacklisted | user_id=%s added_by=%s reason=%s",user_id,added_by,reason or "-")

def remove_user(user_id:int)->bool:
    _ensure()
    user_id=int(user_id)
    with closing(_db()) as con:
        cur=con.execute("DELETE FROM blacklisted_users WHERE user_id=?",(user_id,))
        con.commit()
    removed=cur.rowcount>0
    if removed:
        _USER_CACHE.discard(user_id)
        log.info("User removed from blacklist | user_id=%s",user_id)
    return removed

def get_user(user_id:int):
    _ensure()
    with closing(_db()) as con:
        row=con.execute(
            "SELECT user_id,reason,added_by,added_at FROM blacklisted_users WHERE user_id=?",
            (int(user_id),)
        ).fetchone()
    if not row:
        return None
    return {"user_id":int(row[0]),"reason":row[1] or "","added_by":int(row[2] or 0),"added_at":int(row[3] or 0)}

def list_users(limit:int=50):
    _ensure()
    with closing(_db()) as con:
        rows=con.execute(
            "SELECT user_id,reason,added_by,added_at FROM blacklisted_users ORDER BY added_at DESC LIMIT ?",
            (int(limit),)
        ).fetchall()
    return [{"user_id":int(r[0]),"reason":r[1] or "","added_by":int(r[2] or 0),"added_at":int(r[3] or 0)} for r in rows]

def is_group_blacklisted(group_id:int)->bool:
    _ensure()
    return int(group_id) in _GROUP_CACHE

def add_group(group_id:int,title:str|None=None,reason:str|None=None,added_by:int|None=None):
    _ensure()
    group_id=int(group_id)
    title=(title or "").strip()
    reason=(reason or "").strip()
    added_by=int(added_by or 0)
    added_at=int(time.time())
    with closing(_db()) as con:
        con.execute(
            "INSERT OR REPLACE INTO blacklisted_groups(group_id,title,reason,added_by,added_at) VALUES(?,?,?,?,?)",
            (group_id,title,reason,added_by,added_at)
        )
        con.commit()
    _GROUP_CACHE.add(group_id)
    log.info("Group blacklisted | group_id=%s title=%s added_by=%s reason=%s",group_id,title or "-",added_by,reason or "-")

def remove_group(group_id:int)->bool:
    _ensure()
    group_id=int(group_id)
    with closing(_db()) as con:
        cur=con.execute("DELETE FROM blacklisted_groups WHERE group_id=?",(group_id,))
        con.commit()
    removed=cur.rowcount>0
    if removed:
        _GROUP_CACHE.discard(group_id)
        log.info("Group removed from blacklist | group_id=%s",group_id)
    return removed

def get_group(group_id:int):
    _ensure()
    with closing(_db()) as con:
        row=con.execute(
            "SELECT group_id,title,reason,added_by,added_at FROM blacklisted_groups WHERE group_id=?",
            (int(group_id),)
        ).fetchone()
    if not row:
        return None
    added_at=int(row[4] or 0)
    return {
        "group_id":int(row[0]),
        "title":row[1] or "",
        "reason":row[2] or "",
        "added_by":int(row[3] or 0),
        "added_at":added_at,
        "created_at":added_at
    }

def list_groups(limit:int=50):
    _ensure()
    with closing(_db()) as con:
        rows=con.execute(
            "SELECT group_id,title,reason,added_by,added_at FROM blacklisted_groups ORDER BY added_at DESC LIMIT ?",
            (int(limit),)
        ).fetchall()
    result=[]
    for r in rows:
        added_at=int(r[4] or 0)
        result.append({
            "group_id":int(r[0]),
            "title":r[1] or "",
            "reason":r[2] or "",
            "added_by":int(r[3] or 0),
            "added_at":added_at,
            "created_at":added_at
        })
    return result

_db_init=init