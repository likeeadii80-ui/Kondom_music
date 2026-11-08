"""
Multi-assistant pool.

- Pehla assistant (index 1) env ke STRING_SESSION wala hota hai.
- Baaki assistants owner /addassistant se jodta hai (DB mein persist hote hain).
- Har chat ke liye `pick_for_chat` KHALI (idle) assistant chunta hai: jo
  assistant kisi aur group mein gaana baja raha ho use skip karke agla idle
  assistant us group ko milta hai. Sab busy ho to sabse kam busy wala use hota hai.
"""
import time

from pyrogram import Client
from pytgcalls import PyTgCalls

import config
import music_queue as q
from clients import assistant as primary_client, call_py as primary_call, db as mongo, LOGGER

assistants_col = mongo["assistants"]

RESERVE_SECONDS = 120


class Assistant:
    def __init__(self, client: Client, call: PyTgCalls, me, primary: bool = False):
        self.client = client
        self.call = call
        self.me = me
        self.primary = primary

    @property
    def user_id(self) -> int:
        return self.me.id

    @property
    def username(self) -> str:
        return self.me.username or ""

    @property
    def name(self) -> str:
        return " ".join(x for x in (self.me.first_name, self.me.last_name) if x) or str(self.me.id)


pool: list = []
chat_map: dict = {}      # chat_id -> Assistant
_reserved: dict = {}     # chat_id -> monotonic ts (join hone tak assistant "busy" maana jaata hai)
_stream_end_handler = None


def set_stream_end_handler(fn):
    """Har assistant (aur aage jodne wale) ke PyTgCalls par stream-end handler lagata hai."""
    global _stream_end_handler
    _stream_end_handler = fn
    for a in pool:
        _register(a)


def _register(a: Assistant):
    if _stream_end_handler is None or getattr(a, "_registered", False):
        return
    a.call.on_stream_end()(_stream_end_handler)
    a._registered = True


async def _cache_peers(client: Client):
    try:
        async for _ in client.get_dialogs():
            pass
    except Exception as e:
        LOGGER.warning(f"Assistant peers cache fail: {e}")


async def register_primary():
    """app.py: primary assistant start hone ke baad (call_py.start() se pehle ya baad) bulao."""
    if any(a.primary for a in pool):
        return
    me = await primary_client.get_me()
    a = Assistant(primary_client, primary_call, me, primary=True)
    pool.insert(0, a)
    _register(a)


async def _spawn(api_id: int, api_hash: str, session: str, label: str) -> Assistant:
    client = Client(
        name=f"Assistant_{label}", api_id=api_id, api_hash=api_hash,
        session_string=session, in_memory=True,
    )
    await client.start()
    try:
        me = await client.get_me()
        await _cache_peers(client)
        call = PyTgCalls(client)
        a = Assistant(client, call, me)
        _register(a)
        await call.start()
        return a
    except Exception:
        try:
            await client.stop()
        except Exception:
            pass
        raise


async def load_saved():
    """Startup par DB se saare saved assistants start karta hai."""
    try:
        docs = [d async for d in assistants_col.find({}).sort("added_at", 1)]
    except Exception as e:
        LOGGER.warning(f"Assistants DB load fail: {e}")
        return
    for d in docs:
        if any(x.user_id == d["_id"] for x in pool):
            continue
        try:
            a = await _spawn(d["api_id"], d["api_hash"], d["session"], str(d["_id"]))
            pool.append(a)
            LOGGER.info(f"✅ Extra assistant started: {a.name} (@{a.username})")
        except Exception as e:
            LOGGER.error(f"Assistant {d.get('_id')} start fail: {e!r}")


async def add_assistant(api_id: int, api_hash: str, session: str) -> Assistant:
    a = await _spawn(api_id, api_hash, session, f"new{int(time.time())}")
    if any(x.user_id == a.user_id for x in pool):
        try:
            await a.client.stop()
        except Exception:
            pass
        raise ValueError("duplicate")
    pool.append(a)
    await assistants_col.update_one(
        {"_id": a.user_id},
        {"$set": {"api_id": api_id, "api_hash": api_hash, "session": session, "added_at": time.time()}},
        upsert=True,
    )
    return a


async def remove_assistant(user_id: int) -> str:
    """Returns: 'ok' | 'notfound' | 'primary'"""
    target = next((a for a in pool if a.user_id == user_id), None)
    if target is None:
        return "notfound"
    if target.primary:
        return "primary"
    for cid in list(active_chats(target)):
        try:
            await target.call.leave_group_call(cid)
        except Exception:
            pass
        q.clear(cid)
        release(cid)
    pool.remove(target)
    try:
        await target.client.stop()
    except Exception:
        pass
    try:
        await assistants_col.delete_one({"_id": user_id})
    except Exception:
        pass
    return "ok"


# ---------------- chat <-> assistant mapping ----------------
def active_chats(a: Assistant) -> list:
    now = time.monotonic()
    out = []
    for cid, asst in chat_map.items():
        if asst is not a:
            continue
        if q.is_playing(cid) or (now - _reserved.get(cid, 0) < RESERVE_SECONDS):
            out.append(cid)
    return out


def pick_for_chat(chat_id: int) -> Assistant:
    """Is chat ke liye assistant chunta (aur yaad rakhta) hai."""
    cur = chat_map.get(chat_id)
    if cur in pool and q.is_playing(chat_id):
        return cur

    def others(a):
        return [c for c in active_chats(a) if c != chat_id]

    idle = [a for a in pool if not others(a)]
    if cur in idle:
        choice = cur
    elif idle:
        choice = idle[0]
    else:
        choice = min(pool, key=lambda a: len(others(a)))
    chat_map[chat_id] = choice
    _reserved[chat_id] = time.monotonic()
    return choice


def get_for_chat(chat_id: int) -> Assistant:
    a = chat_map.get(chat_id)
    if a in pool:
        return a
    return pool[0]


def get_call(chat_id: int):
    return get_for_chat(chat_id).call


def get_client(chat_id: int):
    return get_for_chat(chat_id).client


def release(chat_id: int):
    chat_map.pop(chat_id, None)
    _reserved.pop(chat_id, None)
