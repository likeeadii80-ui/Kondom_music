import asyncio
import os
import random
import re
import time

from pyrogram import filters, StopPropagation
from pyrogram.enums import ChatMemberStatus, ButtonStyle
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from pytgcalls.types.input_stream import AudioPiped
from pytgcalls.exceptions import NoActiveGroupCall
from pyrogram.raw import functions as raw_functions

import config
import db
import music_queue as q
import progress
import botstate
import store
import assistants as pool
from clients import bot, LOGGER, START_TIME
from youtube import search_track, get_stream_url, search_related_track
from helpers import (
    smallcaps,
    smallcaps_title,
    random_processing_emoji,
    processing_caption,
    format_duration,
    fancy_italic,
    duration_to_seconds,
    format_uptime,
    blockquote,
    expandable_blockquote,
    esc,
    FOOTER_LINE,
)
from nowplaying import generate_now_playing_card
from assistant_join import ensure_assistant_in_chat


def _call(chat_id: int):
    """Is chat ke liye assigned assistant ka PyTgCalls."""
    return pool.get_call(chat_id)


def _cl(chat_id: int):
    """Is chat ke liye assigned assistant ka pyrogram Client."""
    return pool.get_client(chat_id)

async def _speed_file(path: str, speed: float) -> str:
    """ffmpeg se speed-adjusted copy banata hai (atempo 0.5–2.0)."""
    base, ext = os.path.splitext(path)
    out = f"{base}_x{speed}{ext or '.mp3'}"
    if os.path.isfile(out) and os.path.getsize(out) > 20_000:
        return out
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", path, "-vn", "-filter:a", f"atempo={speed}", out,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(proc.wait(), timeout=180)
    if proc.returncode != 0 or not os.path.isfile(out):
        raise RuntimeError("ffmpeg speed convert fail")
    return out


async def _piped(chat_id: int, track: dict, start=0):
    """Track ke liye AudioPiped banata hai — chat ki speed setting aur start offset ke saath."""
    path = track["stream_url"]
    speed = q.get_speed(chat_id)
    if speed != 1.0:
        try:
            path = await _speed_file(path, speed)
        except Exception as e:
            LOGGER.warning(f"Speed file fail ({speed}x), normal speed par chala rahe hain: {e!r}")
            q.set_speed(chat_id, 1.0)
    if start and int(start) > 0:
        return AudioPiped(path, additional_ffmpeg_parameters=f"-ss {int(start)}")
    return AudioPiped(path)


OWNER_FILTER = filters.user(config.OWNER_ID) if config.OWNER_ID else filters.create(lambda _, __, ___: False)

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

# /addvd chalane ke baad owner ki agli image/video/gif ka wait karte hain (private start message)
_pending_addvd = set()

# /addvd2 chalane ke baad owner ki agli image/video/gif ka wait karte hain (GROUP start message)
_pending_addvd2 = set()

_SEND_MEDIA_MAP_NAME = {"photo": "send_photo", "video": "send_video", "animation": "send_animation"}


# ---------------------------------------------------------------------------
# Peer cache helper — "Peer id invalid" error se bachne ke liye.
# Assistant account jab kisi chat mein direct koi update receive nahi karta
# (sirf VC join karta hai), to pyrogram uska peer/access_hash cache nahi kar
# paata aur baad mein change_stream/leave_group_call fail ho jaata hai.
# Isliye error aane par ek baar dialogs refresh karke retry karte hain.
# ---------------------------------------------------------------------------
async def _refresh_assistant_peers(chat_id: int = None):
    try:
        client = _cl(chat_id) if chat_id is not None else pool.pool[0].client
        async for _ in client.get_dialogs():
            pass
    except Exception as e:
        LOGGER.warning(f"Peer refresh fail: {e}")


def _is_peer_error(e: Exception) -> bool:
    return isinstance(e, ValueError) and "Peer id invalid" in str(e)


# ---------------------------------------------------------------------------
# Koi manually voice chat start na kare to bhi /play kaam kare — assistant
# (userbot) khud group ka voice chat shuru kar deta hai, phir usi mein join
# ho jaata hai. NOTE: Yeh sirf tabhi kaam karega jab assistant account ke
# paas is group mein voice chat start karne ka right ho (group admin ho, ya
# group ki setting "all members can start voice chats" allow karti ho) —
# warna Telegram CHAT_ADMIN_REQUIRED jaisa error dega aur hum purane manual
# wale message par fallback kar jaayenge.
# ---------------------------------------------------------------------------
async def _auto_start_group_call(chat_id: int) -> bool:
    try:
        peer = await _cl(chat_id).resolve_peer(chat_id)
        await _cl(chat_id).invoke(
            raw_functions.phone.CreateGroupCall(
                peer=peer,
                random_id=random.randint(1, 2_147_483_647),
            )
        )
        return True
    except Exception as e:
        LOGGER.warning(f"Auto voice-chat start fail (chat {chat_id}): {e}")
        return False


# ---------------------------------------------------------------------------
# Admin / owner check — /skip /pause /resume /stop /reload sirf group admin
# ya bot OWNER_ID ke liye. Normal user sirf /play use kar sakta hai.
# ---------------------------------------------------------------------------
async def _is_group_admin(client, chat_id: int, user_id: int) -> bool:
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    if store.is_sudo(user_id) or store.is_auth(chat_id, user_id):
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ADMIN_STATUSES
    except Exception:
        return False


ADMIN_ONLY_TEXT = f"❌ {smallcaps_title('only a group admin or the owner can use this command')}."

NOT_YOUR_REQUEST_TEXT = (
    f"❌ {smallcaps_title('this is not your request')}!\n"
    f"{smallcaps_title('only the person who requested this track, or a group admin/owner, can control it')}."
)


# ---------------------------------------------------------------------------
# Control-permission check — /skip /pause /resume /stop (aur inke inline
# buttons) sirf 3 log use kar sakte hain: jisne current track request kiya
# tha, group admin, ya bot owner. Baaki normal users ko NOT_YOUR_REQUEST_TEXT
# dikhaya jaata hai.
# ---------------------------------------------------------------------------
async def _can_control(client, chat_id: int, user_id: int) -> bool:
    if await _is_group_admin(client, chat_id, user_id):
        return True
    track = q.get_now_playing(chat_id)
    return bool(track and track.get("requested_by_id") == user_id)


def _assistant_not_joined_text(username: str) -> str:
    return (
        f"❌ **{smallcaps_title('my assistant account is not in this group')}!**\n\n"
        f"{smallcaps_title('the assistant account needs to be in the group to play music')}.\n"
        f"👉 @{username or config.ASSISTANT_USERNAME} {smallcaps_title('add it to the group, or get it to join')}.\n\n"
        f"{smallcaps_title('then run')} `/play` {smallcaps_title('again')}."
    )


ASSISTANT_FLOOD_TEXT = (
    f"⏳ {smallcaps_title('telegram has rate-limited us for a bit, try again shortly')}."
)


# ---------------------------------------------------------------------------
# Owner: /on /off — pura bot chalu/band karne ke liye global switch.
# OFF hone par bot kisi bhi message/button ka jawab nahi deta, sirf /on /off
# chalte rehte hain. DB mein persist hota hai, isliye restart ke baad bhi
# wahi status yaad rehta hai.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("on") & OWNER_FILTER)
async def on_command(client, message: Message):
    botstate.set_enabled(True)
    await db.set_bot_status(True)
    await message.reply_text(f"✅ {smallcaps_title('bot turned on')}.")


@bot.on_message(filters.command("off") & OWNER_FILTER)
async def off_command(client, message: Message):
    botstate.set_enabled(False)
    await db.set_bot_status(False)
    await message.reply_text(
        f"🔴 {smallcaps_title('bot turned off')}.\n"
        f"{smallcaps_title('only')} `/on` {smallcaps_title('will work now')}."
    )


@bot.on_message(filters.command("processingon") & OWNER_FILTER)
async def processingon_command(client, message: Message):
    botstate.set_processing_text_enabled(True)
    await db.set_processing_text_status(True)
    await message.reply_text(f"✅ {smallcaps_title('processing text turned on')}.")


@bot.on_message(filters.command("processingoff") & OWNER_FILTER)
async def processingoff_command(client, message: Message):
    botstate.set_processing_text_enabled(False)
    await db.set_processing_text_status(False)
    await message.reply_text(
        f"🔴 {smallcaps_title('processing text turned off')}.\n"
        f"{smallcaps_title('now only the emoji will be sent, like before')}."
    )


def _off_blocker(_, __, message: Message) -> bool:
    text = message.text or message.caption or ""
    uid = message.from_user.id if message.from_user else None
    if not botstate.is_enabled():
        # /on aur /off hamesha chalne chahiye, chahe bot OFF hi ho
        return not text.startswith(("/on", "/off"))
    if uid is not None and store.is_sudo(uid):
        return False
    if uid is not None and uid in store.blocked:
        return True
    if message.chat and message.chat.id in store.blchats:
        return True
    if store.flags.get("maintenance"):
        return True
    return False


def _off_blocker_cb(_, __, cq: CallbackQuery) -> bool:
    if not botstate.is_enabled():
        return True
    if store.is_sudo(cq.from_user.id):
        return False
    if cq.from_user.id in store.blocked:
        return True
    if cq.message and cq.message.chat and cq.message.chat.id in store.blchats:
        return True
    return bool(store.flags.get("maintenance"))


# group=-1 -> yeh handler sabse pehle chalta hai; OFF/blocked/maintenance mein
# isse aage kisi aur handler tak message/callback pahunchta hi nahi.
_maint_notice: dict = {}


