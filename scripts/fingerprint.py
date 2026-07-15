"""
fingerprint.py - Identify a downloaded track via Chromaprint fingerprinting +
the AcoustID API (free, open-source stack).

Requires the `fpcalc` binary (ships with Chromaprint) to be on PATH, or
pointed to via config.json's "fpcalc_path".

IMPORTANT: we fingerprint the full downloaded audio file directly, capped at
`maxlength` seconds via fpcalc's own "-length" flag, rather than pre-trimming
a separate short clip with ffmpeg first. That distinction matters a lot:
fpcalc's "-length" only limits how much audio it *decodes for the fingerprint
calculation* — the DURATION it reports (and that we submit to AcoustID) still
reflects the file's true total length, read from the container itself. That
duration is a real scoring signal in AcoustID's matching algorithm, compared
against known recording lengths in its database. Pre-trimming a standalone
25-40 second clip file meant fpcalc correctly reported *that clip's own*
short duration — so we were telling AcoustID "this recording is 25 seconds
long" for songs that are actually several minutes long, which tanked match
scores to zero regardless of how clean the fingerprint itself was.
"""

import os

import requests

from utils import load_config, log

try:
    import acoustid
except ImportError:
    acoustid = None

ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"


def identify(audio_path, maxlength=60):
    """
    Fingerprint the track via pyacoustid (Chromaprint/fpcalc), then query the
    AcoustID lookup endpoint directly with `requests` rather than going
    through pyacoustid's lookup()/parse_lookup_result() wrappers — those have
    proven inconsistent across versions at surfacing AcoustID's actual error
    message on failure. Parsing the raw JSON ourselves guarantees we always
    see AcoustID's real error code and message when something fails.

    `maxlength` caps how much audio fpcalc decodes for speed — it does NOT
    affect the duration reported to AcoustID, which always reflects the
    file's true total length (see module docstring).

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

    if not os.path.exists(audio_path):
        log(f"Audio file not found for fingerprinting: {audio_path}", "error")
        return []

    os.environ.setdefault("FPCALC", cfg.get("fpcalc_path", "fpcalc"))

    try:
        duration, fingerprint_data = acoustid.fingerprint_file(audio_path, maxlength=maxlength)
        fp_len = len(fingerprint_data) if fingerprint_data else 0
        log(f"Fingerprint generated: reported duration={duration}s (true track length), "
            f"fingerprint length={fp_len} chars, decoded up to {maxlength}s for the calculation")
    except acoustid.NoBackendError:
        log("Chromaprint/fpcalc not found. Install Chromaprint and ensure fpcalc is on PATH.", "error")
        return []
    except acoustid.FingerprintGenerationError as e:
        log(f"Fingerprint generation failed: {e}", "error")
        return []

    # fpcalc returns the fingerprint as bytes. Decode it to a plain string
    # explicitly rather than handing bytes straight into requests' form-data
    # encoding alongside int/str values — mixed-type form data doesn't
    # always get encoded the way you'd expect.
    if isinstance(fingerprint_data, (bytes, bytearray)):
        fingerprint_str = fingerprint_data.decode("ascii")
    else:
        fingerprint_str = fingerprint_data

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
            f"message: {error.get('message', 'no message provided')}.",
            "error",
        )
        return []

    raw_results = response.get("results", [])
    linked = [r for r in raw_results if r.get("recordings")]
    log(f"AcoustID raw response: {len(raw_results)} fingerprint match(es) total, "
        f"{len(linked)} linked to recording metadata")

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

    best = {}
    for r in results:
        key = (r["title"].lower(), r["artist"].lower())
        if key not in best or r["score"] > best[key]["score"]:
            best[key] = r

    return sorted(best.values(), key=lambda r: r["score"], reverse=True)
