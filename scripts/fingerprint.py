"""
fingerprint.py - Extract a short clip from a downloaded track and identify it
via Chromaprint fingerprinting + the AcoustID API (free, open-source stack).

Requires the `fpcalc` binary (ships with Chromaprint) to be on PATH, or
pointed to via config.json's "fpcalc_path".
"""

import os
import subprocess

import requests

from utils import load_config, log

try:
    import acoustid
except ImportError:
    acoustid = None

ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"


def extract_clip(source_path, clip_path, offset_seconds, duration_seconds, ffmpeg_path="ffmpeg"):
    """
    Trim a short clip out of the middle of the track for fingerprinting.

    Always re-encodes rather than using "-acodec copy" (stream copy). Stream
    copy combined with "-ss" seeking is a known-flaky combination for MP3:
    ffmpeg can exit with code 0 (success) while producing a malformed clip —
    misaligned frame data that a decoder tolerates well enough to produce
    *a* fingerprint, just not one that corresponds to the real audio. Since
    that failure is silent (no non-zero exit code to catch), it was
    previously slipping through and causing every AcoustID lookup on the
    resulting clip to come back with zero matches, even for very well-known
    songs. Re-encoding costs a bit of extra time but avoids the whole
    failure class.
    """
    cmd = [
        ffmpeg_path, "-y",
        "-ss", str(offset_seconds),
        "-t", str(duration_seconds),
        "-i", source_path,
        clip_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg clip extraction failed: {result.stderr[-500:]}")

    if not os.path.exists(clip_path):
        raise RuntimeError("Failed to extract fingerprinting clip")

    # Sanity-check the output: a valid few-seconds-of-audio MP3 clip should
    # be at least a few KB. A near-empty file means something went wrong
    # even though ffmpeg reported success — better to catch it here with a
    # clear error than silently fingerprint garbage.
    clip_size = os.path.getsize(clip_path)
    if clip_size < 5000:
        raise RuntimeError(f"Extracted clip is suspiciously small ({clip_size} bytes) — likely corrupted or silent")
    return clip_path


def identify(clip_path):
    """
    Fingerprint the clip via pyacoustid (Chromaprint/fpcalc — this part is
    reliable and version-stable), then query the AcoustID lookup endpoint
    directly with `requests` rather than going through pyacoustid's
    lookup()/parse_lookup_result() wrappers. Those wrappers have proven
    inconsistent across versions at surfacing AcoustID's actual error
    message on failure (we were only ever seeing a terse "status: error"
    with no explanation) — parsing the raw JSON ourselves guarantees we
    always see AcoustID's real error code and message when something fails.

    Returns a list of candidates: [{"title", "artist", "score", "mb_recording_id"}]
    sorted by descending confidence score. Empty list if no match, no API
    key, or a request/generation error (all logged with the real reason).
    """
    cfg = load_config()
    api_key = cfg.get("acoustid_api_key", "")

    if not api_key or api_key == "YOUR_ACOUSTID_API_KEY":
        log("No AcoustID API key configured; skipping fingerprint identification", "error")
        return []

    if acoustid is None:
        log("pyacoustid not installed; skipping fingerprint identification", "error")
        return []

    os.environ.setdefault("FPCALC", cfg.get("fpcalc_path", "fpcalc"))
    log(f"Fingerprinting backend: chromaprint lib available={acoustid.have_chromaprint}, "
        f"audioread available={acoustid.have_audioread} "
        f"(expect both False so fpcalc.exe is used)")

    try:
        duration, fingerprint_data = acoustid.fingerprint_file(clip_path)
        fp_len = len(fingerprint_data) if fingerprint_data else 0
        log(f"Fingerprint generated: duration={duration}s, fingerprint length={fp_len} chars, "
            f"clip file size={os.path.getsize(clip_path) if os.path.exists(clip_path) else 'already deleted'} bytes")
    except acoustid.NoBackendError:
        log("Chromaprint/fpcalc not found. Install Chromaprint and ensure fpcalc is on PATH.", "error")
        return []
    except acoustid.FingerprintGenerationError as e:
        log(f"Fingerprint generation failed: {e}", "error")
        return []

    # fpcalc returns the fingerprint as bytes. Decode it to a plain string
    # explicitly rather than handing bytes straight into requests' form-data
    # encoding alongside int/str values — mixed-type form data doesn't
    # always get encoded the way you'd expect, and a fingerprint that's even
    # slightly mangled in transit will just look like "no match" with no
    # error, since the request itself is still perfectly well-formed.
    if isinstance(fingerprint_data, (bytes, bytearray)):
        fingerprint_str = fingerprint_data.decode("ascii")
    else:
        fingerprint_str = fingerprint_data
    log(f"Fingerprint preview (first 40 chars as sent): {fingerprint_str[:40]!r}")

    try:
        resp = requests.post(
            ACOUSTID_LOOKUP_URL,
            data={
                "client": api_key,
                "duration": int(duration),
                "fingerprint": fingerprint_str,
                "meta": "recordings",
                "format": "json",
            },
            timeout=15,
        )
        response = resp.json()
    except requests.RequestException as e:
        log(f"AcoustID request failed: {e}", "error")
        return []
    except ValueError:
        log(f"AcoustID returned a non-JSON response (HTTP {resp.status_code}): {resp.text[:300]}", "error")
        return []

    if response.get("status") != "ok":
        error = response.get("error", {})
        log(
            f"AcoustID lookup failed — code: {error.get('code', 'unknown')}, "
            f"message: {error.get('message', 'no message provided')}. "
            f"(Common causes: invalid/expired API key, or a malformed fingerprint request.)",
            "error",
        )
        return []

    raw_results = response.get("results", [])
    linked = [r for r in raw_results if r.get("recordings")]
    unlinked_scores = [round(r.get("score", 0), 3) for r in raw_results if not r.get("recordings")]
    log(
        f"AcoustID raw response: {len(raw_results)} fingerprint match(es) total, "
        f"{len(linked)} linked to recording metadata, "
        f"{len(unlinked_scores)} fingerprint-only (unlinked scores: {unlinked_scores})"
    )

    results = []
    for r in linked:
        score = r.get("score", 0)
        for rec in r.get("recordings", []):
            title = rec.get("title")
            if not title:
                continue
            artists = rec.get("artists") or []
            artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
            results.append({
                "title": title,
                "artist": artist,
                "score": round(score, 3),
                "mb_recording_id": rec.get("id"),
            })

    # De-dupe by (title, artist), keep highest score
    best = {}
    for r in results:
        key = (r["title"].lower(), r["artist"].lower())
        if key not in best or r["score"] > best[key]["score"]:
            best[key] = r

    return sorted(best.values(), key=lambda r: r["score"], reverse=True)
