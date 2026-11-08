"""
Har chat ke liye music queue aur current-playing track memory mein rakhta hai.
Bot restart hone par yeh khali ho jaata hai — jo ki theek hai, kyunki VC bhi
restart ke baad dobara se join karni padegi.
"""

# chat_id -> list of track dicts: {video_id, title, duration, thumbnail, requested_by, stream_url}
_queues: dict[int, list] = {}

# chat_id -> currently playing track dict (ya None)
_now_playing: dict[int, dict] = {}

# chat_id -> "playing" | "paused"
_state: dict[int, str] = {}

# chat_id -> autoplay on/off (default ON)
_autoplay: dict[int, bool] = {}

# chat_id -> bacha hua loop count (0 = loop off)
_loop: dict[int, int] = {}

# chat_id -> playback speed (1.0 = normal)
_speed: dict[int, float] = {}

# chat_id -> list of recently played video_ids (autoplay repeats avoid karne ke liye)
_recent: dict[int, list] = {}
_RECENT_LIMIT = 15


def get_queue(chat_id: int) -> list:
    return _queues.setdefault(chat_id, [])


def push(chat_id: int, track: dict) -> int:
    """Queue ke end mein track daalta hai, uska position (1-indexed) return karta hai."""
    q = get_queue(chat_id)
    q.append(track)
    return len(q)


def pop_next(chat_id: int):
    """Queue se agla track nikaal kar deta hai, agar khali hai to None."""
    q = get_queue(chat_id)
    if not q:
        return None
    return q.pop(0)


def set_now_playing(chat_id: int, track):
    if track is None:
        _now_playing.pop(chat_id, None)
        _state.pop(chat_id, None)
    else:
        _now_playing[chat_id] = track
        _state[chat_id] = "playing"


def get_now_playing(chat_id: int):
    return _now_playing.get(chat_id)


def is_playing(chat_id: int) -> bool:
    return chat_id in _now_playing


def set_state(chat_id: int, state: str):
    _state[chat_id] = state


def get_state(chat_id: int) -> str:
    return _state.get(chat_id, "playing")


def get_loop(chat_id: int) -> int:
    return _loop.get(chat_id, 0)


def set_loop(chat_id: int, n: int):
    if n <= 0:
        _loop.pop(chat_id, None)
    else:
        _loop[chat_id] = n


def get_speed(chat_id: int) -> float:
    return _speed.get(chat_id, 1.0)


def set_speed(chat_id: int, value: float):
    if abs(value - 1.0) < 0.01:
        _speed.pop(chat_id, None)
    else:
        _speed[chat_id] = value


def clear(chat_id: int):
    _loop.pop(chat_id, None)
    _speed.pop(chat_id, None)
    _queues.pop(chat_id, None)
    _now_playing.pop(chat_id, None)
    _state.pop(chat_id, None)


def get_autoplay(chat_id: int) -> bool:
    return _autoplay.get(chat_id, True)


def set_autoplay(chat_id: int, value: bool):
    _autoplay[chat_id] = value


def remember_played(chat_id: int, video_id):
    if not video_id:
        return
    lst = _recent.setdefault(chat_id, [])
    lst.append(video_id)
    del lst[: -_RECENT_LIMIT]


def recently_played(chat_id: int) -> set:
    return set(_recent.get(chat_id, []))
