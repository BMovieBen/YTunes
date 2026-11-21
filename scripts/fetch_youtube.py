"""
fetch_youtube.py - Resolve YouTube URLs (single video or playlist) and download
audio as MP3 via yt-dlp.
"""

import json
import os
import shutil
import subprocess
import sys
import time

from utils import load_config, log


def _ytdlp(*args):
    """Base yt-dlp command with the options every call needs: quiet
    warnings, browser cookies if configured (YouTube's bot detection often
    403s anonymous requests), and the remote JS challenge solver."""
    cfg = load_config()
    cmd = [_ytdlp_executable(cfg["ytdlp_path"]), "--no-warnings"]
    browser = (cfg.get("cookies_from_browser") or "").strip()
    if browser:
        cmd += ["--cookies-from-browser", browser]
    cmd += ["--remote-components", "ejs:github"]
    cmd += list(args)
    return cmd


def _run(cmd, timeout):
    """Run yt-dlp, decoding its output as UTF-8 so non-English titles
    survive (PYTHONUTF8 makes a Python-based yt-dlp write UTF-8 to pipes
    instead of the Windows console code page)."""
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=True, env=env)
    except FileNotFoundError:
        raise RuntimeError(f"yt-dlp not found ('{cmd[0]}'). Install it or set ytdlp_path in config.json.")


def _ytdlp_executable(configured):
    """The configured yt-dlp if it can be found; otherwise the one installed
    alongside the running Python (the venv), so starting app.py directly
    works without activating the venv first."""
    if os.path.isabs(configured) or shutil.which(configured):
        return configured
    beside_python = os.path.join(os.path.dirname(sys.executable), os.path.basename(configured))
    for candidate in (beside_python, beside_python + ".exe"):
        if os.path.exists(candidate):
            return candidate
    return configured


def resolve_urls(url):
    """
    Given a YouTube URL (video or playlist), return a list of
    {"url": ..., "title": ..., "playlist_title": ... or None}.
    Uses --flat-playlist so nothing is downloaded yet.
    """
    cmd = _ytdlp("--flat-playlist", "--dump-single-json", url)
    try:
        result = _run(cmd, timeout=120)
    except subprocess.CalledProcessError as e:
        log(f"yt-dlp failed to resolve URL {url}: {e.stderr}", "error")
        raise RuntimeError(f"Could not resolve YouTube URL: {_last_line(e.stderr) or url}")
    except subprocess.TimeoutExpired:
        log(f"yt-dlp timed out resolving {url}", "error")
        raise RuntimeError(f"Timed out resolving YouTube URL: {url}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("yt-dlp returned output that isn't JSON")
    return parse_resolved(data, url)


def parse_resolved(data, url):
    """Turn yt-dlp's --dump-single-json output into track entries."""
    if data.get("_type") == "playlist" or "entries" in data:
        playlist_title = data.get("title") or "Untitled Playlist"
        out = []
        for entry in data.get("entries") or []:
            if not entry:
                continue
            video_url = entry.get("url") or entry.get("webpage_url")
            if not video_url:
                continue
            # --flat-playlist sometimes gives bare video IDs
            if not video_url.startswith("http"):
                video_url = f"https://www.youtube.com/watch?v={video_url}"
            out.append({
                "url": video_url,
                "title": entry.get("title") or "Untitled",
                "playlist_title": playlist_title,
            })
        log(f"Resolved playlist '{playlist_title}' with {len(out)} tracks")
        return out

    return [{
        "url": data.get("webpage_url") or url,
        "title": data.get("title") or "Untitled",
        "playlist_title": None,
    }]


def download_audio(url, temp_folder, track_id):
    """Download one video's audio as MP3 into temp_folder; return its path."""
    cfg = load_config()
    args = [
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--no-playlist",
        "-o", os.path.join(temp_folder, f"{track_id}.%(ext)s"),
    ]
    if cfg["ffmpeg_path"] and cfg["ffmpeg_path"] != "ffmpeg":
        args += ["--ffmpeg-location", cfg["ffmpeg_path"]]
    cmd = _ytdlp(*args, url)

    log(f"Downloading audio for track {track_id} from {url}")
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            _run(cmd, timeout=600)
            break
        except subprocess.CalledProcessError as e:
            # YouTube sometimes returns an empty response; a retry normally succeeds.
            if "Did not get any data blocks" in (e.stderr or "") and attempt < attempts:
                log(f"Empty response for {url}, retrying ({attempt}/{attempts - 1})", "warning")
                time.sleep(3)
                continue
            log(f"yt-dlp download failed for {url}: {e.stderr}", "error")
            raise RuntimeError(f"Download failed: {_last_line(e.stderr) or 'unknown error'}")
        except subprocess.TimeoutExpired:
            log(f"yt-dlp download timed out for {url}", "error")
            raise RuntimeError("Download timed out")

    expected_path = os.path.join(temp_folder, f"{track_id}.mp3")
    if not os.path.exists(expected_path):
        raise RuntimeError("yt-dlp reported success but no MP3 was produced")
    log(f"Downloaded {expected_path}")
    return expected_path


def get_video_metadata(url):
    """
    Fetch single-video metadata (title, channel, thumbnail, year). Used as
    the always-available "Original" reference on the review card. Returns
    all-None values on failure rather than raising.
    """
    cmd = _ytdlp("--dump-single-json", "--no-playlist", url)
    try:
        result = _run(cmd, timeout=60)
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        log(f"Failed to fetch YouTube metadata for {url}: {e}", "error")
        return {"title": None, "uploader": None, "thumbnail": None, "year": None}

    thumbnail = data.get("thumbnail")
    if not thumbnail:
        thumbs = data.get("thumbnails") or []
        if thumbs:
            thumbnail = max(thumbs, key=lambda t: t.get("width") or 0).get("url")

    # Prefer an explicit release year (more accurate for music uploads),
    # else the upload date (YYYYMMDD).
    year = None
    if data.get("release_year"):
        year = str(data["release_year"])
    elif data.get("upload_date"):
        year = data["upload_date"][:4]

    return {
        "title": data.get("title"),
        "uploader": data.get("uploader") or data.get("channel"),
        "thumbnail": thumbnail,
        "year": year,
    }


def _last_line(text):
    """The last non-empty line of yt-dlp's stderr — usually the actual error."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1][-300:] if lines else ""
