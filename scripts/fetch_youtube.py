"""
fetch_youtube.py - Resolve YouTube URLs (single video or playlist) and download
audio as MP3 via yt-dlp. Mirrors Article2Pod's fetch-youtube.py, extended to
expand playlists into individual queue entries.
"""

import os
import subprocess
import json

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
        url,
    ]
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
        "--ffmpeg-location", _ffmpeg_path(),
        "--no-playlist",
        "-o", out_template,
        "--no-warnings",
        url,
    ]
    log(f"Downloading audio for track {track_id} from {url}")
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
    except subprocess.CalledProcessError as e:
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


def _ytdlp_path():
    from utils import load_config
    return load_config().get("ytdlp_path", "yt-dlp")


def _ffmpeg_path():
    from utils import load_config
    return load_config().get("ffmpeg_path", "ffmpeg")
