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

Processing starts automatically the moment a URL is added — no manual
"Process Queue" trigger. A single background worker thread handles the
queue; if one's already running when new tracks are added, they're picked
up by that same worker rather than needing a second trigger (see the
locking note on start_background_processing for how that race is avoided).
"""

import os
import re
import threading
from difflib import SequenceMatcher

import queue_manager as qm
import fetch_youtube
import fingerprint
import metadata_lookup
import tag_mp3
from utils import load_config, log

_worker_lock = threading.Lock()   # guards _worker_running only — held briefly, never during I/O
_worker_running = False


def add_url(url):
    """Resolve a URL (single video or playlist) and add each track to the queue,
    then immediately kick off background processing (or, if a worker is
    already running, it will pick these up automatically — see the locking
    note in start_background_processing)."""
    entries = fetch_youtube.resolve_urls(url)
    added = []
    for entry in entries:
        track = qm.add_track(entry["url"], entry["title"], entry.get("playlist_title"))
        added.append(track)
    if added:
        start_background_processing()
    return added


def start_background_processing():
    """
    Start the worker thread if one isn't already running. Whether a worker
    is running is tracked by _worker_running, which is only ever read or
    written while holding _worker_lock — and the worker's own decision to
    stop (see _worker_loop) uses that same lock for its final check. That's
    what prevents the race where a track gets added in the narrow window
    between the worker deciding the queue is empty and it actually exiting:
    that decision and this one can never happen at the same time, so a new
    track is guaranteed to be seen by either the still-running worker or a
    freshly spawned one — never neither.
    """
    global _worker_running
    with _worker_lock:
        if _worker_running:
            log("Background worker already running; new tracks will be picked up by it")
            return None
        _worker_running = True
    log("Starting background worker thread")
    thread = threading.Thread(target=_worker_loop, daemon=True)
    thread.start()
    return thread


def _worker_loop():
    global _worker_running
    processed_count = 0
    try:
        cfg = load_config()
        while True:
            pending = qm.list_tracks(status="queued")
            if not pending:
                # Atomic check-and-stop: only declare "done" if nothing was
                # added between our list_tracks() call above and grabbing
                # the lock here. If something was, fall through and keep going.
                with _worker_lock:
                    pending = qm.list_tracks(status="queued")
                    if not pending:
                        _worker_running = False
                        break
                    # else: something arrived just in time — keep processing
                    continue
            track = pending[0]
            log(f"Worker picked up track {track['id']}: {track.get('video_title')}")
            try:
                _process_single_track(track, cfg)
            except Exception as e:
                log(f"Error processing track {track['id']}: {e}", "error")
                qm.update_track(track["id"], status="error", error_message=str(e))
            processed_count += 1
        log(f"Processing run finished ({processed_count} track(s) handled)")
    except Exception:
        # Anything that escapes here (including load_config() failing, or a
        # bug in the queue-listing calls themselves) would otherwise kill
        # this daemon thread silently. Log the full traceback so it's
        # visible in ytunes.log even when the console window is minimized,
        # then make sure _worker_running doesn't get stuck as True forever.
        import traceback
        log(f"Background worker crashed:\n{traceback.format_exc()}", "error")
        with _worker_lock:
            _worker_running = False
        raise


def _process_single_track(track, cfg):
    track_id = track["id"]

    # Step 1: grab YouTube's own title/channel/thumbnail first — this is a
    # lightweight metadata call (no download), so the card can update with
    # real info almost immediately instead of sitting blank.
    qm.update_track(track_id, status="fetching_info")
    yt_meta = fetch_youtube.get_video_metadata(track["source_url"])
    title = yt_meta.get("title") or track["video_title"]
    is_standalone = not track.get("playlist_title")
    youtube_reference = {
        "source": "youtube",
        "title": title,
        "artist": yt_meta.get("uploader") or "Unknown Artist",
        # Singles aren't part of an album — using the song's own title reads
        # better than leaving it blank. Playlist tracks likely belong to
        # the playlist's own "album" in spirit, so use its title instead.
        "album": title if is_standalone else (track.get("playlist_title") or ""),
        "year": yt_meta.get("year") or "",
        "genre": "",
        "track_number": None,
        "art_url": yt_meta.get("thumbnail"),
    }
    qm.update_track(track_id, youtube_reference=youtube_reference, status="downloading")

    # Step 2: download the audio.
    audio_path = fetch_youtube.download_audio(track["source_url"], cfg["temp_folder"], track_id)
    qm.update_track(track_id, audio_path=audio_path)

    # Step 3: fingerprint + light initial metadata lookup (top-1 per source,
    # at most 3 candidates total) so review can start without waiting on an
    # exhaustive multi-query sweep across every source.
    qm.update_track(track_id, status="analyzing")
    fp_matches = _fingerprint_track(track_id, audio_path, cfg)

    primary_query = _best_query(fp_matches, track["video_title"])
    candidates = []
    if primary_query:
        candidates = metadata_lookup.search_all(primary_query, limit_per_source=1)
        for r in candidates:
            r["confidence"] = _confidence_for(r, fp_matches)
        candidates.sort(key=lambda c: c["confidence"], reverse=True)

    qm.update_track(
        track_id,
        candidates=candidates,
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
    """
    Try fingerprinting a few different windows of the track before giving
    up. AcoustID's match quality can vary a lot section-to-section within
    the very same recording — their crowd-sourced database tends to have
    denser fingerprint coverage over commonly-clipped regions (intros,
    hooks) than over less-frequently-submitted stretches. A single fixed
    offset is a bit of a gamble; trying a couple of alternate windows first
    is cheap compared to falling back to an unverified text-search guess.
    """
    base_offset = cfg.get("clip_offset_seconds", 10)
    duration = cfg.get("clip_duration_seconds", 40)
    ffmpeg_path = cfg.get("ffmpeg_path", "ffmpeg")

    # Configured window first, then a couple of alternates further into and
    # near the start of the track. Dedupe in case the configured offset
    # already happens to equal one of the fallbacks.
    attempts = []
    for offset in (base_offset, base_offset + 60, 5):
        if offset >= 0 and offset not in attempts:
            attempts.append(offset)

    fp_matches = []
    for i, offset in enumerate(attempts):
        clip_path = os.path.join(cfg["temp_folder"], f"{track_id}_clip.mp3")
        try:
            fingerprint.extract_clip(audio_path, clip_path, offset, duration, ffmpeg_path)
            fp_matches = fingerprint.identify(clip_path)
        except Exception as e:
            log(f"Fingerprinting attempt at offset {offset}s failed for track {track_id}: {e}", "error")
            fp_matches = []
        finally:
            if cfg.get("debug_keep_clips", False):
                if os.path.exists(clip_path):
                    log(f"debug_keep_clips is on — kept fingerprinting clip at {clip_path}")
            elif os.path.exists(clip_path):
                os.remove(clip_path)

        if fp_matches:
            log(f"Track {track_id}: fingerprint succeeded at offset {offset}s (attempt {i + 1}/{len(attempts)}), "
                f"top guess '{fp_matches[0]['title']}' by '{fp_matches[0]['artist']}' "
                f"(score {fp_matches[0]['score']})")
            break
        elif i < len(attempts) - 1:
            log(f"Track {track_id}: no match at offset {offset}s, trying another window")
        else:
            log(f"Track {track_id}: fingerprint returned no matches after trying offsets {attempts} "
                f"(check acoustid_api_key and fpcalc if this happens consistently)")

    return fp_matches


def _best_query(fp_matches, video_title):
    if fp_matches:
        top = fp_matches[0]
        q = f"{top['artist']} {top['title']}".strip()
        if q:
            return q
    return video_title


def _normalize(s):
    """Lowercase, strip parenthetical/bracketed annotations like '(Remastered
    2011)' or '[Official Video]', drop punctuation, collapse whitespace —
    enough to compare titles that mean the same thing but aren't identical
    strings."""
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _confidence_for(candidate, fp_matches):
    """
    Fingerprint-corroborated matches get the real acoustic match score —
    but "corroborated" is judged by fuzzy title/artist similarity, not exact
    string equality, since a metadata search result's title formatting
    (remaster tags, punctuation, etc.) is rarely identical to AcoustID's.
    Everything else is an unverified text-search hit, scored at a flat,
    consistently lower default so the UI can visually tell the two apart.
    """
    if not fp_matches:
        return 0.4

    cand_title = _normalize(candidate.get("title"))
    cand_artist = _normalize(candidate.get("artist"))

    best = 0.0
    for m in fp_matches:
        title_sim = SequenceMatcher(None, cand_title, _normalize(m.get("title"))).ratio()
        if cand_artist and m.get("artist"):
            artist_sim = SequenceMatcher(None, cand_artist, _normalize(m.get("artist"))).ratio()
        else:
            artist_sim = 0.5  # missing artist on one side — don't penalize or reward
        combined = title_sim * 0.7 + artist_sim * 0.3
        if combined >= 0.55:
            best = max(best, m["score"])

    return best if best > 0 else 0.4


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
