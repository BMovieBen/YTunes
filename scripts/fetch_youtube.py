"""
fetch_youtube.py - Resolve YouTube URLs (single video or playlist) and download
audio as MP3 via yt-dlp. Mirrors Article2Pod's fetch-youtube.py, extended to
expand playlists into individual queue entries.
"""

import os
import subprocess
import json
import time

from utils import log, safe_filename


def resolve_urls(url):
    """
    Given a YouTube URL (video or playlist), return a list of
    {"url": ..., "title": ..., "playlist_title": ... or None}.

    Uses yt-dlp --flat-playlist --dump-json so we get metadata without
    downloading anything yet.
    """
    cfg_ytdlp = _ytdlp_path()
    cmd = [
        cfg_ytdlp,
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
    ]
    cmd.extend(_cookies_args())
    cmd.extend(["--remote-components", "ejs:github"])
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
    except subprocess.CalledProcessError as e:
        log(f"yt-dlp failed to resolve URL {url}: {e.stderr}", "error")
        raise RuntimeError(f"Could not resolve YouTube URL: {url}")
    except subprocess.TimeoutExpired:
        log(f"yt-dlp timed out resolving {url}", "error")
        raise RuntimeError(f"Timed out resolving YouTube URL: {url}")

    data = json.loads(result.stdout)

    # Playlist: has "entries"
    if data.get("_type") == "playlist" or "entries" in data:
        playlist_title = data.get("title", "Untitled Playlist")
        entries = data.get("entries") or []
        out = []
        for entry in entries:
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
                "title": entry.get("title", "Untitled"),
                "playlist_title": playlist_title,
            })
        log(f"Resolved playlist '{playlist_title}' with {len(out)} tracks")
        return out

    # Single video
    return [{
        "url": data.get("webpage_url", url),
        "title": data.get("title", "Untitled"),
        "playlist_title": None,
    }]


def download_audio(url, temp_folder, track_id):
    """
    Download a single YouTube video's audio as MP3 into temp_folder.
    Returns the path to the downloaded MP3.
    """
    out_template = os.path.join(temp_folder, f"{track_id}.%(ext)s")
    cmd = [
        _ytdlp_path(),
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--no-playlist",
        "-o", out_template,
        "--no-warnings",
    ]
    ffmpeg_path = _ffmpeg_path()
    if ffmpeg_path and ffmpeg_path != "ffmpeg":
        cmd.extend(["--ffmpeg-location", ffmpeg_path])
    cmd.extend(_cookies_args())
    cmd.extend(["--remote-components", "ejs:github"])
    cmd.append(url)
    log(f"Downloading audio for track {track_id} from {url}")
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
            break
        except subprocess.CalledProcessError as e:
            # YouTube sometimes returns an empty response; a retry normally succeeds.
            if "Did not get any data blocks" in (e.stderr or "") and attempt < attempts:
                log(f"Empty response for {url}, retrying ({attempt}/{attempts - 1})", "warning")
                time.sleep(3)
                continue
            log(f"yt-dlp download failed for {url}: {e.stderr}", "error")
            raise RuntimeError(f"Download failed: {e.stderr[-500:] if e.stderr else 'unknown error'}")
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
    Fetch full single-video metadata (title, channel/uploader, thumbnail,
    year) via yt-dlp. Used as a guaranteed fallback candidate so there's
    always something to review/confirm even when no metadata source has a
    match.
    """
    cmd = [
        _ytdlp_path(),
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
    ]
    cmd.extend(_cookies_args())
    cmd.extend(["--remote-components", "ejs:github"])
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        log(f"Failed to fetch YouTube metadata for {url}: {e}", "error")
        return {"title": None, "uploader": None, "thumbnail": None, "year": None}

    thumbnail = data.get("thumbnail")
    if not thumbnail:
        thumbs = data.get("thumbnails") or []
        if thumbs:
            thumbnail = sorted(thumbs, key=lambda t: t.get("width", 0) or 0)[-1].get("url")

    # Prefer an explicit release year (more accurate for music uploads) and
    # fall back to the video's upload date (format YYYYMMDD) otherwise.
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


def _ytdlp_path():
    from utils import load_config
    return load_config().get("ytdlp_path", "yt-dlp")


def _ffmpeg_path():
    from utils import load_config
    return load_config().get("ffmpeg_path", "ffmpeg")


def _cookies_args():
    """
    Build the --cookies-from-browser argument if configured. YouTube's bot
    detection frequently 403s anonymous requests; authenticating as a real
    logged-in browser session works around this.
    """
    from utils import load_config
    browser = load_config().get("cookies_from_browser", "").strip()
    if not browser:
        return []
    return ["--cookies-from-browser", browser]
