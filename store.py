"""
Persistent settings + in-memory cache: sudo users, blocked users, blacklisted
chats, auth users (per chat), per-chat settings, global flags (maintenance /
logger). Startup par `load_all()` DB se sab cache mein bhar deta hai.
"""
import time

import config
from clients import db as mongo, bot, LOGGER

sudo_col = mongo["sudo"]
blocked_col = mongo["blocked"]
blchats_col = mongo["blchats"]
auth_col = mongo["auth"]
chatset_col = mongo["chat_settings"]
globals_col = mongo["global_flags"]

sudoers: set = set()
blocked: set = set()
blchats: set = set()
auth: dict = {}            # chat_id -> set(user_id)
chat_settings: dict = {}   # chat_id -> {key: value}
flags: dict = {"maintenance": False, "logger": False}


async def load_all():
    try:
        async for d in sudo_col.find({}):
            sudoers.add(d["_id"])
        async for d in blocked_col.find({}):
            blocked.add(d["_id"])
        async for d in blchats_col.find({}):
            blchats.add(d["_id"])
        async for d in auth_col.find({}):
            auth.setdefault(d["chat_id"], set()).add(d["user_id"])
        async for d in chatset_col.find({}):
            chat_settings[d["_id"]] = {k: v for k, v in d.items() if k != "_id"}
        async for d in globals_col.find({}):
            flags[d["_id"]] = bool(d.get("value", False))
        LOGGER.info("✅ Store loaded (sudo/block/auth/settings)")
    except Exception as e:
        LOGGER.warning(f"Store load fail: {e}")


def is_owner(uid) -> bool:
    return bool(config.OWNER_ID) and uid == config.OWNER_ID


def is_sudo(uid) -> bool:
    return is_owner(uid) or uid in sudoers


def is_auth(chat_id: int, uid: int) -> bool:
    return uid in auth.get(chat_id, set())


# ---------------- generic id-set helpers ----------------
async def _add(col, cache: set, _id: int):
    cache.add(_id)
    try:
        await col.update_one({"_id": _id}, {"$set": {"_id": _id}}, upsert=True)
    except Exception as e:
        LOGGER.warning(f"store add fail: {e}")


async def _remove(col, cache: set, _id: int):
    cache.discard(_id)
    try:
        await col.delete_one({"_id": _id})
    except Exception as e:
        LOGGER.warning(f"store remove fail: {e}")


async def add_sudo(uid):
    await _add(sudo_col, sudoers, uid)


async def rm_sudo(uid):
    await _remove(sudo_col, sudoers, uid)


async def block_user(uid):
    await _add(blocked_col, blocked, uid)


async def unblock_user(uid):
    await _remove(blocked_col, blocked, uid)


async def blacklist_chat(cid):
    await _add(blchats_col, blchats, cid)


async def whitelist_chat(cid):
    await _remove(blchats_col, blchats, cid)


async def add_auth(chat_id: int, uid: int):
    auth.setdefault(chat_id, set()).add(uid)
    try:
        await auth_col.update_one(
            {"chat_id": chat_id, "user_id": uid},
            {"$set": {"chat_id": chat_id, "user_id": uid}}, upsert=True,
        )
    except Exception as e:
        LOGGER.warning(f"add_auth fail: {e}")


async def rm_auth(chat_id: int, uid: int):
    auth.get(chat_id, set()).discard(uid)
    try:
        await auth_col.delete_one({"chat_id": chat_id, "user_id": uid})
    except Exception as e:
        LOGGER.warning(f"rm_auth fail: {e}")


# ---------------- per-chat settings ----------------
def get_setting(chat_id: int, key: str, default=None):
    return chat_settings.get(chat_id, {}).get(key, default)


async def set_setting(chat_id: int, key: str, value):
    chat_settings.setdefault(chat_id, {})[key] = value
    try:
        await chatset_col.update_one({"_id": chat_id}, {"$set": {key: value}}, upsert=True)
    except Exception as e:
        LOGGER.warning(f"set_setting fail: {e}")


# ---------------- global flags ----------------
async def set_flag(name: str, value: bool):
    flags[name] = bool(value)
    try:
        await globals_col.update_one({"_id": name}, {"$set": {"value": bool(value)}}, upsert=True)
    except Exception as e:
        LOGGER.warning(f"set_flag fail: {e}")


async def log_event(text: str):
    """/logger ON ho to LOG_GROUP_ID mein activity log bhejta hai."""
    if not flags.get("logger") or not config.LOG_GROUP_ID:
        return
    try:
        await bot.send_message(config.LOG_GROUP_ID, text, disable_web_page_preview=True)
    except Exception as e:
        LOGGER.warning(f"log_event fail: {e}")