@bot.on_message(filters.create(_off_blocker), group=-1)
async def _blocked_while_off(client, message: Message):
    text = message.text or ""
    if (botstate.is_enabled() and store.flags.get("maintenance") and text.startswith("/")
            and message.from_user and message.from_user.id not in store.blocked
            and message.chat.id not in store.blchats):
        now = time.monotonic()
        if now - _maint_notice.get(message.chat.id, 0) > 60:
            _maint_notice[message.chat.id] = now
            try:
                await message.reply_text(f"🛠 {smallcaps_title('bot is under maintenance, please try again later')}.")
            except Exception:
                pass
    raise StopPropagation


@bot.on_callback_query(filters.create(_off_blocker_cb), group=-1)
async def _blocked_cb_while_off(client, cq: CallbackQuery):
    await cq.answer(smallcaps_title("bot is currently unavailable"), show_alert=True)
    raise StopPropagation


def _btn(text: str, *, style: str = None, **kwargs) -> InlineKeyboardButton:
    """Telegram Bot API 9.4 colored inline button."""
    if style:
        style_map = {
            "primary": ButtonStyle.PRIMARY,
            "success": ButtonStyle.SUCCESS,
            "danger": ButtonStyle.DANGER,
        }
        return InlineKeyboardButton(
            text,
            style=style_map.get(style, ButtonStyle.PRIMARY),
            **kwargs,
        )
    # Default every inline button to Telegram Bot API 9.4 primary style.
    return InlineKeyboardButton(text, style=ButtonStyle.PRIMARY, **kwargs)


# Saare buttons ke colors green → blue → red ke chakkar mein ghoomte hain.
# Har button ka apna offset hota hai, isliye ek hi row mein alag-alag colors
# dikhte hain (screenshot jaisa) aur har tick par sab shift ho jaate hain.
_CYCLE_STYLES = ("success", "primary", "danger")


def _c(i: int, shift: int = 0) -> str:
    return _CYCLE_STYLES[(i + shift) % len(_CYCLE_STYLES)]


# Button icons — emoji nahi, screenshot jaise plain symbols. U+FE0E (text
# presentation selector) isliye lagaya hai taaki Telegram inhe emoji na banaye.
ICON_PLAY = "▷"
ICON_PAUSE = "II"
ICON_REPLAY = "↻"
ICON_SKIP = "▷|"
ICON_STOP = "□"


def _controls_keyboard(chat_id: int, elapsed_sec=None, total_sec=None, cycle_style: str = "success"):
    """Screenshot jaisa layout: progress bar → play/pause/repeat/skip/stop →
    -15s/15s+ → autoplay toggle → close. Har button ka color alag hota hai
    aur background task isko har 2s rotate karta rehta hai."""
    if cycle_style not in _CYCLE_STYLES:
        cycle_style = "success"
    base = _CYCLE_STYLES.index(cycle_style)

    rows = []
    if total_sec and total_sec > 0:
        from progress import render_button_bar
        rows.append([_btn(render_button_bar(elapsed_sec or 0, total_sec), callback_data="m_progress", style=_c(0, base))])
    rows.extend([
        [
            _btn(ICON_PLAY, callback_data="m_resume", style=_c(1, base)),
            _btn(ICON_PAUSE, callback_data="m_pause", style=_c(2, base)),
            _btn(ICON_REPLAY, callback_data="m_replay", style=_c(3, base)),
            _btn(ICON_SKIP, callback_data="m_skip", style=_c(4, base)),
            _btn(ICON_STOP, callback_data="m_stop", style=_c(5, base)),
        ],
        [
            _btn("-15s", callback_data="m_seek_back", style=_c(6, base)),
            _btn("15s+", callback_data="m_seek_fwd", style=_c(7, base)),
        ],
    ])

    autoplay_on = q.get_autoplay(chat_id)
    rows.append([
        _btn(
            f"♫ {smallcaps_title('autoplay')}: {smallcaps_title('enabled') if autoplay_on else smallcaps_title('disabled')}",
            callback_data="m_autoplay_toggle",
            style="success" if autoplay_on else "danger",
        )
    ])
    rows.append([_btn(smallcaps_title('close'), callback_data="m_close", style=_c(8, base))])
    return InlineKeyboardMarkup(rows)


def _start_keyboard(bot_username: str, shift: int = 0):
    """Add me | Help (color badalta rehta hai) | Updates+Support | Source."""
    return InlineKeyboardMarkup(
        [
            [
                _btn(
                    smallcaps('add me to your chat'),
                    url=f"https://t.me/{bot_username}?startgroup=true",
                    style="success",
                )
            ],
            [_btn(smallcaps('help and command'), callback_data="help_menu", style="primary")],
            [
                _btn(smallcaps('updates'), url=config.CHANNEL_URL, style="primary"),
                _btn(smallcaps('support'), url=config.SUPPORT_URL, style="success"),
            ],
            [_btn(smallcaps('source code'), url=config.SOURCE_URL, style="danger")],
        ]
    )


_HELP_BUTTONS = (
    ("admin", "help_admin", "primary"), ("auth", "help_auth", "success"), ("b-cast", "help_bcast", "primary"),
    ("play", "help_play", "success"), ("sudo", "help_sudo", "success"), ("restrict", "help_restrict", "success"),
    ("thumbnail", "help_thumbnail", "success"), ("start", "help_start", "primary"), ("autoplay", "help_autoplay", "success"),
)


def _help_keyboard(shift: int = 0):
    """3x3 category buttons + (manage | back) — fixed colors."""
    rows = []
    for r in range(3):
        rows.append([
            _btn(smallcaps_title(name), callback_data=cb, style=st)
            for name, cb, st in _HELP_BUTTONS[r * 3:r * 3 + 3]
        ])
    rows.append([
        _btn(smallcaps_title('manage'), callback_data="help_manage", style="danger"),
        _btn(smallcaps_title('back'), callback_data="back_to_start", style="primary"),
    ])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Start/Help buttons ka color-cycle — message bhejne ke baad thodi der tak
# (CYCLE_MAX_TICKS * CYCLE_INTERVAL sec) colors badalte rehte hain. Navigate
# karne (Help/Back dabane) par purana cycle ruk jaata hai.
# ---------------------------------------------------------------------------
CYCLE_INTERVAL = 3
CYCLE_MAX_TICKS = 20
_cycle_tasks: dict = {}


def _stop_cycle(chat_id: int, msg_id: int):
    task = _cycle_tasks.pop((chat_id, msg_id), None)
    if task and not task.done():
        task.cancel()


def _start_cycle(msg, builder):
    """`builder(shift)` -> InlineKeyboardMarkup. msg ke buttons ka color rotate karta hai."""
    if msg is None:
        return
    chat_id, msg_id = msg.chat.id, msg.id
    _stop_cycle(chat_id, msg_id)

    async def _runner():
        shift = 0
        try:
            for _ in range(CYCLE_MAX_TICKS):
                await asyncio.sleep(CYCLE_INTERVAL)
                shift = (shift + 1) % len(_CYCLE_STYLES)
                try:
                    await bot.edit_message_reply_markup(chat_id, msg_id, builder(shift))
                except Exception as e:
                    if "MESSAGE_NOT_MODIFIED" in str(e):
                        continue
                    return
        except asyncio.CancelledError:
            pass
        finally:
            _cycle_tasks.pop((chat_id, msg_id), None)

    _cycle_tasks[(chat_id, msg_id)] = asyncio.create_task(_runner())


def _help_category_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                _btn(smallcaps_title('back'), callback_data="help_menu", style="primary"),
                _btn(smallcaps_title('home'), callback_data="back_to_start", style="danger"),
            ]
        ]
    )


async def _send_welcome(chat_id: int, text: str, reply_markup, media: dict = None):
    """Diye gaye media (photo/video/gif) ke saath ya sirf text ke saath welcome bhejta hai.
    Agar owner ne /addvd se kuch set nahi kiya, to config.START_IMAGE_URL
    (agar set hai) fallback image ke roop mein use hoti hai."""
    if media:
        send_func = getattr(bot, _SEND_MEDIA_MAP_NAME.get(media["media_type"], "send_photo"))
        try:
            return await send_func(chat_id, media["file_id"], caption=text, reply_markup=reply_markup)
        except Exception as e:
            LOGGER.warning(f"Start media send fail, text fallback: {e}")
    elif config.START_IMAGE_URL:
        try:
            return await bot.send_photo(chat_id, config.START_IMAGE_URL, caption=text, reply_markup=reply_markup)
        except Exception as e:
            LOGGER.warning(f"START_IMAGE_URL send fail, text fallback: {e}")
    return await bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)


CAPTION_LIMIT = 1000  # Telegram caption limit 1024 hai, thoda margin


def _plain_len(text: str) -> int:
    return len(re.sub(r"<[^>]+>", "", text))


async def _get_user_dp(user_id: int):
    """User ki profile photo download karke in-memory file deta hai (nahi hai /
    privacy band hai to None). NOTE: profile photo ka file_id seedha send_photo
    mein kaam nahi karta (MEDIA_EMPTY aata hai), isliye download+upload karte hain."""
    try:
        async for photo in bot.get_chat_photos(user_id, limit=1):
            data = await bot.download_media(photo.file_id, in_memory=True)
            if data:
                data.name = "dp.jpg"
                return data
    except Exception as e:
        LOGGER.warning(f"DP fetch fail ({user_id}): {e}")
    return None


async def _edit_body(cq_message, text: str, reply_markup):
    """Callback pe message edit karta hai — media caption ho ya plain text.
    Agar media wale message ka text caption limit se bada hai (jaise help
    pages), to purana message hata kar naya text message bhejta hai.
    Final message return karta hai."""
    has_media = bool(cq_message.photo or cq_message.video or cq_message.animation)
    if has_media and _plain_len(text) > CAPTION_LIMIT:
        chat_id = cq_message.chat.id
        _stop_cycle(chat_id, cq_message.id)
        try:
            await cq_message.delete()
        except Exception:
            pass
        return await bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
    if has_media:
        return await cq_message.edit_caption(text, reply_markup=reply_markup)
    return await cq_message.edit_text(text, reply_markup=reply_markup)


