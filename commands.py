"""
Saari extra commands: queue/playback controls, auth, sudo, block/blacklist,
group management, ping/stats/bug/json, settings panels, channel play,
broadcast (modes ke saath), multi-assistant management aur /pic.
play.py ke handlers ke baad import hota hai (app.py).
"""
import asyncio
import collections
import io
import logging
import random
import time

import aiohttp
from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import ChatAdminRequired, UserAdminInvalid, RPCError
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, ChatPermissions, ChatPrivileges,
)

import config
import db
import music_queue as q
import progress
import store
import assistants as pool
from clients import bot, LOGGER, START_TIME
from helpers import smallcaps, smallcaps_title, esc, duration_to_seconds, blockquote
from play import (
    _do_play, _can_control, _is_group_admin, _play_next_or_autoplay, _call, _piped,
    _btn, OWNER_FILTER, NOT_YOUR_REQUEST_TEXT, ADMIN_ONLY_TEXT, ADMIN_STATUSES,
)

SUDO_FILTER = filters.create(lambda _, __, m: bool(m.from_user) and store.is_sudo(m.from_user.id))

GROUP_ONLY_TEXT = "⚠️ ᴛʜɪs ᴄσᴍᴍᴧηᴅ ᴄᴧη σηʟʏ ʙє ᴜsєᴅ ɪη ɢʀσᴜᴘs."
SUDO_ONLY_TEXT = f"❌ {smallcaps_title('only the owner or a sudo user can use this command')}."
OWNER_ONLY_TEXT = f"❌ {smallcaps_title('only the bot owner can use this command')}."

# ---------------------------------------------------------------------------
# Group-only commands — private mein bheje jaayein to wahi standard warning
# ---------------------------------------------------------------------------
GROUP_CMDS = [
    "pause", "resume", "skip", "askip", "stop", "end", "mute", "unmute", "queue", "jump",
    "move", "clear", "remove", "loop", "shuffle", "seek", "seekback", "speed", "playmode",
    "cmddelete", "auth", "unauth", "authlist", "playforce", "cplay", "cvplay", "cplayforce",
    "channelplay", "thumb", "position", "settings", "autoplayon", "autoplayoff", "reload",
    "promote", "demote", "ban", "unban", "kick",
]


@bot.on_message(filters.command(GROUP_CMDS) & filters.private)
async def _group_only_warning(client, message: Message):
    await message.reply_text(GROUP_ONLY_TEXT)


# /cmddelete ON ho to group mein bot ke commands handle hone ke baad delete ho jaate hain
@bot.on_message(filters.command(GROUP_CMDS + ["play", "ping", "help", "start", "id", "bug", "json", "stats", "sudolist"]) & filters.group, group=7)
async def _cmd_deleter(client, message: Message):
    if store.get_setting(message.chat.id, "cmddelete", False):
        try:
            await message.delete()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------
