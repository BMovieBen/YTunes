"""
fingerprint.py - Extract a short clip from a downloaded track and identify it
via Chromaprint fingerprinting + the AcoustID API (free, open-source stack).

Requires the `fpcalc` binary (ships with Chromaprint) to be on PATH, or
pointed to via config.json's "fpcalc_path".
"""

import os
import subprocess

from utils import load_config, log

try:
    import acoustid
except ImportError:
    acoustid = None


def extract_clip(source_path, clip_path, offset_seconds, duration_seconds, ffmpeg_path="ffmpeg"):
    """Trim a short clip out of the middle of the track for fingerprinting."""
    cmd = [
        ffmpeg_path, "-y",
        "-ss", str(offset_seconds),
        "-t", str(duration_seconds),
        "-i", source_path,
        "-acodec", "copy",
        clip_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
    except subprocess.CalledProcessError:
        # Fall back to re-encoding if stream copy fails (e.g. offset lands mid-frame oddly)
        cmd_reencode = [
            ffmpeg_path, "-y",
            "-ss", str(offset_seconds),
            "-t", str(duration_seconds),
            "-i", source_path,
            clip_path,
        ]
        subprocess.run(cmd_reencode, capture_output=True, text=True, timeout=60, check=True)

    if not os.path.exists(clip_path):
        raise RuntimeError("Failed to extract fingerprinting clip")
    return clip_path


def identify(clip_path):
    """
    Fingerprint the clip and query AcoustID.
    Returns a list of candidates: [{"title", "artist", "score", "mb_recording_id"}]
    sorted by descending confidence score. Empty list if no match or no API key set.
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

    results = []
    try:
        for score, recording_id, title, artist in acoustid.match(api_key, clip_path):
            if not title:
                continue
            results.append({
                "title": title,
                "artist": artist or "",
                "score": round(score, 3),
                "mb_recording_id": recording_id,
            })
    except acoustid.NoBackendError:
        log("Chromaprint/fpcalc not found. Install Chromaprint and ensure fpcalc is on PATH.", "error")
    except acoustid.FingerprintGenerationError as e:
        log(f"Fingerprint generation failed: {e}", "error")
    except acoustid.WebServiceError as e:
        log(f"AcoustID web service error: {e}", "error")

    # De-dupe by (title, artist), keep highest score
    best = {}
    for r in results:
        key = (r["title"].lower(), r["artist"].lower())
        if key not in best or r["score"] > best[key]["score"]:
            best[key] = r

    return sorted(best.values(), key=lambda r: r["score"], reverse=True)