HELP_TEXT = (
    blockquote(f"<b>🔮 {smallcaps_title('explore the complete command index below')}</b>")
    + "\n\n"
    + blockquote(f"<b>• {smallcaps_title('access expert technical guidance & real-time support')}</b>")
    + "\n\n"
    + blockquote(f"<b>• {smallcaps_title('execute all commands using standard prefix')} ➜ /</b>")
)

# ---------------------------------------------------------------------------
# Help category pages — har button click par upar wala message isi text se
# edit ho jaata hai (_help_category_keyboard: Back + Home).
# NOTE: In mein se zyadatar commands abhi sirf REFERENCE ke liye hain — inka
# asli backend (queue jump/move/shuffle, broadcast modes, channel play, sudo
# list, blacklist, logs/maintenance, stats, /eval /sh, etc.) is update mein
# implement nahi kiya gaya hai (bahut bada scope hai — agla batch bata do to
# priority ke hisaab se banata hoon). `/eval` aur `/sh` jaise arbitrary
# code/shell-execution commands maine jaanbojh kar implement nahi kiye — yeh
# bot ke liye ek security backdoor ban jaate hain, chahe "owner only" hi kyun
# na ho.
# ---------------------------------------------------------------------------
HELP_CATEGORY_TEXT = {
    "help_admin": blockquote(
        "⊚ ᴀᴅᴍɪɴ ᴄᴏᴍᴍᴀɴᴅs :\n\n"
        "💮 ᴊᴜsᴛ ᴀᴅᴅ ᴄ ᴀs ᴀ ᴘʀᴇғɪx ᴛᴏ ᴛʜᴇ ᴄᴏᴍᴍᴀɴᴅs ᴛᴏ ᴄᴏɴᴛʀᴏʟ ᴄʜᴀɴɴᴇʟ sᴛʀᴇᴀᴍɪɴɢ.\n\n"
        "➻ `/pause` : ʜᴀʟᴛ ᴛʜᴇ ᴄᴜʀʀᴇɴᴛʟʏ ᴀᴄᴛɪᴠᴇ sᴛʀᴇᴀᴍ.\n"
        "➻ `/resume` : ʀᴇsᴛᴀʀᴛ ᴛʜᴇ ᴘᴀᴜsᴇᴅ ᴘʟᴀʏʙᴀᴄᴋ.\n"
        "➻ `/skip` : ᴘʟᴀʏ ᴛʜᴇ ɴᴇxᴛ ᴛʀᴀᴄᴋ ɪɴ ᴛʜᴇ ǫᴜᴇᴜᴇ.\n"
        "➻ `/askip` : ᴘʟᴀʏ ᴛʜᴇ ɴᴇxᴛ ᴛʀᴀᴄᴋ ɪɴ ᴛʜᴇ ᴀᴜᴛᴏᴘʟᴀʏ ǫᴜᴇᴜᴇ.\n"
        "➻ `/stop` : ᴄᴇᴀsᴇ ᴛʜᴇ sᴛʀᴇᴀᴍ ᴀɴᴅ ᴇᴍᴘᴛʏ ǫᴜᴇᴜᴇ.\n"
        "➻ `/mute` : ᴍᴜᴛᴇ ᴘʟᴀʏʙᴀᴄᴋ ɪɴ ᴠɪᴅᴇᴏᴄʜᴀᴛ.\n"
        "➻ `/unmute` : ᴜɴᴍᴜᴛᴇ ᴘʟᴀʏʙᴀᴄᴋ ɪɴ ᴠɪᴅᴇᴏᴄʜᴀᴛ.\n"
        "(ʀᴇᴘʟʏ/ᴜsᴇʀɴᴀᴍᴇ ᴅᴇ ᴅᴏ ᴛᴏ ᴡᴏ ᴜsᴇʀ ɢʀᴏᴜᴘ ᴍᴇ ᴍᴜᴛᴇ ʜᴏɢᴀ)\n"
        "➻ `/queue` : ᴠɪᴇᴡ ᴛʜᴇ ʟɪsᴛ ᴏғ ᴜᴘᴄᴏᴍɪɴɢ ᴛʀᴀᴄᴋs.\n"
        "➻ `/jump [time]` : ᴊᴜᴍᴘ ᴛᴏ ᴀ ɢɪᴠᴇɴ ᴛɪᴍᴇ ɪɴ ᴛʀᴀᴄᴋ.\n"
        "➻ `/move [from] [to]` : ᴍᴏᴠᴇ ᴀ ǫᴜᴇᴜᴇᴅ ᴛʀᴀᴄᴋ ᴛᴏ ᴀɴᴏᴛʜᴇʀ ᴘᴏsɪᴛɪᴏɴ.\n"
        "➻ `/clear` : ᴄʟᴇᴀʀ ᴀʟʟ sᴏɴɢs ғʀᴏᴍ ǫᴜᴇᴜᴇ.\n"
        "➻ `/remove [no]` : ʀᴇᴍᴏᴠᴇ ᴀ sᴘᴇᴄɪғɪᴄ ᴛʀᴀᴄᴋ ғʀᴏᴍ ǫᴜᴇᴜᴇ.\n"
        "➻ `/loop [1-10]` : ʀᴇᴘᴇᴀᴛ ᴛʜᴇ ᴄᴜʀʀᴇɴᴛʟʏ ᴘʟᴀʏɪɴɢ sᴏɴɢ.\n"
        "➻ `/shuffle` : ʀᴀɴᴅᴏᴍɪᴢᴇ ᴛʜᴇ ǫᴜᴇᴜᴇᴅ ᴘʟᴀʏʟɪsᴛ.\n"
        "➻ `/seek [secs]` : ғᴏʀᴡᴀʀᴅ sᴇᴇᴋ ᴛᴏ ᴀ sᴘᴇᴄɪғɪᴄ ᴛɪᴍᴇ.\n"
        "➻ `/seekback [secs]` : ʀᴇᴠᴇʀsᴇ sᴇᴇᴋ ᴛᴏ ᴀ sᴘᴇᴄɪғɪᴄ ᴛɪᴍᴇ.\n"
        "➻ `/speed [0.5-2.0]` : ᴍᴏᴅɪғʏ ᴘʟᴀʏʙᴀᴄᴋ sᴘᴇᴇᴅ.\n"
        "➻ `/playmode` : ᴄᴏɴᴛʀᴏʟ ᴡʜᴏ ᴄᴀɴ ᴜsᴇ /ᴘʟᴀʏ ᴄᴍᴅ.\n"
        "➻ `/cmddelete` : ᴛᴏɢɢʟᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄ ᴅᴇʟᴇᴛɪᴏɴ ᴏғ ʙᴏᴛ ᴄᴏᴍᴍᴀɴᴅs."
        "\n\n⚠️ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴄᴀɴ ᴏɴʟʏ ʙᴇ ᴜsᴇᴅ ɪɴ ɢʀᴏᴜᴘs."
    ),
    "help_auth": blockquote(
        "⊚ ᴀᴜᴛʜ ᴜsᴇʀs :\n\n"
        "💮 ᴀᴜᴛʜ ᴜsᴇʀs ᴀʀᴇ ɢʀᴀɴᴛᴇᴅ ᴀᴅᴍɪɴ ᴘʀɪᴠɪʟᴇɢᴇs ᴡɪᴛʜᴏᴜᴛ ʀᴇǫᴜɪʀɪɴɢ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴ sᴛᴀᴛᴜs ᴛᴏ ᴍᴀɴᴀɢᴇ sᴛʀᴇᴀᴍs.\n\n"
        "➻ `/auth [user]` : ᴀᴅᴅ ᴜsᴇʀ ᴛᴏ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ʟɪsᴛ.\n"
        "➻ `/unauth [user]` : ʀᴇᴍᴏᴠᴇ ᴜsᴇʀ ғʀᴏᴍ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ʟɪsᴛ.\n"
        "➻ `/authlist` : ᴅɪsᴘʟᴀʏ ᴀʟʟ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴍᴇᴍʙᴇʀs."
        "\n\n⚠️ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴄᴀɴ ᴏɴʟʏ ʙᴇ ᴜsᴇᴅ ɪɴ ɢʀᴏᴜᴘs."
    ),
    "help_bcast": blockquote(
        "⊚ ʙʀᴏᴀᴅᴄᴀsᴛ ғᴇᴀᴛᴜʀᴇ [ᴏɴʟʏ ғᴏʀ sᴜᴅᴏᴇʀs] :\n\n"
        "`/broadcast [message or reply]` : sᴇɴᴅ ᴛᴏ ᴀʟʟ ᴄʜᴀᴛs.\n\n"
        "➻ ᴍᴏᴅᴇs :\n"
        "`-pin` : ᴩɪɴ ᴛʜᴇ ᴍᴇssᴀɢᴇ\n"
        "`-pinloud` : ᴩɪɴ + ɴᴏᴛɪғʏ ᴍᴇᴍʙᴇʀs\n"
        "`-user` : ʙʀᴏᴀᴅᴄᴀsᴛ ᴛᴏ sᴛᴀʀᴛᴇᴅ ᴜsᴇʀs\n"
        "`-assistant` : sᴇɴᴅ ᴠɪᴀ ᴀssɪsᴛᴀɴᴛ\n"
        "`-nobot` : ᴅᴏɴ'ᴛ sᴇɴᴅ ғʀᴏᴍ ʙᴏᴛ\n\n"
        "⊚ Example : `/broadcast -user -pin Testing broadcast`"
    ),
    "help_play": blockquote(
        "⊚ ᴘʟᴀʏ & ᴄʜᴀɴɴᴇʟ ᴘʟᴀʏ :\n\n"
        "➻ `/play` : sᴛᴀʀᴛ ᴀᴜᴅɪᴏ sᴛʀᴇᴀᴍɪɴɢ ɪɴ ᴄʜᴀᴛ.\n"
        "➻ `/playforce` : ɪɴsᴛᴀɴᴛ ᴘʟᴀʏ (ᴏᴠᴇʀʀɪᴅᴇs ǫᴜᴇᴜᴇ).\n\n"
        "⊚ ᴄʜᴀɴɴᴇʟ ᴘʟᴀʏ :\n\n"
        "➻ `/cplay` : ᴀᴜᴅɪᴏ sᴛʀᴇᴀᴍ ɪɴ ʟɪɴᴋᴇᴅ ᴄʜᴀɴɴᴇʟ.\n"
        "➻ `/cvplay` : sᴀᴍᴇ ᴀs /cplay (ᴀᴜᴅɪᴏ-ᴏɴʟʏ ғᴏʀ ɴᴏᴡ).\n"
        "➻ `/cplayforce` : ғᴏʀᴄᴇ sᴛʀᴇᴀᴍ ɪɴ ʟɪɴᴋᴇᴅ ᴄʜᴀɴɴᴇʟ.\n"
        "➻ `/channelplay [id]` : ᴄᴏɴɴᴇᴄᴛ ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴛʜɪs ɢʀᴏᴜᴘ."
        "\n\n⚠️ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴄᴀɴ ᴏɴʟʏ ʙᴇ ᴜsᴇᴅ ɪɴ ɢʀᴏᴜᴘs."
    ),
    "help_sudo": blockquote(
        "⊚ sʏsᴛᴇᴍ & sᴜᴅᴏ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ :\n\n"
        "💮 ᴍᴀɴᴀɢᴇ ʟᴏɢs, ᴀɴᴅ ᴀᴅᴍɪɴɪsᴛʀᴀᴛɪᴠᴇ ᴘʀɪᴠɪʟᴇɢᴇs ᴏғ ʏᴏᴜʀ ʙᴏᴛ.\n\n"
        "‣ ʙᴏᴛ ʟᴏɢs & ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ :\n\n"
        "➻ `/logs` : ɢᴇᴛ ʀᴇᴀʟ-ᴛɪᴍᴇ ʟᴏɢs ᴏғ ᴛʜᴇ ʙᴏᴛ.\n"
        "➻ `/logger` : [ᴇɴᴀʙʟᴇ/ᴅɪsᴀʙʟᴇ] ʙᴏᴛ ᴀᴄᴛɪᴠɪᴛʏ ʟᴏɢɢɪɴɢ.\n"
        "➻ `/maintenance` : [ᴇɴᴀʙʟᴇ/ᴅɪsᴀʙʟᴇ] ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ ᴍᴏᴅᴇ.\n\n"
        "⊚ ᴀᴄᴛɪᴠᴇ sᴛʀᴇᴀᴍs :\n\n"
        "➻ `/activevoice` : ᴠɪᴇᴡ ᴏɴɢᴏɪɴɢ ᴀᴜᴅɪᴏ sᴇssɪᴏɴs.\n"
        "➻ `/ac` : ᴠɪᴇᴡ ᴀʟʟ ᴄᴏᴍʙɪɴᴇᴅ ᴀᴄᴛɪᴠᴇ sᴛʀᴇᴀᴍs.\n\n"
        "‣ sᴜᴅᴏ ʟɪsᴛ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ :\n\n"
        "➻ `/sudo` : ᴀᴅᴅ ᴀ ɴᴇᴡ sᴜᴅᴏ ᴜsᴇʀ ᴛᴏ ʙᴏᴛ.\n"
        "➻ `/rmsudo` : ʀᴇᴍᴏᴠᴇ ᴀ ᴜsᴇʀ ғʀᴏᴍ sᴜᴅᴏ ʟɪsᴛ.\n"
        "➻ `/sudolist` : ᴄʜᴇᴄᴋ ᴀʟʟ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ sᴜᴅᴏ ᴜsᴇʀs.\n\n"
        "‣ ᴀssɪsᴛᴀɴᴛ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ [ᴏᴡɴᴇʀ] :\n\n"
        "➻ `/addassistant` : ᴀᴅᴅ ᴀ ɴᴇᴡ ᴀssɪsᴛᴀɴᴛ (ᴀᴘɪ ɪᴅ, ʜᴀsʜ, sᴛʀɪɴɢ sᴇssɪᴏɴ).\n"
        "➻ `/assistantlist` : ᴠɪᴇᴡ ᴀʟʟ ʀᴜɴɴɪɴɢ ᴀssɪsᴛᴀɴᴛs.\n"
        "➻ `/removeassistant [user id]` : ʀᴇᴍᴏᴠᴇ ᴀɴ ᴀssɪsᴛᴀɴᴛ.\n"
        "➻ `/pic` : sᴇᴛ sᴛᴀʀᴛ ɪᴍᴀɢᴇ ғʀᴏᴍ ᴀ ᴜʀʟ.\n\n"
        "❏ ᴛʜᴇsᴇ ᴄᴏᴍᴍᴀɴᴅs ᴀʀᴇ ʜɪɢʜʟʏ sᴇɴsɪᴛɪᴠᴇ, ᴜsᴇ ᴡɪᴛʜ ᴄᴀʀᴇ."
    ),
    "help_restrict": blockquote(
        "⊚ ʙʟᴀᴄᴋʟɪsᴛ & ɢʟᴏʙᴀʟ ʙᴀɴ :\n\n"
        "💮 ʀᴇsᴛʀɪᴄᴛ sᴘᴇᴄɪғɪᴄ ᴄʜᴀᴛs ᴏʀ ᴜsᴇʀs ғʀᴏᴍ ᴜsɪɴɢ ᴛʜᴇ ʙᴏᴛ ɢʟᴏʙᴀʟʟʏ.\n\n"
        "‣ ᴄʜᴀᴛ ʙʟᴀᴄᴋʟɪsᴛ :\n\n"
        "➻ `/blacklistchat [chat id]` : ʙᴀɴ ᴀ ᴄʜᴀᴛ ғʀᴏᴍ ᴜsɪɴɢ ʙᴏᴛ.\n"
        "➻ `/whitelistchat [chat id]` : ᴜɴʙᴀɴ ᴀ ʙʟᴀᴄᴋʟɪsᴛᴇᴅ ᴄʜᴀᴛ.\n"
        "➻ `/blacklistedchat` : sʜᴏᴡ ᴀʟʟ ʙʟᴀᴄᴋʟɪsᴛᴇᴅ ɢʀᴏᴜᴘs.\n\n"
        "‣ ᴜsᴇʀ ʙʟᴏᴄᴋ :\n\n"
        "➻ `/block [username/id]` : sᴛᴏᴘ ᴜsᴇʀ ғʀᴏᴍ ᴜsɪɴɢ ʙᴏᴛ.\n"
        "➻ `/unblock [username/id]` : ᴀʟʟᴏᴡ ᴛʜᴇ ʙʟᴏᴄᴋᴇᴅ ᴜsᴇʀ.\n"
        "➻ `/blockedusers` : ᴠɪᴇᴡ ᴛʜᴇ ʟɪsᴛ ᴏғ ᴀʟʟ ʙʟᴏᴄᴋᴇᴅ ᴜsᴇʀs."
    ),
    "help_thumbnail": blockquote(
        "⊚ ᴛʜᴜᴍʙɴᴀɪʟ sᴇᴛᴛɪɴɢs :\n\n"
        "➻ `/thumb` : ᴍᴀɴᴀɢᴇ sᴛʀᴇᴀᴍ ᴠɪsᴜᴀʟ sᴇᴛᴛɪɴɢs.\n\n"
        "⊚ ᴡʜᴀᴛ ɪs ᴛʜᴜᴍʙɴᴀɪʟ ᴛᴏɢɢʟᴇ?\n\n"
        "💮 ᴛᴏɢɢʟᴇ ᴀʟʙᴜᴍ ᴀʀᴛ ᴅɪsᴘʟᴀʏ. ᴅɪsᴀʙʟɪɴɢ ɪᴛ ʀᴇᴅᴜᴄᴇs ᴅᴀᴛᴀ ʟᴏᴀᴅ ᴀɴᴅ ᴍᴀɪɴᴛᴀɪɴs ᴀ ᴍɪɴɪᴍᴀʟɪsᴛ ᴄʜᴀᴛ ʟᴏᴏᴋ.\n\n"
        "๏ ᴘᴀɴᴇʟ ᴏᴘᴛɪᴏɴs :\n\n"
        "• ᴇɴᴀʙʟᴇ : ᴅɪsᴘʟᴀʏ ᴀʀᴛᴡᴏʀᴋ ᴡɪᴛʜ sᴛʀᴇᴀᴍ ɪɴғᴏ.\n"
        "• ᴅɪsᴀʙʟᴇ : sᴇɴᴅ ᴘᴜʀᴇ ᴛᴇxᴛ-ʙᴀsᴇᴅ ᴜᴘᴅᴀᴛᴇs.\n"
        "• ᴄʟᴏsᴇ : ᴅɪsᴍɪss ᴛʜᴇ ᴄᴏɴғɪɢᴜʀᴀᴛɪᴏɴ.\n\n"
        "๏ ɴᴏᴛᴇ : sᴇᴛᴛɪɴɢs ᴀʀᴇ sᴀᴠᴇᴅ ɪɴᴅɪᴠɪᴅᴜᴀʟʟʏ ᴘᴇʀ ᴄʜᴀᴛ."
        "\n\n⚠️ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴄᴀɴ ᴏɴʟʏ ʙᴇ ᴜsᴇᴅ ɪɴ ɢʀᴏᴜᴘs."
    ),
    "help_start": blockquote(
        "⊚ ʙᴀsɪᴄ ᴄᴏᴍᴍᴀɴᴅs :\n\n"
        "➻ `/start` : ɪɴɪᴛɪᴀʟɪᴢᴇ ᴛʜᴇ ᴍᴜsɪᴄ sᴇʀᴠɪᴄᴇ.\n"
        "➻ `/help` : ᴀᴄᴄᴇss ᴛʜᴇ ᴄᴏᴍᴍᴀɴᴅ ɢᴜɪᴅᴇʟɪɴᴇs.\n"
        "➻ `/ping` : ᴍᴇᴀsᴜʀᴇ sʏsᴛᴇᴍ ʟᴀᴛᴇɴᴄʏ ᴀɴᴅ ᴘɪɴɢ.\n"
        "➻ `/position` : sʜᴏᴡ ᴄᴜʀʀᴇɴᴛ ᴛʀᴀᴄᴋ's ᴛɪᴍᴇsᴛᴀᴍᴘ.\n"
        "➻ `/reload` : ʀᴇғʀᴇsʜ ᴀᴅᴍɪɴ ᴅᴀᴛᴀ ᴄᴀᴄʜᴇ.\n"
        "➻ `/settings` : ᴀᴅᴊᴜsᴛ ᴄᴏɴғɪɢᴜʀᴀᴛɪᴏɴ ᴏᴘᴛɪᴏɴs.\n"
        "➻ `/id` : sʜᴏᴡ ʏᴏᴜʀ/ɢʀᴏᴜᴘ ɪᴅ.\n"
        "➻ `/bug` : ʀᴇᴘᴏʀᴛ ᴀɴ ɪssᴜᴇ ᴏʀ ᴘʀᴏʙʟᴇᴍ.\n"
        "➻ `/json` : sʜᴏᴡ ᴍᴇssᴀɢᴇ ᴊsᴏɴ sᴛʀᴜᴄᴛᴜʀᴇ.\n"
        "➻ `/sudolist` : ᴠɪᴇᴡ ᴛʜᴇ ʟɪsᴛ ᴏғ sᴜᴅᴏ ᴀᴅᴍɪɴs.\n"
        "➻ `/stats` : ᴠɪᴇᴡ ᴄᴏᴍᴘʀᴇʜᴇɴsɪᴠᴇ ʙᴏᴛ ᴍᴇᴛʀɪᴄs."
    ),
    "help_autoplay": blockquote(
        "⊚ ᴀᴜᴛᴏᴘʟᴀʏ ᴄᴏɴᴛʀᴏʟ :\n\n"
        "➻ `/autoplayon` `/autoplayoff` : ᴛᴏɢɢʟᴇ ᴛʜᴇ ᴀᴜᴛᴏᴘʟᴀʏ sᴇᴛᴛɪɴɢs.\n"
        "➻ `/askip` : ᴘʟᴀʏ ᴛʜᴇ ɴᴇxᴛ ᴛʀᴀᴄᴋ ɪɴ ᴛʜᴇ ᴀᴜᴛᴏᴘʟᴀʏ ǫᴜᴇᴜᴇ.\n\n"
        "⊚ ᴡʜᴀᴛ ɪs ᴀᴜᴛᴏᴘʟᴀʏ?\n\n"
        "💮 ᴀᴜᴛᴏᴘʟᴀʏ sᴇᴀᴍʟᴇssʟʏ ᴀᴅᴅs ʀᴇʟᴀᴛᴇᴅ ᴛʀᴀᴄᴋs ᴛᴏ ᴛʜᴇ ǫᴜᴇᴜᴇ ʙᴀsᴇᴅ ᴏɴ ʏᴏᴜʀ ᴄᴜʀʀᴇɴᴛ ᴘʟᴀʏʟɪsᴛ ғᴏʀ ɴᴏɴ-sᴛᴏᴘ ᴍᴜsɪᴄ.\n\n"
        "๏ ᴘᴀɴᴇʟ ᴏᴘᴛɪᴏɴs :\n\n"
        "• ᴇɴᴀʙʟᴇ : ᴀᴄᴛɪᴠᴀᴛᴇ sᴍᴀʀᴛ ʀᴇᴄᴏᴍᴍᴇɴᴅᴀᴛɪᴏɴs.\n"
        "• ᴅɪsᴀʙʟᴇ : ᴅᴇᴀᴄᴛɪᴠᴀᴛᴇ sᴍᴀʀᴛ ʀᴇᴄᴏᴍᴍᴇɴᴅᴀᴛɪᴏɴs.\n"
        "• ᴄʟᴏsᴇ : ᴇxɪᴛ ᴛʜᴇ ᴄᴏɴᴛʀᴏʟ ᴘᴀɴᴇʟ.\n\n"
        "๏ ɴᴏᴛᴇ : ʀᴇǫᴜɪʀᴇs ᴀᴛ ʟᴇᴀsᴛ ᴏɴᴇ ᴀᴄᴛɪᴠᴇ sᴏɴɢ ᴛᴏ ғᴜɴᴄᴛɪᴏɴ."
        "\n\n⚠️ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴄᴀɴ ᴏɴʟʏ ʙᴇ ᴜsᴇᴅ ɪɴ ɢʀᴏᴜᴘs."
    ),
}