def _mention(user) -> str:
    name = esc(getattr(user, "first_name", None) or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


async def _resolve_user(client, message: Message, arg_index: int = 1):
    """Reply se ya command ke argument (@username / id) se user nikalta hai."""
    r = message.reply_to_message
    if r and r.from_user:
        return r.from_user
    if len(message.command) > arg_index:
        arg = message.command[arg_index].lstrip("@")
        key = int(arg) if arg.lstrip("-").isdigit() else arg
        try:
            return await client.get_users(key)
        except Exception:
            return None
    return None


async def _is_real_admin(client, chat_id: int, uid: int) -> bool:
    """Sirf asli group admin/owner/sudo (auth users nahi)."""
    if store.is_sudo(uid):
        return True
    try:
        m = await client.get_chat_member(chat_id, uid)
        return m.status in ADMIN_STATUSES
    except Exception:
        return False


async def _has_right(client, chat_id: int, uid: int, right: str) -> bool:
    if store.is_sudo(uid):
        return True
    try:
        m = await client.get_chat_member(chat_id, uid)
    except Exception:
        return False
    if m.status == ChatMemberStatus.OWNER:
        return True
    if m.status == ChatMemberStatus.ADMINISTRATOR:
        priv = getattr(m, "privileges", None)
        return bool(priv and getattr(priv, right, False))
    return False


def _parse_time(text: str):
    """'90' ya '1:30' ya '1:02:03' -> seconds, galat ho to None."""
    try:
        parts = [int(x) for x in text.strip().split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(x < 0 for x in parts):
        return None
    sec = 0
    for x in parts:
        sec = sec * 60 + x
    return sec


def _fmt(sec) -> str:
    sec = max(int(sec), 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def _need_playing(message: Message):
    track = q.get_now_playing(message.chat.id)
    if not track:
        await message.reply_text(f"❌ {smallcaps_title('nothing is playing right now')}.")
    return track


async def _control_gate(client, message: Message) -> bool:
    if not message.from_user:
        return False
    if not await _can_control(client, message.chat.id, message.from_user.id):
        await message.reply_text(NOT_YOUR_REQUEST_TEXT)
        return False
    return True


async def _admin_gate(client, message: Message) -> bool:
    if not message.from_user or not await _is_group_admin(client, message.chat.id, message.from_user.id):
        await message.reply_text(ADMIN_ONLY_TEXT)
        return False
    return True


# ---------------------------------------------------------------------------
# Playback: /askip /mute /unmute /seek /seekback /jump /speed /loop /position
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("askip") & filters.group)
async def askip_command(client, message: Message):
    if not await _control_gate(client, message):
        return
    if not await _need_playing(message):
        return
    await _play_next_or_autoplay(message.chat.id, reply_message=message, force_related=True)


async def _seek_to(chat_id: int, track: dict, new_pos: float) -> int:
    total = duration_to_seconds(track.get("duration"))
    if total:
        new_pos = min(new_pos, max(total - 1, 0))
    new_pos = max(int(new_pos), 0)
    await _call(chat_id).change_stream(chat_id, await _piped(chat_id, track, new_pos))
    progress.seek_to(chat_id, new_pos)
    return new_pos


async def _seek_cmd(client, message: Message, direction: int):
    if not await _control_gate(client, message):
        return
    track = await _need_playing(message)
    if not track:
        return
    secs = _parse_time(message.command[1]) if len(message.command) > 1 else None
    if not secs:
        name = "seek" if direction > 0 else "seekback"
        return await message.reply_text(f"❌ {smallcaps_title('give seconds too')}!\nExample: `/{name} 30`")
    try:
        pos = await _seek_to(message.chat.id, track, progress.elapsed(message.chat.id) + direction * secs)
        await message.reply_text(f"{'⏩' if direction > 0 else '⏪'} {smallcaps_title('moved to')} {_fmt(pos)}.")
    except Exception as e:
        LOGGER.warning(f"/seek fail: {e!r}")
        await message.reply_text(f"❌ {smallcaps_title('could not seek')}.")


@bot.on_message(filters.command("seek") & filters.group)
async def seek_command(client, message: Message):
    await _seek_cmd(client, message, +1)


@bot.on_message(filters.command("seekback") & filters.group)
async def seekback_command(client, message: Message):
    await _seek_cmd(client, message, -1)


@bot.on_message(filters.command("jump") & filters.group)
async def jump_command(client, message: Message):
    if not await _control_gate(client, message):
        return
    track = await _need_playing(message)
    if not track:
        return
    pos = _parse_time(message.command[1]) if len(message.command) > 1 else None
    if pos is None:
        return await message.reply_text(f"❌ {smallcaps_title('give a time too')}!\nExample: `/jump 1:30` {smallcaps_title('or')} `/jump 90`")
    try:
        pos = await _seek_to(message.chat.id, track, pos)
        await message.reply_text(f"⏩ {smallcaps_title('jumped to')} {_fmt(pos)}.")
    except Exception as e:
        LOGGER.warning(f"/jump fail: {e!r}")
        await message.reply_text(f"❌ {smallcaps_title('could not jump')}.")


@bot.on_message(filters.command("speed") & filters.group)
async def speed_command(client, message: Message):
    if not await _control_gate(client, message):
        return
    chat_id = message.chat.id
    cur = q.get_speed(chat_id)
    if len(message.command) < 2:
        return await message.reply_text(
            f"⚡ {smallcaps_title('current speed')}: {cur}x\nExample: `/speed 1.5` ({smallcaps_title('range')} 0.5 - 2.0)"
        )
    try:
        new = round(float(message.command[1].lower().rstrip("x")), 2)
    except ValueError:
        return await message.reply_text(f"❌ {smallcaps_title('invalid speed')}. 0.5 - 2.0")
    if not 0.5 <= new <= 2.0:
        return await message.reply_text(f"❌ {smallcaps_title('speed must be between')} 0.5 {smallcaps_title('and')} 2.0")

    track = q.get_now_playing(chat_id)
    if not track:
        q.set_speed(chat_id, new)
        return await message.reply_text(f"⚡ {smallcaps_title('speed set to')} {new}x.")
    status = await message.reply_text(f"⏳ {smallcaps_title('applying speed')}...")
    try:
        orig_pos = progress.elapsed(chat_id) * cur            # original track ke seconds
        q.set_speed(chat_id, new)
        start = orig_pos / new
        await _call(chat_id).change_stream(chat_id, await _piped(chat_id, track, start))
        progress.seek_to(chat_id, int(start))
        await status.edit_text(f"⚡ {smallcaps_title('speed set to')} {q.get_speed(chat_id)}x.")
    except Exception as e:
        LOGGER.warning(f"/speed fail: {e!r}")
        q.set_speed(chat_id, cur)
        await status.edit_text(f"❌ {smallcaps_title('could not change speed')}.")


@bot.on_message(filters.command("loop") & filters.group)
async def loop_command(client, message: Message):
    if not await _control_gate(client, message):
        return
    chat_id = message.chat.id
    if len(message.command) < 2:
        return await message.reply_text(
            f"🔁 {smallcaps_title('loop left')}: {q.get_loop(chat_id)}\nExample: `/loop 3` ({smallcaps_title('1-10')}) {smallcaps_title('or')} `/loop off`"
        )
    arg = message.command[1].lower()
    if arg in ("off", "0", "disable"):
        q.set_loop(chat_id, 0)
        return await message.reply_text(f"🔴 {smallcaps_title('loop turned off')}.")
    if not arg.isdigit() or not 1 <= int(arg) <= 10:
        return await message.reply_text(f"❌ {smallcaps_title('give a number between 1 and 10')}.")
    q.set_loop(chat_id, int(arg))
    await message.reply_text(f"🔁 {smallcaps_title('current song will repeat')} {arg} {smallcaps_title('more time(s)')}.")


@bot.on_message(filters.command("position") & filters.group)
async def position_command(client, message: Message):
    track = await _need_playing(message)
    if not track:
        return
    total = duration_to_seconds(track.get("duration"))
    el = progress.elapsed(message.chat.id)
    bar = progress.render_button_bar(el, total) if total else _fmt(el)
    title = esc(str(track.get("title", "")).split("|")[0].strip())
    await message.reply_text(f"📍 {smallcaps_title('position')}\n\n{smallcaps_title(title)}\n{bar}")


@bot.on_message(filters.command(["mute", "unmute"]) & filters.group)
async def mute_dispatch(client, message: Message):
    """Target (reply/@user/id) diya ho to GROUP mute/unmute, warna VC mute/unmute."""
    cmd = message.command[0].lower().split("@")[0]
    target = await _resolve_user(client, message)
    if target is not None:
        return await _group_mute(client, message, target, mute=(cmd == "mute"))

    if not await _control_gate(client, message):
        return
    if not await _need_playing(message):
        return
    chat_id = message.chat.id
    try:
        if cmd == "mute":
            await _call(chat_id).mute_stream(chat_id)
            await message.reply_text(f"🔇 {smallcaps_title('playback muted in the voice chat')}.")
        else:
            await _call(chat_id).unmute_stream(chat_id)
            await message.reply_text(f"🔊 {smallcaps_title('playback unmuted in the voice chat')}.")
    except Exception as e:
        await message.reply_text(f"❌ {esc(e)}")


# ---------------------------------------------------------------------------
# Queue: /queue /move /remove /clear /shuffle
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("queue") & filters.group)
async def queue_command(client, message: Message):
    chat_id = message.chat.id
    now = q.get_now_playing(chat_id)
    lst = q.get_queue(chat_id)
    if not now and not lst:
        return await message.reply_text(f"📭 {smallcaps_title('queue is empty')}.")
    lines = []
    if now:
        t = esc(str(now.get("title", "")).split("|")[0].strip())
        lines.append(f"▶️ {smallcaps_title('now playing')}: {smallcaps_title(t)} [{esc(now.get('duration', ''))}]")
    if lst:
        lines.append(f"\n{smallcaps_title('up next')} ({len(lst)}):")
        for i, tr in enumerate(lst[:15], 1):
            t = esc(str(tr.get("title", "")).split("|")[0].strip())
            lines.append(f"{i}. {smallcaps_title(t)} [{esc(tr.get('duration', ''))}]")
        if len(lst) > 15:
            lines.append(f"... +{len(lst) - 15} {smallcaps_title('more')}")
    await message.reply_text(blockquote("\n".join(lines)))


@bot.on_message(filters.command("move") & filters.group)
async def move_command(client, message: Message):
    if not await _admin_gate(client, message):
        return
    lst = q.get_queue(message.chat.id)
    try:
        a, b = int(message.command[1]), int(message.command[2])
        assert 1 <= a <= len(lst) and 1 <= b <= len(lst)
    except Exception:
        return await message.reply_text(
            f"❌ {smallcaps_title('usage')}: `/move 3 1` ({smallcaps_title('queue has')} {len(lst)} {smallcaps_title('tracks')})"
        )
    item = lst.pop(a - 1)
    lst.insert(b - 1, item)
    await message.reply_text(f"✅ {smallcaps_title('moved track')} #{a} ➜ #{b}.")


@bot.on_message(filters.command("remove") & filters.group)
async def remove_command(client, message: Message):
    if not await _admin_gate(client, message):
        return
    lst = q.get_queue(message.chat.id)
    try:
        n = int(message.command[1])
        assert 1 <= n <= len(lst)
    except Exception:
        return await message.reply_text(f"❌ {smallcaps_title('usage')}: `/remove 2` ({smallcaps_title('queue has')} {len(lst)} {smallcaps_title('tracks')})")
    item = lst.pop(n - 1)
    t = esc(str(item.get("title", "")).split("|")[0].strip())
    await message.reply_text(f"🗑 {smallcaps_title('removed')}: {smallcaps_title(t)}")


@bot.on_message(filters.command("clear") & filters.group)
async def clear_command(client, message: Message):
    if not await _admin_gate(client, message):
        return
    n = len(q.get_queue(message.chat.id))
    q.get_queue(message.chat.id).clear()
    await message.reply_text(f"🧹 {smallcaps_title('queue cleared')} ({n} {smallcaps_title('tracks removed')}).")


@bot.on_message(filters.command("shuffle") & filters.group)
async def shuffle_command(client, message: Message):
    if not await _admin_gate(client, message):
        return
    lst = q.get_queue(message.chat.id)
    if len(lst) < 2:
        return await message.reply_text(f"❌ {smallcaps_title('need at least 2 queued tracks to shuffle')}.")
    random.shuffle(lst)
    await message.reply_text(f"🔀 {smallcaps_title('queue shuffled')}.")


# ---------------------------------------------------------------------------
# /playforce /cplay /cvplay /cplayforce /channelplay
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("playforce") & filters.group)
async def playforce_command(client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(f"❌ {smallcaps_title('give me a song name too')}!\nExample: `/playforce Shape of You`")
    if not await _admin_gate(client, message):
        return
    await _do_play(message, message.text.split(None, 1)[1], force=True)


async def _channel_play(client, message: Message, force: bool):
    ch = store.get_setting(message.chat.id, "channel")
    if not ch:
        return await message.reply_text(
            f"❌ {smallcaps_title('no channel linked')}.\n{smallcaps_title('use')} `/channelplay [channel id]` {smallcaps_title('first')}."
        )
    if len(message.command) < 2:
        return await message.reply_text(f"❌ {smallcaps_title('give me a song name too')}!\nExample: `/cplay Shape of You`")
    await _do_play(message, message.text.split(None, 1)[1], force=force, target_chat=ch)


@bot.on_message(filters.command(["cplay", "cvplay"]) & filters.group)
async def cplay_command(client, message: Message):
    await _channel_play(client, message, False)


@bot.on_message(filters.command("cplayforce") & filters.group)
async def cplayforce_command(client, message: Message):
    await _channel_play(client, message, True)


@bot.on_message(filters.command("channelplay") & filters.group)
async def channelplay_command(client, message: Message):
    if not await _is_real_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    cur = store.get_setting(message.chat.id, "channel")
    if len(message.command) < 2:
        return await message.reply_text(
            f"📡 {smallcaps_title('linked channel')}: `{cur or 'none'}`\n"
            f"{smallcaps_title('usage')}: `/channelplay -100123456789` {smallcaps_title('or')} `/channelplay disable`"
        )
    arg = message.command[1]
    if arg.lower() in ("disable", "off", "none"):
        await store.set_setting(message.chat.id, "channel", None)
        return await message.reply_text(f"🔴 {smallcaps_title('channel play disabled')}.")
    try:
        chat = await bot.get_chat(int(arg) if arg.lstrip("-").isdigit() else arg.lstrip("@"))
    except Exception:
        return await message.reply_text(f"❌ {smallcaps_title('could not find that channel — add me there as admin first')}.")
    await store.set_setting(message.chat.id, "channel", chat.id)
    await message.reply_text(
        f"✅ {smallcaps_title('linked')}: {esc(chat.title)} (`{chat.id}`)\n"
        f"{smallcaps_title('make sure the bot is admin in the channel, then use')} `/cplay`."
    )


# ---------------------------------------------------------------------------
# /playmode /cmddelete /thumb /settings
# ---------------------------------------------------------------------------
def _onoff(v: bool) -> str:
    return smallcaps_title("enabled") if v else smallcaps_title("disabled")


@bot.on_message(filters.command("playmode") & filters.group)
async def playmode_command(client, message: Message):
    if not await _admin_gate(client, message):
        return
    cur = store.get_setting(message.chat.id, "playmode", "everyone")
    new = "admins" if cur == "everyone" else "everyone"
    await store.set_setting(message.chat.id, "playmode", new)
    await message.reply_text(
        f"🎛 {smallcaps_title('play mode')}: **{smallcaps_title('admins only' if new == 'admins' else 'everyone')}**"
    )


@bot.on_message(filters.command("cmddelete") & filters.group)
async def cmddelete_command(client, message: Message):
    if not await _admin_gate(client, message):
        return
    new = not store.get_setting(message.chat.id, "cmddelete", False)
    await store.set_setting(message.chat.id, "cmddelete", new)
    await message.reply_text(
        f"🧹 {smallcaps_title('command delete')}: **{_onoff(new)}**\n"
        f"{smallcaps_title('make sure i have the delete messages right')}."
    )


def _thumb_panel(chat_id: int):
    on = store.get_setting(chat_id, "thumb", True)
    text = (
        f"⊚ {smallcaps_title('thumbnail settings')}\n\n"
        f"{smallcaps_title('status')}: **{_onoff(on)}**"
    )
    kb = InlineKeyboardMarkup([
        [_btn(smallcaps_title("enable"), callback_data="thumb_on", style="success"),
         _btn(smallcaps_title("disable"), callback_data="thumb_off", style="danger")],
        [_btn(smallcaps_title("close"), callback_data="thumb_close", style="primary")],
    ])
    return text, kb


@bot.on_message(filters.command("thumb") & filters.group)
async def thumb_command(client, message: Message):
    if not await _admin_gate(client, message):
        return
    text, kb = _thumb_panel(message.chat.id)
    await message.reply_text(text, reply_markup=kb)


@bot.on_callback_query(filters.regex("^thumb_(on|off|close)$"))
async def thumb_cb(client, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    if not await _is_group_admin(client, chat_id, cq.from_user.id):
        return await cq.answer(ADMIN_ONLY_TEXT, show_alert=True)
    action = cq.data.split("_")[1]
    if action == "close":
        await cq.answer()
        return await cq.message.delete()
    await store.set_setting(chat_id, "thumb", action == "on")
    await cq.answer("✅")
    text, kb = _thumb_panel(chat_id)
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


def _settings_panel(chat_id: int):
    pm = store.get_setting(chat_id, "playmode", "everyone")
    cd = store.get_setting(chat_id, "cmddelete", False)
    th = store.get_setting(chat_id, "thumb", True)
    ap = q.get_autoplay(chat_id)
    text = (
        f"⚙️ {smallcaps_title('chat settings')}\n\n"
        f"• {smallcaps_title('play mode')}: **{smallcaps_title('admins only' if pm == 'admins' else 'everyone')}**\n"
        f"• {smallcaps_title('command delete')}: **{_onoff(cd)}**\n"
        f"• {smallcaps_title('thumbnail')}: **{_onoff(th)}**\n"
        f"• {smallcaps_title('autoplay')}: **{_onoff(ap)}**\n"
        f"• {smallcaps_title('channel')}: `{store.get_setting(chat_id, 'channel') or 'none'}`"
    )
    kb = InlineKeyboardMarkup([
        [_btn(smallcaps_title("play mode"), callback_data="st_playmode", style="primary"),
         _btn(smallcaps_title("cmd delete"), callback_data="st_cmddelete", style="success")],
        [_btn(smallcaps_title("thumbnail"), callback_data="st_thumb", style="success"),
         _btn(smallcaps_title("autoplay"), callback_data="st_autoplay", style="primary")],
        [_btn(smallcaps_title("close"), callback_data="st_close", style="danger")],
    ])
    return text, kb


@bot.on_message(filters.command("settings") & filters.group)
async def settings_command(client, message: Message):
    text, kb = _settings_panel(message.chat.id)
    await message.reply_text(text, reply_markup=kb)


@bot.on_callback_query(filters.regex("^st_"))
async def settings_cb(client, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    if not await _is_group_admin(client, chat_id, cq.from_user.id):
        return await cq.answer(ADMIN_ONLY_TEXT, show_alert=True)
    a = cq.data
    if a == "st_close":
        await cq.answer()
        return await cq.message.delete()
    if a == "st_playmode":
        cur = store.get_setting(chat_id, "playmode", "everyone")
        await store.set_setting(chat_id, "playmode", "admins" if cur == "everyone" else "everyone")
    elif a == "st_cmddelete":
        await store.set_setting(chat_id, "cmddelete", not store.get_setting(chat_id, "cmddelete", False))
    elif a == "st_thumb":
        await store.set_setting(chat_id, "thumb", not store.get_setting(chat_id, "thumb", True))
    elif a == "st_autoplay":
        q.set_autoplay(chat_id, not q.get_autoplay(chat_id))
    await cq.answer("✅")
    text, kb = _settings_panel(chat_id)
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /auth /unauth /authlist
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("auth") & filters.group)
async def auth_command(client, message: Message):
    if not await _is_real_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    user = await _resolve_user(client, message)
    if not user:
        return await message.reply_text(f"❌ {smallcaps_title('reply to a user or give @username / id')}.")
    await store.add_auth(message.chat.id, user.id)
    await message.reply_text(f"✅ {_mention(user)} {smallcaps_title('added to the authorized list')}.")


@bot.on_message(filters.command("unauth") & filters.group)
async def unauth_command(client, message: Message):
    if not await _is_real_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    user = await _resolve_user(client, message)
    if not user:
        return await message.reply_text(f"❌ {smallcaps_title('reply to a user or give @username / id')}.")
    await store.rm_auth(message.chat.id, user.id)
    await message.reply_text(f"✅ {_mention(user)} {smallcaps_title('removed from the authorized list')}.")


@bot.on_message(filters.command("authlist") & filters.group)
async def authlist_command(client, message: Message):
    ids = sorted(store.auth.get(message.chat.id, set()))
    if not ids:
        return await message.reply_text(f"📭 {smallcaps_title('no authorized users in this chat')}.")
    lines = []
    for i, uid in enumerate(ids, 1):
        try:
            u = await client.get_users(uid)
            lines.append(f"{i}. {_mention(u)} (`{uid}`)")
        except Exception:
            lines.append(f"{i}. `{uid}`")
    await message.reply_text(f"👥 {smallcaps_title('authorized users')}:\n\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Group management: /promote /demote /ban /unban /kick (+ user /mute /unmute)
# ---------------------------------------------------------------------------
async def _manage(client, message: Message, right: str, action_name: str, run):
    """Common checks + error handling. run(chat_id, user) -> coroutine."""
    chat_id = message.chat.id
    if not message.from_user or not await _has_right(client, chat_id, message.from_user.id, right):
        return await message.reply_text(
            f"❌ {smallcaps_title('you need the right permission to use this command')}."
        )
    user = await _resolve_user(client, message)
    if not user:
        return await message.reply_text(f"❌ {smallcaps_title('reply to a user or give @username / id')}.")
    me = await bot.get_me()
    if user.id == me.id:
        return await message.reply_text(f"😅 {smallcaps_title('i cannot do that to myself')}.")
    if user.id == message.from_user.id:
        return await message.reply_text(f"😅 {smallcaps_title('you cannot do that to yourself')}.")
    try:
        await run(chat_id, user)
        await message.reply_text(f"✅ {_mention(user)} {smallcaps_title(action_name)}.")
    except ChatAdminRequired:
        await message.reply_text(f"❌ {smallcaps_title('i need admin rights with the required permission for this')}.")
    except UserAdminInvalid:
        await message.reply_text(f"❌ {smallcaps_title('i cannot do this to an admin (or the owner)')}.")
    except RPCError as e:
        await message.reply_text(f"❌ {esc(e.MESSAGE if hasattr(e, 'MESSAGE') else e)}")
    except Exception as e:
        await message.reply_text(f"❌ {esc(e)}")


@bot.on_message(filters.command("ban") & filters.group)
async def ban_command(client, message: Message):
    await _manage(client, message, "can_restrict_members", "banned", lambda c, u: client.ban_chat_member(c, u.id))


@bot.on_message(filters.command("unban") & filters.group)
async def unban_command(client, message: Message):
    await _manage(client, message, "can_restrict_members", "unbanned", lambda c, u: client.unban_chat_member(c, u.id))


@bot.on_message(filters.command("kick") & filters.group)
async def kick_command(client, message: Message):
    async def run(c, u):
        await client.ban_chat_member(c, u.id)
        await asyncio.sleep(0.5)
        await client.unban_chat_member(c, u.id)
    await _manage(client, message, "can_restrict_members", "kicked", run)


async def _group_mute(client, message: Message, target, mute: bool):
    async def run(c, u):
        if mute:
            await client.restrict_chat_member(c, u.id, ChatPermissions())
        else:
            chat = await client.get_chat(c)
            perms = chat.permissions or ChatPermissions(can_send_messages=True)
            await client.restrict_chat_member(c, u.id, perms)
    await _manage(client, message, "can_restrict_members", "muted" if mute else "unmuted", run)


@bot.on_message(filters.command("promote") & filters.group)
async def promote_command(client, message: Message):
    await _manage(
        client, message, "can_promote_members", "promoted",
        lambda c, u: client.promote_chat_member(
            c, u.id,
            privileges=ChatPrivileges(
                can_manage_chat=True, can_delete_messages=True, can_manage_video_chats=True,
                can_restrict_members=True, can_invite_users=True, can_pin_messages=True,
                can_change_info=False, can_promote_members=False,
            ),
        ),
    )


@bot.on_message(filters.command("demote") & filters.group)
async def demote_command(client, message: Message):
    await _manage(
        client, message, "can_promote_members", "demoted",
        lambda c, u: client.promote_chat_member(
            c, u.id,
            privileges=ChatPrivileges(
                can_manage_chat=False, can_delete_messages=False, can_manage_video_chats=False,
                can_restrict_members=False, can_invite_users=False, can_pin_messages=False,
                can_change_info=False, can_promote_members=False,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# /ping /stats /bug /json
# ---------------------------------------------------------------------------
def _uptime_text() -> str:
    s = int(time.monotonic() - START_TIME)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m}m {sec}s"


def _sys_lines() -> str:
    try:
        import psutil
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        cpu = psutil.cpu_percent(interval=0.3)
        gb = 1024 ** 3
        return (
            f"ʀᴧᴍ ᴜsᴧɢє: {vm.used / gb:.2f} / {vm.total / gb:.2f} GB\n"
            f"ᴄᴘᴜ ᴜsᴧɢє: {cpu:.2f}%\n"
            f"ᴅɪsᴋ ᴜsᴧɢє: {du.used / gb:.2f} / {du.total / gb:.2f} GB"
        )
    except Exception as e:
        LOGGER.warning(f"psutil stats fail: {e}")
        return "ʀᴧᴍ/ᴄᴘᴜ/ᴅɪsᴋ: n/a"


@bot.on_message(filters.command("ping"))
async def ping_command(client, message: Message):
    t0 = time.perf_counter()
    m = await message.reply_text("🏓 ...")
    ms = int((time.perf_counter() - t0) * 1000)
    text = (
        f"🏓 ᴘσηɢ: {ms}ms\n\n"
        f"🤖 𝙈ɪᴛ𝙨ᴜʀɪ × 𝙈ᴜ𝙨ɪᴄ sʏsᴛєᴍ sᴛᴧᴛ's :\n\n"
        f"ᴜᴘᴛɪᴍє: {_uptime_text()}\n"
        f"{_sys_lines()}"
    )
    await m.edit_text(text)


@bot.on_message(filters.command("stats"))
async def stats_command(client, message: Message):
    users = len(await db.get_all_users())
    chats = len(await db.get_all_chats())
    active = len(q._now_playing)
    lines = [
        f"📊 {smallcaps_title('bot stats')}\n",
        f"{smallcaps_title('users')}: {users}",
        f"{smallcaps_title('chats')}: {chats}",
        f"{smallcaps_title('assistants')}: {len(pool.pool)}",
        f"{smallcaps_title('active streams')}: {active}",
        f"{smallcaps_title('sudo users')}: {len(store.sudoers)}",
        f"ᴜᴘᴛɪᴍє: {_uptime_text()}",
        _sys_lines(),
    ]
    await message.reply_text("\n".join(lines))


@bot.on_message(filters.command("bug"))
async def bug_command(client, message: Message):
    text = message.text.split(None, 1)[1] if len(message.command) > 1 else None
    if not text and message.reply_to_message:
        text = message.reply_to_message.text or message.reply_to_message.caption
    if not text:
        return await message.reply_text(f"❌ {smallcaps_title('describe the bug too')}!\nExample: `/bug skip button is not working`")
    u = message.from_user
    where = message.chat.title or "Private chat"
    report = (
        f"🐞 <b>Bug Report</b>\n\n"
        f"👤 {_mention(u)} (<code>{u.id}</code>)\n"
        f"💬 {esc(where)} (<code>{message.chat.id}</code>)\n\n"
        f"{esc(text)}"
    )
    sent = False
    for target in (config.LOG_GROUP_ID, config.OWNER_ID):
        if not target:
            continue
        try:
            await bot.send_message(target, report, parse_mode=ParseMode.HTML)
            sent = True
            break
        except Exception as e:
            LOGGER.warning(f"Bug report send fail ({target}): {e}")
    if sent:
        await message.reply_text(f"✅ {smallcaps_title('your report has been sent, thank you')}!")
    else:
        await message.reply_text(f"❌ {smallcaps_title('could not send the report right now')}.")


@bot.on_message(filters.command("json"))
async def json_command(client, message: Message):
    obj = message.reply_to_message or message
    data = str(obj)
    if len(data) > 3500:
        bio = io.BytesIO(data.encode())
        bio.name = "message.json"
        return await message.reply_document(bio)
    await message.reply_text(f"<pre>{esc(data)}</pre>", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Sudo / block / blacklist / logs / logger / maintenance / active streams
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("sudo"))
async def sudo_command(client, message: Message):
    if not (message.from_user and store.is_owner(message.from_user.id)):
        return await message.reply_text(OWNER_ONLY_TEXT)
    user = await _resolve_user(client, message)
    if not user:
        return await message.reply_text(f"❌ {smallcaps_title('reply to a user or give @username / id')}.")
    await store.add_sudo(user.id)
    await message.reply_text(f"✅ {_mention(user)} {smallcaps_title('is now a sudo user')}.")


@bot.on_message(filters.command("rmsudo"))
async def rmsudo_command(client, message: Message):
    if not (message.from_user and store.is_owner(message.from_user.id)):
        return await message.reply_text(OWNER_ONLY_TEXT)
    user = await _resolve_user(client, message)
    if not user:
        return await message.reply_text(f"❌ {smallcaps_title('reply to a user or give @username / id')}.")
    await store.rm_sudo(user.id)
    await message.reply_text(f"✅ {_mention(user)} {smallcaps_title('removed from sudo')}.")


async def _names(client, ids) -> str:
    out = []
    for i, uid in enumerate(sorted(ids), 1):
        try:
            u = await client.get_users(uid)
            out.append(f"{i}. {_mention(u)} (<code>{uid}</code>)")
        except Exception:
            out.append(f"{i}. <code>{uid}</code>")
    return "\n".join(out)


@bot.on_message(filters.command("sudolist"))
async def sudolist_command(client, message: Message):
    owner = f"👑 {smallcaps_title('owner')}: <code>{config.OWNER_ID}</code>\n\n"
    body = await _names(client, store.sudoers) if store.sudoers else smallcaps_title("no sudo users")
    await message.reply_text(owner + body, parse_mode=ParseMode.HTML)


@bot.on_message(filters.command("block") & SUDO_FILTER)
async def block_command(client, message: Message):
    user = await _resolve_user(client, message)
    if not user:
        return await message.reply_text(f"❌ {smallcaps_title('reply to a user or give @username / id')}.")
    if store.is_sudo(user.id):
        return await message.reply_text(f"❌ {smallcaps_title('cannot block the owner or a sudo user')}.")
    await store.block_user(user.id)
    await message.reply_text(f"🚫 {_mention(user)} {smallcaps_title('blocked from using the bot')}.")


@bot.on_message(filters.command("unblock") & SUDO_FILTER)
async def unblock_command(client, message: Message):
    user = await _resolve_user(client, message)
    if not user:
        return await message.reply_text(f"❌ {smallcaps_title('reply to a user or give @username / id')}.")
    await store.unblock_user(user.id)
    await message.reply_text(f"✅ {_mention(user)} {smallcaps_title('unblocked')}.")


@bot.on_message(filters.command("blockedusers") & SUDO_FILTER)
async def blockedusers_command(client, message: Message):
    if not store.blocked:
        return await message.reply_text(f"📭 {smallcaps_title('no blocked users')}.")
    await message.reply_text(f"🚫 {smallcaps_title('blocked users')}:\n\n" + await _names(client, store.blocked), parse_mode=ParseMode.HTML)


def _chat_id_arg(message: Message):
    if len(message.command) < 2:
        return None
    try:
        return int(message.command[1])
    except ValueError:
        return None


@bot.on_message(filters.command("blacklistchat") & SUDO_FILTER)
async def blacklistchat_command(client, message: Message):
    cid = _chat_id_arg(message)
    if cid is None:
        return await message.reply_text(f"❌ {smallcaps_title('usage')}: `/blacklistchat -100123456789`")
    await store.blacklist_chat(cid)
    await message.reply_text(f"🚫 {smallcaps_title('chat blacklisted')}: `{cid}`")


@bot.on_message(filters.command("whitelistchat") & SUDO_FILTER)
async def whitelistchat_command(client, message: Message):
    cid = _chat_id_arg(message)
    if cid is None:
        return await message.reply_text(f"❌ {smallcaps_title('usage')}: `/whitelistchat -100123456789`")
    await store.whitelist_chat(cid)
    await message.reply_text(f"✅ {smallcaps_title('chat whitelisted')}: `{cid}`")


@bot.on_message(filters.command("blacklistedchat") & SUDO_FILTER)
async def blacklistedchat_command(client, message: Message):
    if not store.blchats:
        return await message.reply_text(f"📭 {smallcaps_title('no blacklisted chats')}.")
    await message.reply_text(f"🚫 {smallcaps_title('blacklisted chats')}:\n\n" + "\n".join(f"• `{c}`" for c in sorted(store.blchats)))


# --- logs ---
class _Ring(logging.Handler):
    def __init__(self):
        super().__init__()
        self.buf = collections.deque(maxlen=500)

    def emit(self, record):
        try:
            self.buf.append(self.format(record))
        except Exception:
            pass


_ring = _Ring()
_ring.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_ring)


@bot.on_message(filters.command("logs") & SUDO_FILTER)
async def logs_command(client, message: Message):
    data = "\n".join(list(_ring.buf)[-300:]) or "no logs yet"
    bio = io.BytesIO(data.encode())
    bio.name = "bot_logs.txt"
    await message.reply_document(bio, caption=f"📄 {smallcaps_title('latest logs')}")


async def _toggle_flag(message: Message, flag: str, label: str):
    if len(message.command) < 2:
        cur = store.flags.get(flag, False)
        return await message.reply_text(
            f"{label}: **{_onoff(cur)}**\n{smallcaps_title('usage')}: `/{message.command[0]} enable` {smallcaps_title('or')} `/{message.command[0]} disable`"
        )
    arg = message.command[1].lower()
    if arg in ("enable", "on", "yes"):
        await store.set_flag(flag, True)
        return await message.reply_text(f"✅ {label}: **{_onoff(True)}**")
    if arg in ("disable", "off", "no"):
        await store.set_flag(flag, False)
        return await message.reply_text(f"🔴 {label}: **{_onoff(False)}**")
    await message.reply_text(f"❌ {smallcaps_title('use enable or disable')}.")


@bot.on_message(filters.command("logger") & SUDO_FILTER)
async def logger_command(client, message: Message):
    if not config.LOG_GROUP_ID:
        return await message.reply_text(f"❌ {smallcaps_title('set LOG_GROUP_ID in env first')}.")
    await _toggle_flag(message, "logger", smallcaps_title("activity logger"))


@bot.on_message(filters.command("maintenance") & SUDO_FILTER)
async def maintenance_command(client, message: Message):
    await _toggle_flag(message, "maintenance", smallcaps_title("maintenance mode"))


@bot.on_message(filters.command(["activevoice", "ac"]) & SUDO_FILTER)
async def activevoice_command(client, message: Message):
    if not q._now_playing:
        return await message.reply_text(f"📭 {smallcaps_title('no active streams right now')}.")
    lines = [f"🎧 {smallcaps_title('active streams')} ({len(q._now_playing)}):\n"]
    for i, (cid, tr) in enumerate(list(q._now_playing.items()), 1):
        try:
            title = (await bot.get_chat(cid)).title
        except Exception:
            title = str(cid)
        a = pool.get_for_chat(cid)
        song = str(tr.get("title", "")).split("|")[0].strip()
        lines.append(f"{i}. {esc(title)} (`{cid}`)\n   🎵 {esc(song)}\n   🤖 {esc(a.name)}")
    await message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /broadcast  (-pin -pinloud -user -assistant -nobot)
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("broadcast") & SUDO_FILTER)
async def broadcast_command(client, message: Message):
    raw = message.text.split(None, 1)[1] if len(message.command) > 1 else ""
    flags, words = set(), raw.split()
    while words and words[0].startswith("-"):
        flags.add(words.pop(0).lower())
    text = " ".join(words)
    reply = message.reply_to_message
    if not text and not reply:
        return await message.reply_text(
            f"❌ {smallcaps_title('give me a message to broadcast')}!\n"
            "Example: `/broadcast -user -pin Testing broadcast`"
        )

    targets = await (db.get_all_users() if "-user" in flags else db.get_all_chats())
    status = await message.reply_text(f"📢 {smallcaps_title('broadcasting to')} {len(targets)} {smallcaps_title('chats')}...")
    sent = failed = pinned = 0
    a_text = text or (reply.text or reply.caption or "") if reply else text

    for cid in targets:
        ok = False
        if "-nobot" not in flags:
            try:
                msg = await reply.copy(cid) if reply else await bot.send_message(cid, text)
                ok = True
                if "-pin" in flags or "-pinloud" in flags:
                    try:
                        await msg.pin(disable_notification="-pinloud" not in flags)
                        pinned += 1
                    except Exception:
                        pass
            except Exception:
                pass
        if "-assistant" in flags and a_text and "-user" not in flags:
            for a in list(pool.pool):
                try:
                    await a.client.send_message(cid, a_text)
                    ok = True
                    break
                except Exception:
                    continue
        sent += 1 if ok else 0
        failed += 0 if ok else 1
        await asyncio.sleep(0.15)

    await status.edit_text(
        f"✅ {smallcaps_title('broadcast done')}.\n{smallcaps_title('sent')}: {sent}\n"
        f"{smallcaps_title('failed')}: {failed}\n{smallcaps_title('pinned')}: {pinned}"
    )


# ---------------------------------------------------------------------------
# Owner: /addassistant /assistantlist /removeassistant
# ---------------------------------------------------------------------------
_add_state: dict = {}   # owner_id -> {"step": ..., "api_id": ..., "api_hash": ...}
_pic_state: set = set()


def _pending_filter(_, __, m: Message) -> bool:
    uid = m.from_user.id if m.from_user else None
    return uid is not None and (uid in _add_state or uid in _pic_state)


@bot.on_message(filters.command("addassistant") & OWNER_FILTER & filters.private)
async def addassistant_command(client, message: Message):
    uid = message.from_user.id
    _pic_state.discard(uid)
    _add_state[uid] = {"step": "api_id"}
    await message.reply_text(
        f"🤖 {smallcaps_title('add new assistant')}\n\n"
        f"1️⃣ {smallcaps_title('send the API ID')}\n\n"
        f"({smallcaps_title('send')} /cancel {smallcaps_title('to stop')})"
    )


@bot.on_message(filters.command("addassistant") & OWNER_FILTER & filters.group)
async def addassistant_group_warning(client, message: Message):
    await message.reply_text(f"🔒 {smallcaps_title('send this command in my DM — it asks for private session data')}.")


@bot.on_message(filters.private & filters.text & OWNER_FILTER & filters.create(_pending_filter), group=3)
async def pending_input(client, message: Message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if text.startswith("/"):
        if text.split()[0].lower().startswith("/cancel"):
            _add_state.pop(uid, None)
            _pic_state.discard(uid)
            await message.reply_text(f"🛑 {smallcaps_title('cancelled')}.")
        return  # baaki commands apne handlers se chalte hain

    # ---- /pic: URL ----
    if uid in _pic_state:
        _pic_state.discard(uid)
        return await _set_pic_from_url(message, text)

    # ---- /addassistant steps ----
    st = _add_state[uid]
    if st["step"] == "api_id":
        if not text.isdigit():
            return await message.reply_text(f"❌ {smallcaps_title('API ID must be a number, send it again')}.")
        st["api_id"] = int(text)
        st["step"] = "api_hash"
        return await message.reply_text(f"2️⃣ {smallcaps_title('now send the API HASH')}")

    if st["step"] == "api_hash":
        if len(text) < 20:
            return await message.reply_text(f"❌ {smallcaps_title('that does not look like an API hash, send it again')}.")
        st["api_hash"] = text
        st["step"] = "session"
        try:
            await message.delete()
        except Exception:
            pass
        return await message.reply_text(f"3️⃣ {smallcaps_title('now send the STRING SESSION (pyrogram)')}")

    if st["step"] == "session":
        if len(text) < 50:
            return await message.reply_text(f"❌ {smallcaps_title('that does not look like a string session, send it again')}.")
        try:
            await message.delete()   # session private rahe
        except Exception:
            pass
        _add_state.pop(uid, None)
        status = await message.reply_text(f"⏳ {smallcaps_title('starting the assistant, please wait')}...")
        try:
            a = await pool.add_assistant(st["api_id"], st["api_hash"], text)
        except ValueError:
            return await status.edit_text(f"❌ {smallcaps_title('this assistant account is already added')}.")
        except Exception as e:
            LOGGER.error(f"addassistant fail: {e!r}", exc_info=True)
            return await status.edit_text(
                f"❌ {smallcaps_title('could not start this assistant — check API ID/HASH and that the session is a valid pyrogram string')}.\n\n`{esc(str(e)[:200])}`"
            )
        await status.edit_text(
            f"✅ {smallcaps_title('assistant added')}!\n\n"
            f"{smallcaps_title('name')}: {esc(a.name)}\n"
            f"{smallcaps_title('username')}: @{a.username or 'none'}\n"
            f"{smallcaps_title('id')}: `{a.user_id}`\n"
            f"{smallcaps_title('total assistants')}: {len(pool.pool)}"
        )


@bot.on_message(filters.command("assistantlist") & SUDO_FILTER)
async def assistantlist_command(client, message: Message):
    if not pool.pool:
        return await message.reply_text(f"📭 {smallcaps_title('no assistants running')}.")
    lines = [f"🎧 {smallcaps_title('assistants running')}: {len(pool.pool)}\n"]
    for i, a in enumerate(pool.pool, 1):
        busy = pool.active_chats(a)
        status = f"🎵 {smallcaps_title('busy')} ({len(busy)} {smallcaps_title('chat')})" if busy else f"✅ {smallcaps_title('idle')}"
        tag = " ⭐" if a.primary else ""
        lines.append(
            f"{i}. {esc(a.name)}{tag}\n"
            f"   @{a.username or 'none'} • `{a.user_id}`\n"
            f"   {status}"
        )
    await message.reply_text("\n".join(lines))


@bot.on_message(filters.command("removeassistant") & OWNER_FILTER)
async def removeassistant_command(client, message: Message):
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        return await message.reply_text(f"❌ {smallcaps_title('usage')}: `/removeassistant 123456789`\n({smallcaps_title('see ids in')} /assistantlist)")
    res = await pool.remove_assistant(int(message.command[1]))
    if res == "ok":
        await message.reply_text(f"✅ {smallcaps_title('assistant removed')}. {smallcaps_title('remaining')}: {len(pool.pool)}")
    elif res == "primary":
        await message.reply_text(f"❌ {smallcaps_title('the main assistant comes from STRING_SESSION in env — change it there')}.")
    else:
        await message.reply_text(f"❌ {smallcaps_title('no assistant with that id')}.")


# ---------------------------------------------------------------------------
# Owner: /pic — image URL se start image set
# ---------------------------------------------------------------------------
async def _set_pic_from_url(message: Message, url: str):
    if not url.startswith(("http://", "https://")):
        return await message.reply_text(f"❌ {smallcaps_title('send a valid image url (starting with http)')}.")
    status = await message.reply_text(f"⏳ {smallcaps_title('downloading image')}...")
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=40)) as r:
                if r.status != 200:
                    return await status.edit_text(f"❌ {smallcaps_title('url returned status')} {r.status}.")
                data = await r.read()
        if len(data) < 1000 or len(data) > 15 * 1024 * 1024:
            return await status.edit_text(f"❌ {smallcaps_title('that is not a usable image (too small/large)')}.")
        bio = io.BytesIO(data)
        bio.name = "start.jpg"
        sent = await bot.send_photo(message.chat.id, bio, caption=f"✅ {smallcaps_title('preview of your new start image')}")
        file_id = sent.photo.file_id
        await db.set_start_media(file_id, "photo")
        await db.set_group_start_media(file_id, "photo")
        await status.edit_text(
            f"✅ {smallcaps_title('start image set')}!\n"
            f"{smallcaps_title('it will show with the start message (in dm, users with a profile photo see their own dp)')}."
        )
    except Exception as e:
        LOGGER.warning(f"/pic fail: {e!r}")
        await status.edit_text(f"❌ {smallcaps_title('could not use this url — make sure it is a direct image link (jpg/png)')}.")


@bot.on_message(filters.command("pic") & OWNER_FILTER)
async def pic_command(client, message: Message):
    uid = message.from_user.id
    _add_state.pop(uid, None)
    if len(message.command) > 1:
        return await _set_pic_from_url(message, message.command[1])
    if message.chat.type.name != "PRIVATE":
        return await message.reply_text(f"❌ {smallcaps_title('use')} `/pic [url]` {smallcaps_title('or send /pic in my dm')}.")
    _pic_state.add(uid)
    await message.reply_text(
        f"🖼 {smallcaps_title('send the image url now')}\n\n({smallcaps_title('send')} /cancel {smallcaps_title('to stop')})"
    )


# ---------------------------------------------------------------------------
# Bot "/" menu — har command yahan se register hota hai (app.py)
# ---------------------------------------------------------------------------
BOT_COMMAND_LIST = [
    ("start", "start the bot"), ("help", "help and command menu"), ("ping", "live ping and system stats"),
    ("play", "play a song"), ("playforce", "play instantly, skip queue"), ("cplay", "play in linked channel"),
    ("cplayforce", "force play in linked channel"), ("channelplay", "link a channel for cplay"),
    ("pause", "pause playback"), ("resume", "resume playback"), ("skip", "skip the track"),
    ("askip", "play a related track"), ("stop", "stop and leave"), ("mute", "mute vc or a user"),
    ("unmute", "unmute vc or a user"), ("queue", "show the queue"), ("jump", "jump to a time"),
    ("seek", "seek forward"), ("seekback", "seek backward"), ("move", "move a queued track"),
    ("remove", "remove a queued track"), ("clear", "clear the queue"), ("shuffle", "shuffle the queue"),
    ("loop", "repeat the current song"), ("speed", "change playback speed"), ("position", "show track position"),
    ("playmode", "who can use play"), ("cmddelete", "auto delete commands"), ("thumb", "thumbnail settings"),
    ("settings", "chat settings"), ("autoplayon", "turn autoplay on"), ("autoplayoff", "turn autoplay off"),
    ("auth", "authorize a user"), ("unauth", "remove authorized user"), ("authlist", "list authorized users"),
    ("promote", "promote a user"), ("demote", "demote an admin"), ("ban", "ban a user"),
    ("unban", "unban a user"), ("kick", "kick a user"), ("reload", "refresh admin data"),
    ("bug", "report a problem"), ("json", "show message json"), ("stats", "bot statistics"),
    ("sudolist", "list sudo users"), ("id", "show ids"),
    ("sudo", "add a sudo user"), ("rmsudo", "remove a sudo user"), ("broadcast", "broadcast a message"),
    ("block", "block a user"), ("unblock", "unblock a user"), ("blockedusers", "list blocked users"),
    ("blacklistchat", "blacklist a chat"), ("whitelistchat", "whitelist a chat"),
    ("blacklistedchat", "list blacklisted chats"), ("logs", "get bot logs"), ("logger", "toggle activity logger"),
    ("maintenance", "toggle maintenance mode"), ("activevoice", "active streams"), ("ac", "active streams"),
    ("addassistant", "add a new assistant"), ("assistantlist", "list assistants"),
    ("removeassistant", "remove an assistant"), ("pic", "set start image from url"),
]
