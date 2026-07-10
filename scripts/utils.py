"""
utils.py - Shared helpers for YTunes: config loading, logging, path helpers.
Mirrors the pattern used in Article2Pod's utils.py.
"""

import json
import os
import re
import sys
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_DIR = os.path.join(BASE_DIR, "log")
LOG_PATH = os.path.join(LOG_DIR, "ytunes.log")

_config_cache = None


def load_config():
    """Load config.json, caching the result. Exits with a clear message if missing."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    if not os.path.exists(CONFIG_PATH):
        print(
            "ERROR: config.json not found. Copy config.sample.json to config.json "
            "and fill in your values before running YTunes."
        )
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        _config_cache = json.load(f)

    # Resolve relative folders against BASE_DIR
    for key in ("output_folder", "temp_folder"):
        val = _config_cache.get(key, key.replace("_folder", ""))
        if not os.path.isabs(val):
            _config_cache[key] = os.path.join(BASE_DIR, val)
        os.makedirs(_config_cache[key], exist_ok=True)

    return _config_cache


def log(message, level="info"):
    """Simple leveled logger, honoring config.log_level (verbose/on_error/off)."""
    cfg = load_config() if os.path.exists(CONFIG_PATH) else {"log_level": "verbose"}
    log_level = cfg.get("log_level", "verbose")

    if log_level == "off":
        return
    if log_level == "on_error" and level != "error":
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level.upper()}] {message}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def safe_filename(name, max_length=150):
    """Strip characters that are illegal in Windows filenames."""
    if not name:
        return "Unknown"
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip(" .")
    return name[:max_length] if name else "Unknown"


def new_track_id():
    """Generate a short unique ID for a queue track."""
    import uuid
    return uuid.uuid4().hex[:12]