HELP_CATEGORY_TEXT["help_manage"] = blockquote(
    "⊚ ɢʀᴏᴜᴘ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ :\n\n"
    "💮 ʀᴇᴘʟʏ ᴋᴀʀᴋᴇ ʏᴀ [ᴜsᴇʀɴᴀᴍᴇ/ɪᴅ] ᴅᴇᴋᴀʀ ᴅᴏɴᴏ ᴛᴀʀᴀʜ ᴄʜᴀʟᴛᴇ ʜᴀɪɴ.\n\n"
    "➻ `/promote` : ᴍᴀᴋᴇ ᴀ ᴜsᴇʀ ᴀᴅᴍɪɴ.\n"
    "➻ `/demote` : ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ ʀɪɢʜᴛs.\n"
    "➻ `/ban` : ʙᴀɴ ᴀ ᴜsᴇʀ ғʀᴏᴍ ɢʀᴏᴜᴘ.\n"
    "➻ `/unban` : ᴜɴʙᴀɴ ᴀ ᴜsᴇʀ.\n"
    "➻ `/kick` : ʀᴇᴍᴏᴠᴇ ᴀ ᴜsᴇʀ (ᴄᴀɴ ʀᴇᴊᴏɪɴ).\n"
    "➻ `/mute [user]` : ᴍᴜᴛᴇ ᴀ ᴜsᴇʀ ɪɴ ɢʀᴏᴜᴘ.\n"
    "➻ `/unmute [user]` : ᴜɴᴍᴜᴛᴇ ᴀ ᴜsᴇʀ.\n\n"
    "⚠️ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴄᴀɴ ᴏɴʟʏ ʙᴇ ᴜsᴇᴅ ɪɴ ɢʀᴏᴜᴘs."
)

