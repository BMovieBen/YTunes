"""
utils.py - Shared helpers for YTunes: config loading, logging, text/path helpers.
"""

import datetime
import json
import logging
import logging.handlers
import os
import re
import sys
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "log")
LOG_PATH = os.path.join(LOG_DIR, "ytunes.log")


def _config_path():
    # YTUNES_CONFIG lets tests (or a second instance) point at a different
    # config without touching the real config.json.
    return os.environ.get("YTUNES_CONFIG") or os.path.join(BASE_DIR, "config.json")


# Every key the app reads, with its default. Anything missing from
# config.json falls back to these.
DEFAULTS = {
    "web_port": 8090,
    "bind_host": "127.0.0.1",
    "log_level": "verbose",
    "output_folder": "output",
    "temp_folder": "temp",
    "db_path": "log/ytunes.db",
    "default_art": "default_art.jpg",
    "ytdlp_path": "yt-dlp",
    "ffmpeg_path": "ffmpeg",
    "fpcalc_path": "fpcalc",
    "cookies_from_browser": "",
    "fingerprint_max_seconds": 60,
    "acoustid_api_key": "",
    "discogs_token": "",
    "metadata_source_order": ["itunes", "musicbrainz", "discogs"],
    "auto_match_confidence_threshold": 0.9,
    "user_agent": "YTunes/1.0",
}

# Keys from older versions that no longer do anything.
_OBSOLETE_KEYS = ("clip_offset_seconds", "clip_duration_seconds", "debug_keep_clips")

# Values copied straight from config.sample.json count as "not configured".
_PLACEHOLDERS = {"YOUR_ACOUSTID_API_KEY", "YOUR_DISCOGS_PERSONAL_ACCESS_TOKEN"}

_config_cache = None


def load_config():
    """Load config.json (merged over DEFAULTS), caching the result. Exits
    with a clear message if the file is missing or not valid JSON."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    path = _config_path()
    if not os.path.exists(path):
        print(
            "ERROR: config.json not found. Copy config.sample.json to config.json "
            "and fill in your values before running YTunes."
        )
        sys.exit(1)

    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: config.json is not valid JSON: {e}")
        sys.exit(1)

    cfg = dict(DEFAULTS)
    cfg.update(user_cfg)

    # Resolve relative paths against BASE_DIR.
    for key in ("output_folder", "temp_folder", "db_path", "default_art"):
        if not os.path.isabs(cfg[key]):
            cfg[key] = os.path.normpath(os.path.join(BASE_DIR, cfg[key]))
    for key in ("output_folder", "temp_folder"):
        os.makedirs(cfg[key], exist_ok=True)

    for key in ("acoustid_api_key", "discogs_token"):
        if cfg.get(key) in _PLACEHOLDERS:
            cfg[key] = ""

    _config_cache = cfg
    _configure_logging(cfg.get("log_level", "verbose"))

    stale = [k for k in _OBSOLETE_KEYS if k in user_cfg]
    if stale:
        log(f"config.json contains settings that are no longer used: {', '.join(stale)}", "warning")
    return cfg


def reset_config_cache():
    """For tests: forget the cached config so the next load_config() re-reads it."""
    global _config_cache
    _config_cache = None


# ---------- Logging ----------

_logger = logging.getLogger("ytunes")
_logging_configured = False


def _configure_logging(log_level):
    global _logging_configured
    for h in list(_logger.handlers):
        _logger.removeHandler(h)
        h.close()
    _logger.propagate = False

    if log_level == "off":
        _logger.setLevel(logging.CRITICAL + 1)
    elif log_level == "on_error":
        _logger.setLevel(logging.ERROR)
    else:
        _logger.setLevel(logging.INFO)

    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    _logger.addHandler(console)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8", delay=True
        )
        file_handler.setFormatter(fmt)
        _logger.addHandler(file_handler)
    except OSError:
        pass
    _logging_configured = True


_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def log(message, level="info"):
    """Leveled logger honoring config.log_level (verbose/on_error/off)."""
    if not _logging_configured:
        _configure_logging("verbose")
    _logger.log(_LEVELS.get(level, logging.INFO), message)


# ---------- Text helpers ----------

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name, max_length=150):
    """Make a string safe to use as a Windows filename."""
    if not name:
        return "Unknown"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = name[:max_length].strip(" .")
    if not name:
        return "Unknown"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name


def normalize_text(s):
    """Lowercase, strip parenthetical/bracketed annotations like '(Remastered
    2011)' or '[Official Video]', drop punctuation, collapse whitespace —
    enough to compare titles that mean the same thing but aren't identical
    strings."""
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_VIDEO_ID_PATTERNS = (
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})"),
)


def youtube_video_id(url):
    """Extract the 11-character video ID from the common YouTube URL forms,
    or None (e.g. for a playlist-only URL)."""
    for pattern in _VIDEO_ID_PATTERNS:
        m = pattern.search(url or "")
        if m:
            return m.group(1)
    return None


# Thumbnail hosts: https://i.ytimg.com/vi/<id>/hq720.jpg (also vi_webp, img.youtube.com)
_THUMBNAIL_URL = re.compile(
    r"^https?://(?:i\d?\.ytimg\.com|img\.youtube\.com)/vi(?:_webp)?/([A-Za-z0-9_-]{11})/", re.I
)
_IMAGE_EXTENSIONS = re.compile(r"\.(?:jpe?g|png|webp|gif|bmp|avif)$", re.I)


class InvalidUrl(ValueError):
    """The pasted link can't be a video."""


def clean_video_url(url):
    """Tidy a pasted link before it's queued. A YouTube thumbnail link (what
    "Copy image address" gives you) becomes its video's watch URL. Any other
    image link is rejected, since yt-dlp would "download" the picture and
    then fail to find audio in it."""
    url = (url or "").strip()
    m = _THUMBNAIL_URL.match(url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    path = re.split(r"[?#]", url, maxsplit=1)[0]
    if _IMAGE_EXTENSIONS.search(path):
        raise InvalidUrl("That's a link to an image, not a video. Copy the video's link instead.")
    return url


def new_track_id():
    """Generate a short unique ID for a queue track."""
    return uuid.uuid4().hex[:12]


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")
