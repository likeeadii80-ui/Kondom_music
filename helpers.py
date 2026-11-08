import html
import random

# Naya "fancy small caps" style — purane plain-smallcaps (ᴀ ɴ ᴏ ᴘ ᴜ ᴍ) se alag,
# kuch letters Greek/Cyrillic look-alike glyphs use karte hain (ᴧ η σ ᴩ υ ϻ є)
# jaisa ki reference example mein tha ("ᴛʜᴧηᴋs ғσʀ ᴧᴅᴅɪηɢ" jaisa).
_SMALLCAPS = {
    "a": "ᴧ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "є", "f": "ғ", "g": "ɢ",
    "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ϻ", "n": "η",
    "o": "σ", "p": "ᴩ", "q": "ǫ", "r": "ʀ", "s": "s", "t": "ᴛ", "u": "υ",
    "v": "ᴠ", "w": "ᴡ", "x": "x", "y": "ʏ", "z": "ᴢ",
}

# Processing / "searching" ke waqt dikhne wala random emoji
PROCESSING_EMOJIS = ["🧪", "🦋", "🔍", "💕"]

# Emoji ke niche dikhne wala random processing text
PROCESSING_TEXTS = ["HOLD ON DARLING", "OKI BABY W8", "PROCESSING OUR REQUEST BABY"]

# Har bade template (welcome, help, added-to-group, now playing...) ke neeche
# jaane wala common footer separator — reference style ke hisaab se.
FOOTER_LINE = "•── ⋅ ⋅  ────── ⋅᯽⋅ ────── ⋅ ⋅ ⋅──•"


def smallcaps(text: str) -> str:
    """Har letter ko smallcaps mein badalta hai (lowercase style — labels ke liye)."""
    return "".join(_SMALLCAPS.get(ch.lower(), ch) if ch.isalpha() else ch for ch in text)


def smallcaps_title(text: str) -> str:
    """Har word ka pehla letter normal capital, baaki smallcaps — headings ke liye."""
    out = []
    new_word = True
    for ch in text:
        if ch.isalpha():
            out.append(ch.upper() if new_word else _SMALLCAPS.get(ch.lower(), ch))
            new_word = False
        else:
            out.append(ch)
            new_word = ch in " -_/\n"
    return "".join(out)


def fancy_italic(text: str) -> str:
    """A-Z/a-z ko Mathematical Sans-Serif Bold Italic unicode mein badalta hai
    (jaise 𝙌𝙪𝙚𝙚𝙣 𝙭 𝙢𝙪𝙨𝙞𝙘 style) — baaki characters (space, emoji) waise hi rehte hain."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D63C + (ord(ch) - ord("A"))))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D656 + (ord(ch) - ord("a"))))
        else:
            out.append(ch)
    return "".join(out)


def random_processing_emoji() -> str:
    """Sirf ek random emoji deta hai — pehle status message isi ek emoji ke saath jaata hai."""
    return random.choice(PROCESSING_EMOJIS)


def processing_caption(emoji: str) -> str:
    """Pehle se bheje gaye emoji ke niche ek random processing text jodta hai,
    bot ki smallcaps style mein (jaise baaki saara text)."""
    return f"{emoji}\n{smallcaps_title(random.choice(PROCESSING_TEXTS))}"


def format_duration(seconds) -> str:
    """Seconds ko MM:SS ya H:MM:SS format mein badalta hai. Already-string ho to wahi wapas."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return str(seconds) if seconds else "??:??"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def duration_to_seconds(duration_str: str) -> int:
    """'4:38' ya '1:04:38' jaisi string ko seconds mein badalta hai."""
    if not duration_str:
        return 0
    parts = str(duration_str).split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def format_uptime(seconds) -> str:
    """Seconds ko 'ʜʜ:ᴍᴍ:ss' jaisa smallcaps uptime string banata hai
    (jaise '12ʜ:4ᴍ:10s') — Now Playing/group start message ke liye."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 0
    h, rem = divmod(max(seconds, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h}ʜ:{m}ᴍ:{s}s"


def esc(text) -> str:
    """HTML-escape karta hai (&, <, >) taaki blockquote/HTML parsing na tootey."""
    return html.escape(str(text), quote=False)


def blockquote(text: str) -> str:
    """Telegram ka normal (non-expandable) quote box."""
    return f"<blockquote>{text}</blockquote>"


def expandable_blockquote(text: str) -> str:
    """Telegram ka collapsible/expandable quote box (लंबा content ho to 'show more' aata hai)."""
    return f"<blockquote expandable>{text}</blockquote>"