_STY = str.maketrans({"ᴀ": "ᴧ", "ɴ": "η", "ᴏ": "σ", "ᴇ": "є"})
HELP_CATEGORY_TEXT = {k: v.translate(_STY) for k, v in HELP_CATEGORY_TEXT.items()}


def _welcome_text(user_name: str, user_id: int, bot_name: str, bot_username: str) -> str:
    user_tag = f'<a href="tg://user?id={user_id}">{esc(smallcaps_title(user_name))}</a>'
    bot_tag = f'<a href="https://t.me/{bot_username}"> 𝘼𝙣𝙖𝙣𝙮𝙖 ✘ 𝙈𝙪𝙨𝙞𝙘 </a>'

    greeting = blockquote(f"<b>💐 Gʀєєᴛɪηɢs {user_tag} 🥀</b>")
    details = blockquote(
        f"<b>💮 Yσυ Aʀє Usɪηɢ {bot_tag} : "
        f"Tʜє Uʟᴛɪϻᴧᴛє Dєsᴛɪηᴧᴛɪση Fσʀ Hɪɢʜ Qυᴧʟɪᴛʏ Sᴛʀєᴧϻɪηɢ.\n\n"
        f"● Bυɪʟᴅ : V2.0 Stable.\n"
        f"● Oυᴛᴩυᴛ : Hi-Res Audio.\n"
        f"● Lᴧᴛєηᴄʏ : Zero Delay.\n\n"
        f"🎋 Cʟɪᴄᴋ Hєʟᴩ Tσ Sєє Aʟʟ Aᴠᴧɪʟᴧʙʟє Cσϻϻᴧηᴅs.</b>"
    )
    return f"{greeting}\n\n{details}"


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("start"))
async def start_cmd(client, message: Message):
    await db.add_user(message.from_user.id)
    me = await bot.get_me()

    # Ab group aur private dono jagah wahi ek greeting-style start message
    # jaata hai (purana alag "group start" wala version hata diya gaya hai).
    text = _welcome_text(message.from_user.first_name, message.from_user.id, me.first_name, me.username)

    if message.chat.type != "private":
        await db.add_chat(message.chat.id)
        media = await db.get_group_start_media()
    else:
        # DM mein start karne wale ki apni profile photo ke saath welcome jaata
        # hai. DP nahi hai/hidden hai to owner ka /addvd media (ya default) use hota hai.
        dp = await _get_user_dp(message.from_user.id)
        media = {"file_id": dp, "media_type": "photo"} if dp else await db.get_start_media()

    await _send_welcome(message.chat.id, text, _start_keyboard(me.username), media)

    # User ka /start wala msg turant delete — sirf welcome card hi dikhe.
    try:
        await message.delete()
    except Exception as e:
        LOGGER.warning(f"/start msg delete fail: {e}")

    # Owner ko batao ki kisne bot use kiya (private chat mein)
    if message.chat.type == "private" and config.OWNER_ID and message.from_user.id != config.OWNER_ID:
        try:
            await bot.send_message(
                config.OWNER_ID,
                f"👤 {smallcaps_title('bot used by')}:\n"
                f"{smallcaps_title('name')}: {message.from_user.first_name}\n"
                f"{smallcaps_title('username')}: @{message.from_user.username}\n"
                f"{smallcaps_title('id')}: `{message.from_user.id}`",
            )
        except Exception as e:
            LOGGER.warning(f"Owner notify fail: {e}")


