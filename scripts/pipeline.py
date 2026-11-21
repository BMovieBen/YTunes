"""
pipeline.py - Orchestrates the YTunes processing pipeline:
  1. Add a URL -> immediately visible as a placeholder card, resolved in
     the background (single video, or expanded into one card per playlist
     entry)
  2. Download audio for a resolved track
  3. Fingerprint it via AcoustID + a light initial metadata lookup (top pick
     per source) so review can start quickly
  4. On demand ("Find more matches"), run a broader search
  5. Once the user confirms metadata, tag the file, move it to the
     configured output folder, and remove the card

A single background worker thread handles the queue. Adding a URL starts it
if it isn't already running; if it is, it picks up the new track itself.
Track state lives in the database (store.py), so it survives restarts.
"""

import os
import re
import threading
import traceback
from difflib import SequenceMatcher

import fetch_youtube
import fingerprint
import metadata_lookup
import statuses as st
import store
import tag_mp3
from utils import load_config, log, normalize_text, youtube_video_id


class DuplicateTrack(ValueError):
    """The URL is a single video that's already on a card."""


# ---------- Worker ----------

_worker_lock = threading.Lock()   # guards _worker_running only; never held during I/O
_worker_running = False


def start_background_processing():
    """Start the worker thread if one isn't already running.

    _worker_running is only read or written under _worker_lock, and the
    worker's own decision to stop (see _worker_loop) re-checks for pending
    work under that same lock. So a track added just as the worker is
    finishing is always seen by either that worker or a new one."""
    global _worker_running
    with _worker_lock:
        if _worker_running:
            return None
        _worker_running = True
    log("Starting background worker thread")
    thread = threading.Thread(target=_worker_loop, name="ytunes-worker", daemon=True)
    thread.start()
    return thread


def _worker_loop():
    global _worker_running
    processed = 0
    try:
        cfg = load_config()
        while True:
            track = store.claim_next_pending()
            if track is None:
                with _worker_lock:
                    if not store.has_pending():
                        _worker_running = False
                        break
                continue
            log(f"Worker picked up track {track['id']} (status={track['status']}): {track.get('video_title')}")
            try:
                if track["status"] == st.RESOLVING:
                    _resolve_track(track)
                else:
                    _process_track(track, cfg)
            except Exception as e:
                log(f"Error processing track {track['id']}: {e}", "error")
                _fail(track["id"], str(e))
            processed += 1
        log(f"Processing run finished ({processed} track(s) handled)")
    except Exception:
        # Anything escaping here would otherwise kill the daemon thread
        # silently; log it and make sure the next add can start a new one.
        log(f"Background worker crashed:\n{traceback.format_exc()}", "error")
        with _worker_lock:
            _worker_running = False


def _fail(track_id, message):
    try:
        store.transition(track_id, st.ERROR, error_message=message)
    except (KeyError, store.InvalidTransition) as e:
        log(f"Couldn't mark track {track_id} as failed: {e}", "error")


# ---------- Adding / resolving ----------

def add_url(url):
    """Add a placeholder card right away (its title is just the URL until
    the worker resolves it), then make sure the worker is running.
    Raises DuplicateTrack if it's a single video that's already on a card."""
    video_id = youtube_video_id(url)
    if video_id and "list=" not in url and store.existing_video_ids([video_id]):
        raise DuplicateTrack("That video is already in the list")
    track = store.add_track(url, video_title=url, playlist_title=None, resolved=False)
    start_background_processing()
    return [track]


def _resolve_track(track):
    """Turn a placeholder's raw URL into real tracks. A single video updates
    the same card; a playlist replaces it with one queued card per entry,
    skipping videos that are already on a card."""
    track_id = track["id"]
    entries = fetch_youtube.resolve_urls(track["source_url"])
    if not entries:
        raise RuntimeError("No video found at that URL")

    if len(entries) == 1:
        entry = entries[0]
        store.transition(
            track_id, st.QUEUED,
            source_url=entry["url"],
            video_title=entry["title"],
            playlist_title=entry.get("playlist_title"),
            resolved=True,
        )
        log(f"Resolved track {track_id}: {entry['title']}")
        return

    existing = store.existing_video_ids(youtube_video_id(e["url"]) for e in entries)
    seen = set(existing)
    fresh = []
    for e in entries:
        vid = youtube_video_id(e["url"])
        if vid and vid in seen:
            continue
        seen.add(vid)
        fresh.append(e)
    skipped = len(entries) - len(fresh)
    if not fresh:
        raise RuntimeError(f"All {len(entries)} videos in this playlist are already in the list")
    store.replace_with_entries(track_id, fresh)
    log(f"Resolved playlist for track {track_id} into {len(fresh)} track(s)"
        + (f", skipped {skipped} already in the list" if skipped else ""))


