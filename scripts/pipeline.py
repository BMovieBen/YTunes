"""
pipeline.py - Orchestrates the YTunes processing pipeline:
  1. Resolve a URL (video or playlist) into queue entries
  2. Download audio for a queued track
  3. Extract a clip and fingerprint/identify it via AcoustID
  4. Run a light initial metadata lookup (top pick per source) so review can
     start quickly without hammering every source up front
  5. On demand ("Find more matches"), run a broader search across more
     fingerprint guesses and more results per source
  6. Once the user confirms metadata (edited or as-is), tag and move the file

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
    Process every 'queued' track: download -> fingerprint -> light metadata
    lookup. Runs in a background thread so the web UI stays responsive. Only
    one processing run happens at a time.
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

    fp_matches = _fingerprint_track(track_id, audio_path, cfg)

    # Light initial lookup: one query, top-1 per source (at most 3 candidates
    # total) so review can start immediately instead of waiting on an
    # exhaustive multi-query sweep across every source.
    primary_query = _best_query(fp_matches, track["video_title"])
    candidates = []
    if primary_query:
        candidates = metadata_lookup.search_all(primary_query, limit_per_source=1)
        for r in candidates:
            r["confidence"] = _confidence_for(r, fp_matches)
        candidates.sort(key=lambda c: c["confidence"], reverse=True)

    # The YouTube video's own title/channel/thumbnail — always available as
    # a reference point, shown side-by-side with the best match rather than
    # folded into the scored candidate list.
    yt_meta = fetch_youtube.get_video_metadata(track["source_url"])
    youtube_reference = {
        "source": "youtube",
        "title": yt_meta.get("title") or track["video_title"],
        "artist": yt_meta.get("uploader") or "Unknown Artist",
        "album": "",
        "year": "",
        "genre": "",
        "track_number": None,
        "art_url": yt_meta.get("thumbnail"),
    }

    qm.update_track(
        track_id,
        candidates=candidates,
        youtube_reference=youtube_reference,
        fp_matches=fp_matches,
        status="needs_review",
    )
    log(f"Track {track_id} ready for review with {len(candidates)} initial candidate(s)")


def find_more_matches(track_id, custom_query=None):
    """
    On-demand broader search, triggered by the user clicking "Find more
    matches" (optionally with their own search text). Searches more queries
    (remaining fingerprint guesses + the raw YouTube title) and more results
    per source, merges with whatever candidates already exist, and persists
    the combined, deduped, re-sorted list.
    """
    track = qm.get_track(track_id)
    if not track:
        raise ValueError(f"Track {track_id} not found")

    fp_matches = track.get("fp_matches") or []
    queries = []
    if custom_query:
        queries.append(custom_query)
    for m in fp_matches[:3]:
        q = f"{m['artist']} {m['title']}".strip()
        if q and q not in queries:
            queries.append(q)
    if track.get("video_title") and track["video_title"] not in queries:
        queries.append(track["video_title"])

    existing = track.get("candidates") or []
    seen = {_candidate_key(c) for c in existing}

    new_candidates = []
    for query in queries:
        for r in metadata_lookup.search_all(query, limit_per_source=3):
            key = _candidate_key(r)
            if key in seen:
                continue
            seen.add(key)
            r["confidence"] = _confidence_for(r, fp_matches)
            new_candidates.append(r)

    combined = existing + new_candidates
    combined.sort(key=lambda c: c["confidence"], reverse=True)
    qm.update_track(track_id, candidates=combined)
    log(f"Track {track_id}: found {len(new_candidates)} additional candidate(s)")
    return combined


def _fingerprint_track(track_id, audio_path, cfg):
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
    return fp_matches


def _best_query(fp_matches, video_title):
    if fp_matches:
        top = fp_matches[0]
        q = f"{top['artist']} {top['title']}".strip()
        if q:
            return q
    return video_title


def _confidence_for(candidate, fp_matches):
    """
    Fingerprint-corroborated matches get the real acoustic match score.
    Everything else is an unverified text-search hit, scored lower and
    consistently so the UI can visually distinguish the two.
    """
    for m in fp_matches:
        if m["title"].strip().lower() == candidate["title"].strip().lower():
            return m["score"]
    return 0.4


def _candidate_key(c):
    return (c["source"], c["title"].lower(), c["artist"].lower(), (c.get("album") or "").lower())


def confirm_track(track_id, selected_metadata, custom_art_path=None):
    """
    Called once the user confirms the edit form (whatever values it holds,
    pre-filled or edited). Tags the file and moves it to the output folder.
    """
    track = qm.get_track(track_id)
    if not track:
        raise ValueError(f"Track {track_id} not found")

    qm.update_track(
        track_id,
        selected=selected_metadata,
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