@bot.on_message(filters.command("help"))
async def help_cmd(client, message: Message):
    await message.reply_text(HELP_TEXT, reply_markup=_help_keyboard(), disable_web_page_preview=True)


@bot.on_callback_query(filters.regex("^help_menu$"))
async def help_menu_cb(client, cq: CallbackQuery):
    await cq.answer()
    _stop_cycle(cq.message.chat.id, cq.message.id)
    await _edit_body(cq.message, HELP_TEXT, _help_keyboard())


@bot.on_callback_query(filters.regex("^help_(admin|auth|bcast|play|sudo|restrict|thumbnail|start|autoplay|manage)$"))
async def help_category_cb(client, cq: CallbackQuery):
    await cq.answer()
    text = HELP_CATEGORY_TEXT.get(cq.data)
    if not text:
        return
    _stop_cycle(cq.message.chat.id, cq.message.id)
    await _edit_body(cq.message, text, _help_category_keyboard())


@bot.on_callback_query(filters.regex("^back_to_start$"))
async def back_to_start_cb(client, cq: CallbackQuery):
    await cq.answer()
    me = await bot.get_me()
    text = _welcome_text(cq.from_user.first_name, cq.from_user.id, me.first_name, me.username)
    msg = cq.message
    _stop_cycle(msg.chat.id, msg.id)
    has_media = bool(msg.photo or msg.video or msg.animation)

    if has_media:
        await _edit_body(msg, text, _start_keyboard(me.username))
    elif msg.chat.type == "private":
        # Help page text message tha (media nahi) — welcome ko DP ke saath dobara bhejo
        dp = await _get_user_dp(cq.from_user.id)
        media = {"file_id": dp, "media_type": "photo"} if dp else await db.get_start_media()
        try:
            await msg.delete()
        except Exception:
            pass
        await _send_welcome(msg.chat.id, text, _start_keyboard(me.username), media)
    else:
        await _edit_body(msg, text, _start_keyboard(me.username))


# ---------------------------------------------------------------------------
# Bot ko group mein add kiya jaana
# ---------------------------------------------------------------------------
@bot.on_message(filters.new_chat_members)
async def added_to_group(client, message: Message):
    me = await bot.get_me()
    if not any(u.id == me.id for u in message.new_chat_members):
        return

    await db.add_chat(message.chat.id)
    adder = message.from_user.first_name if message.from_user else "there"
    bot_tag = f"[{fancy_italic(me.first_name)}](https://t.me/{me.username})"

    await message.reply_text(
        f"❖ {smallcaps_title('hey')} {adder}..!! 🥀\n"
        f"» {smallcaps_title('thanks for adding')} {bot_tag}!\n\n"
        f"» {bot_tag} {smallcaps_title('can now play songs in this chat')}.\n\n"
        f"⌾ {smallcaps_title('play music')} : /play\n"
        f"⌾ {smallcaps_title('help & cmds')} : /help\n\n"
        f"{FOOTER_LINE}",
        reply_markup=_start_keyboard(me.username),
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /play — private chat mein bheja gaya to bata do ki yeh group command hai
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("play") & filters.private)
async def play_private_command(client, message: Message):
    me = await bot.get_me()
    await message.reply_text(
        f"❌ {smallcaps_title('this is a group command')}!\n\n"
        f"» {smallcaps_title('add me to your group, start the voice chat there and turn the vc live on, then use')} "
        f"`/play` {smallcaps_title('in the group')}.",
        reply_markup=_start_keyboard(me.username),
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /play — sabke liye khula hai
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("play") & filters.group)
async def play_command(client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"❌ {smallcaps_title('give me a song name too')}!\nExample: `/play Shape of You`"
        )
    await _do_play(message, message.text.split(None, 1)[1])


async def _do_play(message: Message, query: str, force: bool = False, target_chat: int = None):
    """/play /playforce /cplay /cplayforce sab yahi use karte hain.
    target_chat = channel id (cplay) — warna isi group mein play hota hai."""
    notify = message.chat.id                 # status/error messages yahin jaate hain
    chat_id = target_chat or notify          # VC + queue + Now Playing card yahin
    requester = message.from_user.first_name if message.from_user else "Someone"
    requester_id = message.from_user.id if message.from_user else None

    # /playmode: sirf admins -> normal member /play nahi chala sakta
    if (store.get_setting(notify, "playmode", "everyone") == "admins"
            and requester_id and not await _is_group_admin(bot, notify, requester_id)):
        return await message.reply_text(
            f"🔒 {smallcaps_title('only admins can use play in this chat right now')}."
        )

    # User ka /play command wala msg turant delete — taaki kisi aur group
    # member ko pata na chale isne kaunsa gaana request kiya tha.
    try:
        await message.delete()
    except Exception as e:
        LOGGER.warning(f"Play command msg delete fail: {e}")

    # Khali (idle) assistant chuno — jo kisi aur group mein baj raha ho use skip karke
    # agla free assistant is group ko milta hai.
    asst = pool.pick_for_chat(chat_id)
    joined, reason = await ensure_assistant_in_chat(chat_id, asst.client)
    if not joined:
        if not q.is_playing(chat_id):
            pool.release(chat_id)
        if reason == "flood_wait":
            return await bot.send_message(notify, ASSISTANT_FLOOD_TEXT)
        return await bot.send_message(notify, _assistant_not_joined_text(asst.username))

    # Pehle sirf ek single emoji jaata hai...
    status = await bot.send_message(notify, random_processing_emoji())
    # ...uske turant baad, agar owner ne /processingon kiya hai, to usi emoji
    # ke niche ek random text (bot ki smallcaps style mein) jud jaata hai.
    if botstate.is_processing_text_enabled():
        try:
            await status.edit_text(processing_caption(status.text))
        except Exception as e:
            LOGGER.warning(f"Processing caption edit fail: {e}")

    track = await search_track(query)
    if not track:
        if not q.is_playing(chat_id):
            pool.release(chat_id)
        return await status.edit_text(f"❌ {smallcaps_title('could not find anything, try a different name')}.")

    try:
        stream_url = await get_stream_url(track["id"])
    except Exception as e:
        LOGGER.error(f"Stream URL error: {e}")
        if not q.is_playing(chat_id):
            pool.release(chat_id)
        return await status.edit_text(
            f"❌ {smallcaps_title('could not load this track, try again shortly or send another song')}."
        )

    track["stream_url"] = stream_url
    track["requested_by"] = requester
    track["requested_by_id"] = requester_id

    # Agar pehle se kuch baj raha hai
    if q.is_playing(chat_id):
        if force:
            # /playforce — queue ke aage daalo aur turant isi par jump
            q.get_queue(chat_id).insert(0, track)
            await status.delete()
            return await _play_next_or_autoplay(chat_id)

        position = q.push(chat_id, track)
        await status.delete()
        queue_title = track["title"].split("|")
        queue_main = smallcaps_title(esc(queue_title[0].strip()))
        queue_lines = [f"╰┈➤ {smallcaps_title(esc(x.strip()))}" for x in queue_title[1:] if x.strip()]
        requester_tag = f"[{smallcaps_title(requester)}](tg://user?id={requester_id})"

        header = blockquote(f"❖ {smallcaps_title('queued to play')}..!! ✦")
        body_lines = [f"» 『 #{position} • {queue_main} 』"] + queue_lines
        body = expandable_blockquote("\n".join(body_lines))
        meta = expandable_blockquote(
            f"⌾ {smallcaps_title('duration')} : {esc(track['duration'])}\n"
            f"⌾ {smallcaps_title('requested by')} : {requester_tag}"
        )
        queue_text = f"{header}\n\n{body}\n\n{meta}"

        await bot.send_message(
            notify,
            queue_text,
            reply_markup=InlineKeyboardMarkup([[
                _btn(smallcaps_title('add me to your chat'),
                     url=f"https://t.me/{(await bot.get_me()).username}?startgroup=true", style="success")
            ]]),
            disable_web_page_preview=True,
        )
        return

    await status.delete()
    await _start_playing(chat_id, track)


async def _start_playing(chat_id: int, track: dict):
    """VC join/change karke track play karta hai aur Now Playing card bhejta hai.
    (User ka original /play msg turant delete ho jaata hai, isliye yahan
    reply ke bajaye seedha chat_id par bheja jaata hai.)"""
    try:
        try:
            await _call(chat_id).join_group_call(chat_id, await _piped(chat_id, track))
        except NoActiveGroupCall:
            # Koi voice chat live nahi hai — manually maangne ke bajaye khud
            # shuru karne ki koshish karte hain, phir dobara join try karo.
            started = await _auto_start_group_call(chat_id)
            if started:
                await asyncio.sleep(1.5)
                try:
                    await _call(chat_id).join_group_call(chat_id, await _piped(chat_id, track))
                    started = "joined"
                except Exception as e3:
                    LOGGER.warning(f"Auto-start ke baad join fail: {e3}")
                    started = False

            if started != "joined":
                return await bot.send_message(
                    chat_id,
                    f"❌ **{smallcaps_title('voice chat is not active')}!**\n\n"
                    f"{smallcaps_title('start a voice chat in the group first')}:\n"
                    "Group Settings → Voice Chat → Start Voice Chat\n\n"
                    f"{smallcaps_title('then send')} `/play` {smallcaps_title('again')}.",
                )
        except Exception as e:
            LOGGER.warning(f"join_group_call fail: {e!r}", exc_info=True)
            if _is_peer_error(e):
                await _refresh_assistant_peers(chat_id)
            try:
                await _call(chat_id).change_stream(chat_id, await _piped(chat_id, track))
            except Exception as e2:
                LOGGER.error(f"Play error: {e2!r}", exc_info=True)
                return await bot.send_message(
                    chat_id,
                    f"❌ **{smallcaps_title('could not play it')}**\n\n"
                    f"{smallcaps_title('check whether the voice chat is active, then try again')}.",
                )

        q.set_now_playing(chat_id, track)
        q.remember_played(chat_id, track.get("id"))
        _a = pool.get_for_chat(chat_id)
        asyncio.create_task(store.log_event(
            f"🎵 Now playing in {chat_id}: {track.get('title')} | by {track.get('requested_by')} | assistant: {_a.name}"
        ))
        await _send_now_playing(chat_id, track)

    except Exception as e:
        LOGGER.error(f"_start_playing fatal error: {e}")
        await bot.send_message(chat_id, f"❌ {smallcaps_title('something went wrong, try again')}.")


