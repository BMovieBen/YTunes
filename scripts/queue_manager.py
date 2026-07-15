"""
queue_manager.py - Load/save/mutate the persistent YTunes track list.

The list persists across app restarts in log/queue.json — a single unified
list of cards (in-progress and finished-but-not-yet-confirmed). There's no
separate "library": once a track is confirmed & tagged, its file lives in
the configured output folder and the card is removed entirely (see
pipeline.confirm_track) rather than lingering in a "complete" state.

Track schema:
{
  "id": str,
  "source_url": str,          # YouTube URL. Initially the raw URL as typed,
                                # since a placeholder is added before resolving.
  "playlist_title": str|None, # set if this came from a playlist
  "video_title": str,         # display title. Initially just the raw URL
                                # (so a card shows up immediately on add),
                                # replaced with the real title once resolved.
  "resolved": bool,           # False until resolve_urls() has run for this
                                # entry — worker resolves before doing anything else
  "status": str,               # resolving | queued | fetching_info | downloading |
                                # analyzing | needs_review | tagging | error
                                # (no "complete" — confirmed tracks are removed, not
                                # transitioned)
  "error_message": str|None,
  "audio_path": str|None,      # downloaded full-length MP3 (temp)
  "candidates": list,          # metadata match candidates from lookup sources
  "youtube_reference": dict|None,  # video's own title/channel/thumbnail, for compare
  "fp_matches": list,          # raw fingerprint guesses, kept for "find more matches"
  "selected": dict|None,       # the metadata the user confirmed
  "custom_art_path": str|None, # user-uploaded album art, overrides candidate art
  "output_path": str|None,     # final tagged file location (set only transiently
                                # during tag_and_move; the track is removed right after)
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

# Statuses where a background worker is actively doing I/O for that track
# right now — these can't be deleted. Kept here (not just in pipeline.py) so
# queue_manager itself can enforce it at the data layer too, not just via
# whatever calls it.
ACTIVE_STATUSES = ("resolving", "fetching_info", "downloading", "analyzing", "tagging")


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


def add_track(source_url, video_title, playlist_title=None, resolved=True):
    queue = load_queue()
    track = {
        "id": new_track_id(),
        "source_url": source_url,
        "playlist_title": playlist_title,
        "video_title": video_title,
        "resolved": resolved,
        "status": "queued" if resolved else "resolving",
        "error_message": None,
        "audio_path": None,
        "candidates": [],
        "youtube_reference": None,
        "fp_matches": [],
        "selected": None,
        "custom_art_path": None,
        "output_path": None,
        "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    queue["tracks"].append(track)
    save_queue(queue)
    log(f"Added track {track['id']} (resolved={resolved}): {video_title}")
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


def is_deletable(track):
    """A card can only be removed if nothing is actively working on it right
    now. Pending work (queued/resolving), a paused review, or a finished-
    but-failed attempt (error) are all fine to delete; anything mid-flight
    is not."""
    return track.get("status") not in ACTIVE_STATUSES


def cleanup_stuck_tracks():
    """
    Called once at app startup. Anything left actively mid-processing when
    the app last closed (downloading, analyzing, tagging, fetching_info) has
    no in-memory progress to resume — reset it back to "queued" so the
    worker restarts that stage cleanly. A track still sitting in "resolving"
    needs no change; the worker already knows how to pick that up as-is.
    """
    queue = load_queue()
    changed = False
    for t in queue["tracks"]:
        if t["status"] in ("fetching_info", "downloading", "analyzing", "tagging"):
            t["status"] = "queued"
            changed = True
    if changed:
        save_queue(queue)
        log("Reset in-progress tracks to 'queued' after restart.")