def retry_track(track_id):
    """Re-queue a failed track (resolve again if it was never resolved)."""
    track = store.get_track(track_id)
    if not track:
        raise KeyError(track_id)
    new_status = st.QUEUED if track.get("resolved") else st.RESOLVING
    store.transition(track_id, new_status, error_message=None)
    start_background_processing()


# ---------- Processing one track ----------

def _process_track(track, cfg):
    """Runs with the track already claimed (status fetching_info)."""
    track_id = track["id"]

    # Step 1: YouTube's own title/channel/thumbnail — a quick metadata call,
    # so the card shows real info before the download finishes.
    yt_meta = fetch_youtube.get_video_metadata(track["source_url"])
    title = yt_meta.get("title") or track["video_title"]
    playlist_title = track.get("playlist_title")
    youtube_reference = {
        "source": "youtube",
        "title": title,
        "artist": yt_meta.get("uploader") or "Unknown Artist",
        # A single isn't part of an album, so its own title reads better
        # than a blank. Playlist tracks use the playlist's title.
        "album": playlist_title or title,
        "year": yt_meta.get("year") or "",
        "genre": "",
        "track_number": None,
        "art_url": yt_meta.get("thumbnail"),
    }
    store.transition(track_id, st.DOWNLOADING, youtube_reference=youtube_reference)

    # Step 2: download the audio.
    audio_path = fetch_youtube.download_audio(track["source_url"], cfg["temp_folder"], track_id)
    store.transition(track_id, st.ANALYZING, audio_path=audio_path)

    # Step 3: fingerprint + a light lookup (top result per source) so review
    # can start without waiting on a full search.
    fp_matches = _fingerprint_track(track_id, audio_path, cfg)
    query = _best_query(fp_matches, track["video_title"])
    candidates = []
    if query:
        candidates = metadata_lookup.search_all(query, limit_per_source=1)
        for c in candidates:
            c["confidence"] = _confidence_for(c, fp_matches)
        candidates.sort(key=lambda c: c["confidence"], reverse=True)

    store.transition(track_id, st.NEEDS_REVIEW, candidates=candidates, fp_matches=fp_matches)
    log(f"Track {track_id} ready for review with {len(candidates)} initial candidate(s)")


def _fingerprint_track(track_id, audio_path, cfg):
    try:
        fp_matches = fingerprint.identify(audio_path, maxlength=cfg["fingerprint_max_seconds"])
    except Exception as e:
        log(f"Fingerprinting failed for track {track_id}: {e}", "error")
        return []

    if fp_matches:
        top = fp_matches[0]
        log(f"Track {track_id}: fingerprint identified {len(fp_matches)} candidate(s), "
            f"top guess '{top['title']}' by '{top['artist']}' (score {top['score']})")
    else:
        log(f"Track {track_id}: fingerprint returned no matches "
            f"(check acoustid_api_key and fpcalc if this happens consistently)")
    return fp_matches


def find_more_matches(track_id, custom_query=None):
    """The "Find more" button: search more queries (the user's text, the
    remaining fingerprint guesses, the YouTube title) with more results per
    source, merge with existing candidates, and save the combined list."""
    track = store.get_track(track_id)
    if not track:
        raise KeyError(track_id)

    fp_matches = track.get("fp_matches") or []
    queries = []
    if custom_query:
        queries.append(custom_query)
    for m in fp_matches[:3]:
        queries.append(f"{m['artist']} {m['title']}".strip())
    queries.append(track.get("video_title") or "")
    queries = list(dict.fromkeys(q for q in queries if q))  # dedupe, keep order

    existing = track.get("candidates") or []
    seen = {_candidate_key(c) for c in existing}
    new_candidates = []
    for c in metadata_lookup.search_many(queries, limit_per_source=3):
        key = _candidate_key(c)
        if key in seen:
            continue
        seen.add(key)
        c["confidence"] = _confidence_for(c, fp_matches)
        new_candidates.append(c)

    combined = existing + new_candidates
    combined.sort(key=lambda c: c["confidence"], reverse=True)
    if store.update_track(track_id, candidates=combined) is None:
        raise KeyError(track_id)
    log(f"Track {track_id}: found {len(new_candidates)} additional candidate(s)")
    return combined