def _now_playing_caption(track: dict) -> str:
    parts = [x.strip() for x in str(track.get("title", "")).split("|") if x.strip()]
    main_title = smallcaps_title(esc(parts[0])) if parts else smallcaps_title("unknown")
    song_lines = [f"» 『 {main_title} 』"]
    for part in parts[1:]:
        song_lines.append(f"╰┈➤ {smallcaps_title(esc(part))}")

    requester = track.get("requested_by", "Unknown")
    requester_id = track.get("requested_by_id")
    if requester_id:
        requester_tag = f"[{smallcaps_title(requester)}](tg://user?id={requester_id})"
    else:
        requester_tag = smallcaps_title(requester)

    header = blockquote(f"❖ {smallcaps_title('now playing')}..!! ✦")
    body = expandable_blockquote("\n".join(song_lines))
    meta = expandable_blockquote(
        f"⌾ {smallcaps_title('duration')} : {esc(track.get('duration', '0:00'))}\n"
        f"⌾ {smallcaps_title('by')} : {requester_tag}"
    )
    return f"{header}\n\n{body}\n\n{meta}"


async def _send_now_playing(chat_id: int, track: dict, message: Message = None, edit_message: Message = None):
    """
    Now Playing card bhejta hai. Agar `edit_message` diya gaya hai (jaise skip
    button se), to naya message bhejne/purana delete karne ke bajaye wahi
    message in-place update ho jaata hai — isse card kabhi "gayab" nahi hota,
    bas apne aap refresh ho jaata hai.
    """
    caption = _now_playing_caption(track)
    thumb_on = store.get_setting(chat_id, "thumb", True)
    card = await generate_now_playing_card(track.get("thumbnail"), track["title"], track["duration"]) if thumb_on else None
    total_sec = duration_to_seconds(track.get("duration"))
    markup = _controls_keyboard(chat_id, 0, total_sec, "success")
    media = (card or track.get("thumbnail")) if thumb_on else None

    sent = None

    if edit_message is not None:
        try:
            if media:
                sent = await edit_message.edit_media(InputMediaPhoto(media, caption=caption), reply_markup=markup)
            else:
                sent = await edit_message.edit_text(caption, reply_markup=markup)
        except Exception as e:
            LOGGER.warning(f"Now playing in-place edit fail, naya message bhej rahe hain: {e}")

    if sent is None:
        try:
            if media:
                sent = await bot.send_photo(chat_id, media, caption=caption, reply_markup=markup)
            elif message is not None:
                sent = await message.reply_text(caption, reply_markup=markup, disable_web_page_preview=True)
            else:
                sent = await bot.send_message(chat_id, caption, reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            LOGGER.warning(f"Now playing card send fail: {e}")
            if message is not None:
                sent = await message.reply_text(caption, reply_markup=markup, disable_web_page_preview=True)
            else:
                sent = await bot.send_message(chat_id, caption, reply_markup=markup, disable_web_page_preview=True)

    # 🎚️ Live progress bar shuru — gaana ke saath 00:00 se duration tak khud
    # aage badhta rahega (button ke andar), jaise screenshot mein dikha tha.
    if sent is not None:
        total_sec = duration_to_seconds(track.get("duration"))
        progress.start(chat_id, track["id"])
        progress.start_updater(
            chat_id, sent,
            lambda: _now_playing_caption(track),
            lambda el, tot, style: _controls_keyboard(chat_id, el, tot, style),
            track["id"], total_sec,
        )

    return sent


# ---------------------------------------------------------------------------
# Stream khatam hone par queue se agla gaana
# ---------------------------------------------------------------------------
# chat_id -> (video_id, retry_count) — gaana beech mein hi ruk jaaye to wahin se
# dobara chalane ki koshish (assistant ke "join karke nikal jaane" wali problem).
_early_end: dict = {}


async def on_stream_end(client, update):
    chat_id = update.chat_id
    track = q.get_now_playing(chat_id)

    if track:
        # /loop — gaana dobara chalao jab tak loop count bacha hai
        loops_left = q.get_loop(chat_id)
        if loops_left > 0:
            q.set_loop(chat_id, loops_left - 1)
            try:
                await _call(chat_id).change_stream(chat_id, await _piped(chat_id, track))
                progress.replay(chat_id)
                _early_end.pop(chat_id, None)
                return
            except Exception as e:
                LOGGER.warning(f"Loop replay fail: {e!r}")

        total = duration_to_seconds(track.get("duration"))
        el = progress.elapsed(chat_id)
        LOGGER.info(f"Stream end: chat={chat_id} elapsed={int(el)}s total={total}s")

        vid = track.get("id")
        rec = _early_end.get(chat_id)
        tries = rec[1] if rec and rec[0] == vid else 0

        # Gaana abhi poora nahi hua (kam se kam 10s baaki) -> stream beech mein
        # ruk gayi. VC chhodne ya agle gaane par jaane ke bajaye wahin se resume.
        # (speed != 1x par duration alag hoti hai, isliye tab yeh guard off rehta hai.)
        if (total > 20 and 2 < el < total - 10 and tries < 3
                and q.get_state(chat_id) != "paused" and q.get_speed(chat_id) == 1.0):
            _early_end[chat_id] = (vid, tries + 1)
            try:
                await _call(chat_id).change_stream(chat_id, await _piped(chat_id, track, el))
                LOGGER.info(f"Stream early-end recover try {tries + 1} at {int(el)}s")
                return
            except Exception as e:
                LOGGER.warning(f"Early-end recover fail: {e!r}")

    _early_end.pop(chat_id, None)
    await _play_next_or_autoplay(chat_id, silent_empty=True)


# Har assistant (PyTgCalls) par yahi handler lagta hai
pool.set_stream_end_handler(on_stream_end)


# ---------------------------------------------------------------------------
# /skip /pause /resume /stop — sirf group admin/owner
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("skip") & filters.group)
async def skip_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    await _play_next_or_autoplay(message.chat.id, reply_message=message)


@bot.on_message(filters.command("pause") & filters.group)
async def pause_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await _call(message.chat.id).pause_stream(message.chat.id)
        q.set_state(message.chat.id, "paused")
        progress.pause(message.chat.id)
        await message.reply_text(f"⏸ {smallcaps_title('paused')}.")
    except Exception as e:
        await message.reply_text(f"❌ {e}")


@bot.on_message(filters.command("resume") & filters.group)
async def resume_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await _call(message.chat.id).resume_stream(message.chat.id)
        q.set_state(message.chat.id, "playing")
        progress.resume(message.chat.id)
        await message.reply_text(f"▶️ {smallcaps_title('resumed')}.")
    except Exception as e:
        await message.reply_text(f"❌ {e}")


@bot.on_message(filters.command(["stop", "end"]) & filters.group)
async def stop_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await _call(message.chat.id).leave_group_call(message.chat.id)
    except Exception:
        pass
    q.clear(message.chat.id)
    progress.clear(message.chat.id)
    pool.release(message.chat.id)
    await message.reply_text(f"⏹️ {smallcaps_title('left the voice chat')}.")


@bot.on_message(filters.command("autoplayon") & filters.group)
async def autoplayon_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    q.set_autoplay(message.chat.id, True)
    await message.reply_text(f"✅ {smallcaps_title('autoplay turned on')} — {smallcaps_title('a related song will play after each track')}.")


@bot.on_message(filters.command("autoplayoff") & filters.group)
async def autoplayoff_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    q.set_autoplay(message.chat.id, False)
    await message.reply_text(f"🔴 {smallcaps_title('autoplay turned off')}.")


# ---------------------------------------------------------------------------
# /reload — sirf group admin/owner. Check karta hai bot khud admin hai ya nahi.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("reload") & filters.group)
async def reload_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)

    me = await bot.get_me()
    try:
        bot_member = await client.get_chat_member(message.chat.id, me.id)
        is_bot_admin = bot_member.status in ADMIN_STATUSES
    except Exception as e:
        LOGGER.warning(f"Reload admin-check fail: {e}")
        is_bot_admin = False

    if is_bot_admin:
        await message.reply_text(f"✅ {smallcaps_title('reloaded successfully')}.")
    else:
        await message.reply_text(
            f"❌ {smallcaps_title('make me a group admin first, then run')} `/reload` {smallcaps_title('again')}."
        )


