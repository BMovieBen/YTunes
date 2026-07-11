"""
queue_manager.py - Load/save/mutate the persistent YTunes queue.
Mirrors Article2Pod's queue_manager.py so the queue survives app restarts.

Track schema:
{
  "id": str,
  "source_url": str,          # original YouTube video URL
  "playlist_title": str|None, # set if this came from a playlist
  "video_title": str,         # raw YouTube title, used as fallback search query
  "status": str,               # queued | downloading | analyzing | needs_review |
                                # tagging | complete | error
  "error_message": str|None,
  "audio_path": str|None,      # downloaded full-length MP3 (temp)
  "candidates": list,          # metadata match candidates from lookup sources
  "youtube_reference": dict|None,  # video's own title/channel/thumbnail, for compare
  "fp_matches": list,          # raw fingerprint guesses, kept for "find more matches"
  "selected": dict|None,       # the metadata the user confirmed
  "manual_override": bool,     # True if user typed metadata instead of picking a candidate
  "custom_art_path": str|None, # user-uploaded album art, overrides candidate art
  "output_path": str|None,     # final tagged file location once complete
  "added_at": str
}
"""

import json
import os
import datetime
import threading

from utils import BASE_DIR, log, new_track_id

QUEUE_PATH = os.path.join(BASE_DIR, "log", "queue.json")
_lock = threading.Lock()


def _empty_queue():
    return {"tracks": []}


def load_queue():
    if not os.path.exists(QUEUE_PATH):
        return _empty_queue()
    try:
        with open(QUEUE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f"Failed to load queue.json, starting fresh: {e}", "error")
        return _empty_queue()


def save_queue(queue):
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
    with _lock:
        with open(QUEUE_PATH, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)


def add_track(source_url, video_title, playlist_title=None):
    queue = load_queue()
    track = {
        "id": new_track_id(),
        "source_url": source_url,
        "playlist_title": playlist_title,
        "video_title": video_title,
        "status": "queued",
        "error_message": None,
        "audio_path": None,
        "candidates": [],
        "youtube_reference": None,
        "fp_matches": [],
        "selected": None,
        "manual_override": False,
        "custom_art_path": None,
        "output_path": None,
        "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    queue["tracks"].append(track)
    save_queue(queue)
    log(f"Queued track {track['id']}: {video_title}")
    return track


def get_track(track_id):
    queue = load_queue()
    for t in queue["tracks"]:
        if t["id"] == track_id:
            return t
    return None


def update_track(track_id, **fields):
    queue = load_queue()
    for t in queue["tracks"]:
        if t["id"] == track_id:
            t.update(fields)
            save_queue(queue)
            return t
    log(f"update_track: track {track_id} not found", "error")
    return None


def remove_track(track_id):
    queue = load_queue()
    before = len(queue["tracks"])
    queue["tracks"] = [t for t in queue["tracks"] if t["id"] != track_id]
    save_queue(queue)
    return len(queue["tracks"]) < before


def list_tracks(status=None):
    queue = load_queue()
    if status is None:
        return queue["tracks"]
    return [t for t in queue["tracks"] if t["status"] == status]


def cleanup_stuck_tracks():
    """On startup, anything left mid-processing from a crash goes back to 'queued'."""
    queue = load_queue()
    changed = False
    for t in queue["tracks"]:
        if t["status"] in ("downloading", "analyzing", "tagging"):
            t["status"] = "queued"
            changed = True
    if changed:
        save_queue(queue)
        log("Reset in-progress tracks to 'queued' after restart.")