def _best_query(fp_matches, video_title):
    if fp_matches:
        top = fp_matches[0]
        q = f"{top['artist']} {top['title']}".strip()
        if q:
            return q
    return video_title


def _confidence_for(candidate, fp_matches):
    """A candidate that fuzzy-matches one of the fingerprint guesses gets
    that guess's acoustic score. Fuzzy, because a search result's title
    formatting (remaster tags, punctuation) is rarely identical to
    AcoustID's. Anything else is an unverified text hit and gets a flat
    0.4 so the UI can tell the two apart."""
    if not fp_matches:
        return 0.4

    cand_title = normalize_text(candidate.get("title"))
    cand_artist = normalize_text(candidate.get("artist"))

    best = 0.0
    for m in fp_matches:
        title_sim = SequenceMatcher(None, cand_title, normalize_text(m.get("title"))).ratio()
        if cand_artist and m.get("artist"):
            artist_sim = SequenceMatcher(None, cand_artist, normalize_text(m.get("artist"))).ratio()
        else:
            artist_sim = 0.5  # missing on one side: neither reward nor penalize
        if title_sim * 0.7 + artist_sim * 0.3 >= 0.55:
            best = max(best, m["score"])

    return best if best > 0 else 0.4


def _candidate_key(c):
    return (c["source"], c["title"].lower(), c["artist"].lower(), (c.get("album") or "").lower())


# ---------- Confirm / remove ----------

def confirm_track(track_id, selected_metadata, use_custom_art=True):
    """Tag the file with the confirmed metadata, move it to the output
    folder, then remove the card and its temp files.

    The needs_review -> tagging move is atomic, so a double-click can't tag
    twice. If tagging fails, the track goes back to needs_review with the
    error shown, so the user can fix things and confirm again."""
    track = store.transition(track_id, st.TAGGING, selected=selected_metadata, error_message=None)
    if not use_custom_art:
        track["custom_art_path"] = None
    try:
        output_path = tag_mp3.tag_and_move(track)
    except Exception as e:
        log(f"Tagging failed for track {track_id}: {e}", "error")
        store.transition(track_id, st.NEEDS_REVIEW, error_message=f"Tagging failed: {e}")
        raise
    store.remove_track(track_id)
    delete_temp_files(track_id)
    return output_path


def remove_track(track_id):
    """Delete a card and every temp file it produced. Raises KeyError if
    missing, store.InvalidTransition if a worker currently owns it."""
    track = store.remove_if_deletable(track_id)
    delete_temp_files(track_id)
    return track


# ---------- Temp files ----------

# Every temp file is named "<12-hex track id>" followed by "." or "_"
# (".mp3", "_art.jpg", "_custom_art.png", yt-dlp's ".webm.part", ...).
_TEMP_NAME = re.compile(r"^([0-9a-f]{12})[._]")


def delete_temp_files(track_id):
    temp_folder = load_config()["temp_folder"]
    for name in os.listdir(temp_folder):
        m = _TEMP_NAME.match(name)
        if m and m.group(1) == track_id:
            _try_remove(os.path.join(temp_folder, name))


def cleanup_orphan_temp_files():
    """At startup: delete temp files whose track no longer exists (left
    behind by older versions, or a crash)."""
    temp_folder = load_config()["temp_folder"]
    live = store.all_track_ids()
    removed = 0
    for name in os.listdir(temp_folder):
        m = _TEMP_NAME.match(name)
        if m and m.group(1) not in live:
            removed += _try_remove(os.path.join(temp_folder, name))
    if removed:
        log(f"Removed {removed} leftover temp file(s)")


def _try_remove(path):
    try:
        os.remove(path)
        return 1
    except OSError as e:
        log(f"Failed to delete {path}: {e}", "error")
        return 0