async def _play_next_or_autoplay(chat_id: int, edit_message: Message = None, reply_message: Message = None, silent_empty: bool = False, force_related: bool = False):
    """Queue mein agla track ho to wahi play karta hai. Nahi ho aur autoplay ON
    hai to khud ek related track dhoond kar play karta hai. Dono hi na milein
    to VC chhod deta hai. Skip button, /skip command, aur track khatam hone
    (on_stream_end) — teeno yahi function use karte hain."""
    q.set_loop(chat_id, 0)  # skip/next par loop band
    next_track = None if force_related else q.pop_next(chat_id)

    # NOTE: Autoplay toggle button ab sirf UI/status ke liye hai — owner ke
    # faisle se asli auto-continue (related track) disable kar diya gaya hai.
    # Neeche wala condition isliye hamesha False milta hai; button abhi bhi
    # ON/OFF dikhayega aur color/status toggle karega, bas effect nahi dalega.
    AUTOPLAY_ACTUALLY_ENABLED = False
    if not next_track and (force_related or (AUTOPLAY_ACTUALLY_ENABLED and q.get_autoplay(chat_id))):
        current = q.get_now_playing(chat_id)
        seed_title = (current or {}).get("title", "").split("|")[0].strip() if current else None
        if seed_title:
            try:
                related = await search_related_track(seed_title, exclude_ids=q.recently_played(chat_id))
            except Exception as e:
                LOGGER.warning(f"Autoplay search fail: {e}")
                related = None
            if related:
                try:
                    related["stream_url"] = await get_stream_url(related["id"])
                    related["requested_by"] = smallcaps_title("autoplay")
                    related["requested_by_id"] = None
                    next_track = related
                except Exception as e:
                    LOGGER.warning(f"Autoplay stream fetch fail: {e}")

    if not next_track and force_related:
        if reply_message is not None:
            await reply_message.reply_text(f"❌ {smallcaps_title('could not find a related track right now')}.")
        return

    if not next_track:
        q.set_now_playing(chat_id, None)
        progress.clear(chat_id)
        try:
            await _call(chat_id).leave_group_call(chat_id)
        except Exception:
            pass
        pool.release(chat_id)
        if not silent_empty:
            text = f"⏭ {smallcaps_title('queue is empty, left the voice chat')}."
            if edit_message is not None:
                try:
                    await edit_message.edit_reply_markup(None)
                except Exception:
                    pass
                await edit_message.reply_text(text)
            elif reply_message is not None:
                await reply_message.reply_text(text)
        return

    try:
        await _call(chat_id).change_stream(chat_id, await _piped(chat_id, next_track))
    except Exception as e:
        if _is_peer_error(e):
            await _refresh_assistant_peers(chat_id)
        try:
            await _call(chat_id).join_group_call(chat_id, await _piped(chat_id, next_track))
        except Exception as e2:
            LOGGER.error(f"Play-next error: {e2!r}", exc_info=True)
            if reply_message is not None:
                await reply_message.reply_text(f"❌ {smallcaps_title('could not continue playback, try again')}.")
            return

    q.set_now_playing(chat_id, next_track)
    q.remember_played(chat_id, next_track.get("id"))
    await _send_now_playing(chat_id, next_track, edit_message=edit_message)


# ---------------------------------------------------------------------------
# Inline buttons (Now Playing card ke neeche)
# ---------------------------------------------------------------------------
@bot.on_callback_query(filters.regex("^m_"))
async def controls_callback(client, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    action = cq.data

    # Playback-changing actions — sirf requester, group admin ya owner.
    # m_progress (sirf display) aur m_close (sirf panel hide karta hai) ko
    # koi bhi member dabaa sakta hai.
    if action in ("m_resume", "m_pause", "m_skip", "m_stop", "m_seek_back", "m_seek_fwd",
                  "m_autoplay_toggle"):
        if not await _can_control(client, chat_id, cq.from_user.id):
            return await cq.answer(NOT_YOUR_REQUEST_TEXT, show_alert=True)

    try:
        if action == "m_progress":
            await cq.answer()

        elif action == "m_resume":
            await _call(chat_id).resume_stream(chat_id)
            q.set_state(chat_id, "playing")
            progress.resume(chat_id)
            await cq.answer("▶️ Resumed")

        elif action == "m_pause":
            await _call(chat_id).pause_stream(chat_id)
            q.set_state(chat_id, "paused")
            progress.pause(chat_id)
            await cq.answer("⏸ Paused")

        elif action == "m_replay":
            track = q.get_now_playing(chat_id)
            if track:
                await _call(chat_id).change_stream(chat_id, await _piped(chat_id, track))
                progress.replay(chat_id)
                await cq.answer("🔁 Replaying")
            else:
                await cq.answer(smallcaps_title("nothing is playing right now"), show_alert=True)

        elif action in ("m_seek_back", "m_seek_fwd"):
            track = q.get_now_playing(chat_id)
            if not track:
                return await cq.answer(smallcaps_title("nothing is playing right now"), show_alert=True)
            total_sec = duration_to_seconds(track.get("duration"))
            current = progress.elapsed(chat_id)
            delta = -15 if action == "m_seek_back" else 15
            new_pos = max(0, min(current + delta, max(total_sec - 1, 0)) if total_sec else current + delta)
            try:
                await _call(chat_id).change_stream(
                    chat_id,
                    await _piped(chat_id, track, new_pos),
                )
                progress.seek_to(chat_id, new_pos)
                await cq.answer("⏪ -15s" if action == "m_seek_back" else "⏩ +15s")
            except Exception as e:
                LOGGER.warning(f"Seek fail: {e}")
                await cq.answer(f"❌ {smallcaps_title('could not seek')}.", show_alert=True)

        elif action == "m_skip":
            await cq.answer("⏭ Skipping")
            await _play_next_or_autoplay(chat_id, edit_message=cq.message)

        elif action == "m_stop":
            await _call(chat_id).leave_group_call(chat_id)
            q.clear(chat_id)
            progress.clear(chat_id)
            pool.release(chat_id)
            await cq.answer("⏹ Stopped")
            try:
                await cq.message.edit_reply_markup(None)
            except Exception:
                pass
            await cq.message.reply_text(f"⏹️ {smallcaps_title('left the voice chat')}.")

        elif action == "m_autoplay_toggle":
            new_value = not q.get_autoplay(chat_id)
            q.set_autoplay(chat_id, new_value)
            await cq.answer(
                f"🔁 {smallcaps_title('autoplay')} {smallcaps_title('on') if new_value else smallcaps_title('off')}"
            )
            track = q.get_now_playing(chat_id)
            if track:
                total_sec = duration_to_seconds(track.get("duration"))
                el = progress.elapsed(chat_id)
                await _edit_body(cq.message, _now_playing_caption(track), _controls_keyboard(chat_id, el, total_sec))

        elif action == "m_close":
            progress.cancel_task(chat_id)
            await cq.answer()
            try:
                await cq.message.delete()
            except Exception:
                pass

    except Exception as e:
        LOGGER.warning(f"Callback error ({action}): {e}")
        await cq.answer(f"❌ {e}", show_alert=True)


# ---------------------------------------------------------------------------
# Owner: /addvd /delvd — /start message ke saath jaane wala image/video/gif
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("addvd") & OWNER_FILTER)
async def addvd_command(client, message: Message):
    _pending_addvd.add(message.from_user.id)
    await message.reply_text(
        f"🖼 {smallcaps_title('ab ek image, video ya gif bhejo — wahi ab se PRIVATE start message ke saath sabko jayega')}."
    )


@bot.on_message(filters.command("delvd") & OWNER_FILTER)
async def delvd_command(client, message: Message):
    await db.delete_start_media()
    _pending_addvd.discard(message.from_user.id)
    await message.reply_text(f"🗑 {smallcaps_title('private start message media hata diya gaya')}.")


@bot.on_message(filters.command("addvd2") & OWNER_FILTER)
async def addvd2_command(client, message: Message):
    _pending_addvd2.add(message.from_user.id)
    await message.reply_text(
        f"🖼 {smallcaps_title('ab ek image, video ya gif bhejo — wahi ab se GROUP start message ke saath sabko jayega')}."
    )


@bot.on_message(filters.command("delvd2") & OWNER_FILTER)
async def delvd2_command(client, message: Message):
    await db.delete_group_start_media()
    _pending_addvd2.discard(message.from_user.id)
    await message.reply_text(f"🗑 {smallcaps_title('group start message media hata diya gaya')}.")


@bot.on_message(
    (filters.photo | filters.video | filters.animation)
    & OWNER_FILTER
    & filters.create(
        lambda _, __, m: bool(m.from_user)
        and (m.from_user.id in _pending_addvd or m.from_user.id in _pending_addvd2)
    )
)
async def addvd_receive(client, message: Message):
    is_group_variant = message.from_user.id in _pending_addvd2
    _pending_addvd.discard(message.from_user.id)
    _pending_addvd2.discard(message.from_user.id)

    if message.photo:
        file_id, media_type = message.photo.file_id, "photo"
    elif message.video:
        file_id, media_type = message.video.file_id, "video"
    elif message.animation:
        file_id, media_type = message.animation.file_id, "animation"
    else:
        return

    if is_group_variant:
        await db.set_group_start_media(file_id, media_type)
        await message.reply_text(f"✅ {smallcaps_title('group start message media set ho gaya')}.")
    else:
        await db.set_start_media(file_id, media_type)
        await message.reply_text(f"✅ {smallcaps_title('private start message media set ho gaya')}.")


# ---------------------------------------------------------------------------
# /id — user aur chat id batao
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("id"))
async def id_command(client, message: Message):
    user_id = message.from_user.id if message.from_user else "Unknown"
    lines = [f"👤 **{smallcaps_title('your id')}:** `{user_id}`"]
    if message.chat.type != "private":
        lines.append(f"👥 **{smallcaps_title('chat id')}:** `{message.chat.id}`")
    if message.reply_to_message and message.reply_to_message.from_user:
        lines.append(f"↩️ **{smallcaps_title('replied user id')}:** `{message.reply_to_message.from_user.id}`")
    await message.reply_text("\n".join(lines))
