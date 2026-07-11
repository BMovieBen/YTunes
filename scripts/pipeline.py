"""
pipeline.py - Orchestrates the YTunes processing pipeline:
  1. Resolve a URL (video or playlist) into queue entries
  2. Download audio for a queued track
  3. Extract a clip and fingerprint/identify it via AcoustID
  4. Search iTunes/MusicBrainz/Discogs (fallback order) for metadata candidates
  5. Once the user confirms a match (or types one manually), tag and move the file

Mirrors Article2Pod's pipeline.py structure: explicit "Generate/Process" trigger
rather than fully automatic background processing, so the user stays in control.
"""

import os
import threading

import queue_manager as qm
import fetch_youtube
import fingerprint
import metadata_lookup
import tag_mp3
from utils import load_config, log

_processing_lock = threading.Lock()


def add_url(url):
    """Resolve a URL (single video or playlist) and add each track to the queue."""
    entries = fetch_youtube.resolve_urls(url)
    added = []
    for entry in entries:
        track = qm.add_track(entry["url"], entry["title"], entry.get("playlist_title"))
        added.append(track)
    return added


def process_pending():
    """
    Process every 'queued' track: download -> fingerprint -> search candidates.
    Runs in a background thread so the web UI stays responsive. Only one
    processing run happens at a time.
    """
    if not _processing_lock.acquire(blocking=False):
        log("Processing already in progress; ignoring duplicate trigger")
        return
    try:
        cfg = load_config()
        pending = qm.list_tracks(status="queued")
        log(f"Starting processing run for {len(pending)} queued track(s)")
        for track in pending:
            try:
                _process_single_track(track, cfg)
            except Exception as e:
                log(f"Error processing track {track['id']}: {e}", "error")
                qm.update_track(track["id"], status="error", error_message=str(e))
    finally:
        _processing_lock.release()


def _process_single_track(track, cfg):
    track_id = track["id"]

    qm.update_track(track_id, status="downloading")
    audio_path = fetch_youtube.download_audio(track["source_url"], cfg["temp_folder"], track_id)
    qm.update_track(track_id, audio_path=audio_path)

    qm.update_track(track_id, status="analyzing")
    candidates = _gather_candidates(track_id, audio_path, track["video_title"], cfg)

    # Always add a YouTube-sourced fallback (title + channel + thumbnail),
    # so there's a usable starting point even when no metadata source matches.
    yt_meta = fetch_youtube.get_video_metadata(track["source_url"])
    fallback = {
        "source": "youtube",
        "title": yt_meta.get("title") or track["video_title"],
        "artist": yt_meta.get("uploader") or "Unknown Artist",
        "album": "",
        "year": "",
        "genre": "",
        "track_number": None,
        "art_url": yt_meta.get("thumbnail"),
        "confidence": 0.1,
    }
    candidates.append(fallback)

    qm.update_track(track_id, candidates=candidates, status="needs_review")
    log(f"Track {track_id} ready for review with {len(candidates)} candidate(s)")


def _gather_candidates(track_id, audio_path, video_title, cfg):
    clip_path = os.path.join(cfg["temp_folder"], f"{track_id}_clip.mp3")
    fp_matches = []
    try:
        fingerprint.extract_clip(
            audio_path, clip_path,
            cfg.get("clip_offset_seconds", 20),
            cfg.get("clip_duration_seconds", 25),
            cfg.get("ffmpeg_path", "ffmpeg"),
        )
        fp_matches = fingerprint.identify(clip_path)
    except Exception as e:
        log(f"Fingerprinting failed for track {track_id}, falling back to title search: {e}", "error")
    finally:
        if os.path.exists(clip_path):
            os.remove(clip_path)

    # Build search queries: fingerprint guesses first (higher trust), then the
    # raw YouTube title as a fallback so the user always has *something* to pick from.
    queries = []
    for match in fp_matches[:3]:
        q = f"{match['artist']} {match['title']}".strip()
        if q and q not in queries:
            queries.append(q)
    if video_title and video_title not in queries:
        queries.append(video_title)

    candidates = []
    seen = set()
    for query in queries:
        results = metadata_lookup.search_all(query, limit_per_source=3)
        for r in results:
            key = (r["source"], r["title"].lower(), r["artist"].lower(), r["album"].lower())
            if key in seen:
                continue
            seen.add(key)
            # Attach fingerprint confidence if this candidate matches a fingerprint guess
            r["confidence"] = 0.5
            for m in fp_matches:
                if m["title"].lower() == r["title"].lower():
                    r["confidence"] = m["score"]
                    break
            candidates.append(r)

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates


def confirm_track(track_id, selected_metadata, manual_override=False, custom_art_path=None):
    """
    Called once the user has picked a candidate (or typed one manually) in the UI.
    Tags the file and moves it to the output folder.
    """
    track = qm.get_track(track_id)
    if not track:
        raise ValueError(f"Track {track_id} not found")

    qm.update_track(
        track_id,
        selected=selected_metadata,
        manual_override=manual_override,
        custom_art_path=custom_art_path,
        status="tagging",
    )
    track = qm.get_track(track_id)

    output_path = tag_mp3.tag_and_move(track)
    qm.update_track(track_id, status="complete", output_path=output_path)
    return output_path


def start_background_processing():
    """Kick off process_pending() in a background thread (non-blocking for Flask)."""
    thread = threading.Thread(target=process_pending, daemon=True)
    thread.start()
    return thread
