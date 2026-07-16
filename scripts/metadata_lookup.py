"""
metadata_lookup.py - Search iTunes, MusicBrainz, and Discogs for track metadata,
in that fallback order (configurable), and normalize results into a common
candidate shape for the queue UI.

Each candidate:
{
  "source": "itunes" | "musicbrainz" | "discogs",
  "title": str,
  "artist": str,
  "album": str,
  "year": str,
  "genre": str,
  "track_number": int|None,
  "art_url": str|None,
  "preview_url": str|None,  # 30s audio preview clip — only iTunes provides this
  "confidence": float  # 0-1, from fingerprint score if available, else 0.4 default
}
"""

import re
from difflib import SequenceMatcher

import requests

from utils import load_config, log

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
MUSICBRAINZ_SEARCH_URL = "https://musicbrainz.org/ws/2/recording/"
MUSICBRAINZ_COVERART_URL = "https://coverartarchive.org/release/{mbid}/front"
DISCOGS_SEARCH_URL = "https://api.discogs.com/database/search"
DISCOGS_RELEASE_URL = "https://api.discogs.com/releases"


def _headers():
    cfg = load_config()
    return {"User-Agent": cfg.get("user_agent", "YTunes/1.0")}


def search_itunes(query, limit=5):
    try:
        resp = requests.get(
            ITUNES_SEARCH_URL,
            params={"term": query, "media": "music", "entity": "song", "limit": limit},
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log(f"iTunes search failed for '{query}': {e}", "error")
        return []

    out = []
    for item in data.get("results", []):
        art = item.get("artworkUrl100", "")
        if art:
            art = art.replace("100x100bb", "1200x1200bb")  # request higher-res art
        out.append({
            "source": "itunes",
            "title": item.get("trackName", ""),
            "artist": item.get("artistName", ""),
            "album": item.get("collectionName", ""),
            "year": (item.get("releaseDate") or "")[:4],
            "genre": item.get("primaryGenreName", ""),
            "track_number": item.get("trackNumber"),
            "art_url": art or None,
            "preview_url": item.get("previewUrl") or None,
        })
    return out


def search_musicbrainz(query, limit=5):
    try:
        resp = requests.get(
            MUSICBRAINZ_SEARCH_URL,
            params={"query": query, "fmt": "json", "limit": limit},
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log(f"MusicBrainz search failed for '{query}': {e}", "error")
        return []

    out = []
    for rec in data.get("recordings", []):
        artist = ", ".join(c.get("name", "") for c in rec.get("artist-credit", []) if c.get("name"))
        release = (rec.get("releases") or [{}])[0]
        mbid = release.get("id")
        out.append({
            "source": "musicbrainz",
            "title": rec.get("title", ""),
            "artist": artist,
            "album": release.get("title", ""),
            "year": (release.get("date") or "")[:4],
            "genre": "",
            "track_number": None,
            "art_url": MUSICBRAINZ_COVERART_URL.format(mbid=mbid) if mbid else None,
            "preview_url": None,
        })
    return out


def search_discogs(query, limit=5):
    cfg = load_config()
    token = cfg.get("discogs_token", "")
    if not token or token == "YOUR_DISCOGS_PERSONAL_ACCESS_TOKEN":
        log("No Discogs token configured; skipping Discogs search", "error")
        return []

    try:
        resp = requests.get(
            DISCOGS_SEARCH_URL,
            params={"q": query, "type": "release", "per_page": limit, "token": token},
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log(f"Discogs search failed for '{query}': {e}", "error")
        return []

    out = []
    for item in data.get("results", []):
        title_full = item.get("title", "")  # "Artist - Release Title" — a RELEASE name (album/single/EP), not a track
        artist, _, release_title = title_full.partition(" - ")

        track_title, track_number = _best_matching_track(item.get("id"), query, token, fallback=release_title)

        out.append({
            "source": "discogs",
            "title": track_title,
            "artist": artist,
            "album": release_title or title_full,
            "year": str(item.get("year", "")),
            "genre": ", ".join(item.get("genre", [])) if item.get("genre") else "",
            "track_number": track_number,
            "art_url": item.get("cover_image") or None,
            "preview_url": None,
        })
    return out


def _best_matching_track(release_id, query, token, fallback):
    """
    Discogs' search API works at the release (album/single/EP) level — its
    'title' field is the release's own name, not any specific song. A
    search for "Artist Song Title" can easily match an ALBUM whose title
    has nothing to do with the song, just because that song appears
    somewhere in its tracklist. Fetch the release's actual tracklist and
    pick whichever track title is the closest textual match to the search
    query, so we return a real song title instead of accidentally treating
    an album/EP name as one.
    """
    if not release_id:
        return fallback, None

    try:
        resp = requests.get(
            f"{DISCOGS_RELEASE_URL}/{release_id}",
            params={"token": token},
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log(f"Discogs release lookup failed for release {release_id}: {e}", "error")
        return fallback, None

    tracklist = [t for t in (data.get("tracklist") or []) if t.get("title")]
    if not tracklist:
        return fallback, None

    query_norm = _normalize(query)
    best_track = None
    best_score = 0.0
    for t in tracklist:
        score = SequenceMatcher(None, query_norm, _normalize(t["title"])).ratio()
        if score > best_score:
            best_score = score
            best_track = t

    if best_track is None or best_score < 0.35:
        # Nothing in the tracklist resembles the query closely enough to
        # trust — fall back to the release title rather than guessing.
        return fallback, None

    position = best_track.get("position")
    track_number = int(position) if position and str(position).isdigit() else None
    return best_track["title"], track_number


def _normalize(s):
    """Lowercase, strip parenthetical/bracketed annotations, drop
    punctuation, collapse whitespace — for comparing a search query against
    tracklist titles that are rarely formatted identically."""
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_SOURCE_FUNCS = {
    "itunes": search_itunes,
    "musicbrainz": search_musicbrainz,
    "discogs": search_discogs,
}


def search_all(query, limit_per_source=5):
    """
    Query each configured source in fallback order. Unlike a strict fallback
    (stop at first hit), we gather from ALL sources so the user has a full
    result list to choose from, but sources appear in the configured
    priority order.
    """
    cfg = load_config()
    order = cfg.get("metadata_source_order", ["itunes", "musicbrainz", "discogs"])

    candidates = []
    for source in order:
        func = _SOURCE_FUNCS.get(source)
        if not func:
            continue
        results = func(query, limit_per_source)
        candidates.extend(results)

    return candidates


def download_art(url, dest_path):
    """Download album art to dest_path. Returns True on success."""
    try:
        resp = requests.get(url, headers=_headers(), timeout=15)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        return True
    except requests.RequestException as e:
        log(f"Failed to download art from {url}: {e}", "error")
        return False
