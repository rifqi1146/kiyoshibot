import os
import time
import json
import sqlite3
import asyncio

GROQ_MEMORY_EXPIRE=int(os.getenv("GROQ_MEMORY_EXPIRE",str(60*60*24)))
GROQ_DB_PATH=os.getenv("GROQ_MEMORY_DB_PATH","data/groq_memory.sqlite3")
GROQ_MAX_MESSAGES=int(os.getenv("GROQ_MAX_MESSAGES","20"))

def _db_init():
    os.makedirs(os.path.dirname(GROQ_DB_PATH) or ".",exist_ok=True)
    con=sqlite3.connect(GROQ_DB_PATH)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("""
            CREATE TABLE IF NOT EXISTS groq_memory (
                user_id INTEGER PRIMARY KEY,
                history_json TEXT NOT NULL,
                last_used REAL NOT NULL,
                last_message_id INTEGER
            )
        """)
        con.commit()
    finally:
        con.close()

def _trim_history(history:list)->list:
    if GROQ_MAX_MESSAGES and GROQ_MAX_MESSAGES>0:
        history=history[-GROQ_MAX_MESSAGES:]
    return history

def _db_get(user_id:int):
    _db_init()
    con=sqlite3.connect(GROQ_DB_PATH)
    try:
        row=con.execute(
            "SELECT history_json,last_used,last_message_id FROM groq_memory WHERE user_id=?",
            (int(user_id),)
        ).fetchone()
        if not row:
            return None
        try:
            history=json.loads(row[0]) if row[0] else []
        except Exception:
            history=[]
        if not isinstance(history,list):
            history=[]
        last_used=float(row[1] or 0)
        last_message_id=int(row[2]) if row[2] is not None else None
        return history,last_used,last_message_id
    finally:
        con.close()

def _db_set(user_id:int,history:list,last_message_id:int|None):
    _db_init()
    history=_trim_history(history if isinstance(history,list) else [])
    con=sqlite3.connect(GROQ_DB_PATH)
    try:
        con.execute("""
            INSERT INTO groq_memory(user_id,history_json,last_used,last_message_id)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                history_json=excluded.history_json,
                last_used=excluded.last_used,
                last_message_id=excluded.last_message_id
        """,(int(user_id),json.dumps(history,ensure_ascii=False),time.time(),last_message_id))
        con.commit()
    finally:
        con.close()

def _db_set_last_message_id(user_id:int,last_message_id:int|None):
    current=_db_get(user_id)
    history=current[0] if current else []
    _db_set(user_id,history,last_message_id)

def _db_clear(user_id:int):
    _db_init()
    con=sqlite3.connect(GROQ_DB_PATH)
    try:
        con.execute("DELETE FROM groq_memory WHERE user_id=?",(int(user_id),))
        con.commit()
    finally:
        con.close()

def _db_cleanup(expire_seconds:int):
    _db_init()
    cutoff=time.time()-float(expire_seconds)
    con=sqlite3.connect(GROQ_DB_PATH)
    try:
        con.execute("DELETE FROM groq_memory WHERE last_used < ?",(cutoff,))
        con.commit()
    finally:
        con.close()

def _db_has_last_message_id(message_id:int)->bool:
    _db_init()
    con=sqlite3.connect(GROQ_DB_PATH)
    try:
        row=con.execute(
            "SELECT 1 FROM groq_memory WHERE last_message_id=? LIMIT 1",
            (int(message_id),)
        ).fetchone()
        return row is not None
    finally:
        con.close()

async def init():
    await asyncio.to_thread(_db_init)

async def cleanup():
    await asyncio.to_thread(_db_cleanup,GROQ_MEMORY_EXPIRE)

async def get_history(user_id:int)->list:
    res=await asyncio.to_thread(_db_get,user_id)
    if not res:
        return []
    history,_,_=res
    return history

async def set_history(user_id:int,history:list,last_message_id:int|None=None):
    await asyncio.to_thread(_db_set,user_id,history,last_message_id)

async def append_messages(user_id:int,messages:list,last_message_id:int|None=None):
    history=await get_history(user_id)
    history.extend(messages)
    await set_history(user_id,history,last_message_id)

async def set_last_message_id(user_id:int,last_message_id:int|None):
    await asyncio.to_thread(_db_set_last_message_id,user_id,last_message_id)

async def get_last_message_id(user_id:int)->int|None:
    res=await asyncio.to_thread(_db_get,user_id)
    if not res:
        return None
    _,_,last_message_id=res
    return last_message_id

async def clear(user_id:int):
    await asyncio.to_thread(_db_clear,user_id)

async def has_last_message_id(message_id:int)->bool:
    return await asyncio.to_thread(_db_has_last_message_id,message_id)