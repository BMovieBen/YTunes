"""
metadata_lookup.py - Search iTunes, MusicBrainz, and Discogs for track metadata
and normalize results into a common candidate shape for the review UI.

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
  "confidence": float       # added by pipeline.py
}

Sources are searched in parallel (one thread per source), but each source's
own requests go out one at a time with a minimum gap between them, to stay
inside its rate limit (MusicBrainz: 1/sec; Discogs: 60/min).
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

import requests

from utils import load_config, log, normalize_text

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
MUSICBRAINZ_SEARCH_URL = "https://musicbrainz.org/ws/2/recording/"
MUSICBRAINZ_COVERART_URL = "https://coverartarchive.org/release/{mbid}/front"
DISCOGS_SEARCH_URL = "https://api.discogs.com/database/search"
DISCOGS_RELEASE_URL = "https://api.discogs.com/releases"


class _RateLimiter:
    """Enforces a minimum interval between calls, across threads."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                time.sleep(delay)
            self._next_allowed = max(now, self._next_allowed) + self.min_interval


_LIMITS = {
    "itunes": _RateLimiter(0.5),
    "musicbrainz": _RateLimiter(1.1),
    "discogs": _RateLimiter(1.0),
}

_local = threading.local()

# Long-lived pool (one thread per source) so each thread's Session, and its
# open connections, get reused across searches.
_POOL = ThreadPoolExecutor(max_workers=len(_LIMITS), thread_name_prefix="lookup")


def _session():
    """One requests.Session per thread (connection reuse, shared headers)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = load_config().get("user_agent") or "YTunes/1.0"
        _local.session = s
    return s


_RETRY_STATUSES = (429, 503)   # "slow down" / "busy" — worth a short wait and retry
_RETRY_DELAYS = (1.5, 4.0)


def _get_json(source, url, params=None, headers=None, timeout=10):
    for delay in _RETRY_DELAYS + (None,):
        _LIMITS[source].wait()
        resp = _session().get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code in _RETRY_STATUSES and delay is not None:
            log(f"{source} returned {resp.status_code}; retrying in {delay}s", "warning")
            time.sleep(delay)
            continue
        resp.raise_for_status()
        return resp.json()


# ---------- Sources ----------

def search_itunes(query, limit=5):
    try:
        data = _get_json("itunes", ITUNES_SEARCH_URL,
                         params={"term": query, "media": "music", "entity": "song", "limit": limit})
    except (requests.RequestException, ValueError) as e:
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
        data = _get_json("musicbrainz", MUSICBRAINZ_SEARCH_URL,
                         params={"query": query, "fmt": "json", "limit": limit})
    except (requests.RequestException, ValueError) as e:
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


def _discogs_headers():
    # Token goes in a header, never the URL, so it can't end up in logs.
    return {"Authorization": f"Discogs token={load_config()['discogs_token']}"}


def search_discogs(query, limit=5):
    if not load_config().get("discogs_token"):
        log("No Discogs token configured; skipping Discogs search", "warning")
        return []

    try:
        data = _get_json("discogs", DISCOGS_SEARCH_URL,
                         params={"q": query, "type": "release", "per_page": limit},
                         headers=_discogs_headers())
    except (requests.RequestException, ValueError) as e:
        log(f"Discogs search failed for '{query}': {e}", "error")
        return []

    out = []
    for item in data.get("results", []):
        # "Artist - Release Title": a RELEASE name (album/single/EP), not a track
        title_full = item.get("title", "")
        artist, _, release_title = title_full.partition(" - ")
        track_title, track_number = _best_matching_track(item.get("id"), query, fallback=release_title)
        out.append({
            "source": "discogs",
            "title": track_title,
            "artist": artist,
            "album": release_title or title_full,
            "year": str(item.get("year", "")),
            "genre": ", ".join(item.get("genre") or []),
            "track_number": track_number,
            "art_url": item.get("cover_image") or None,
            "preview_url": None,
        })
    return out


def _best_matching_track(release_id, query, fallback):
    """
    Discogs search works at the release level — its 'title' is the album/
    single/EP name, not a song. A search for "Artist Song" can match an
    album just because the song is somewhere on it. So fetch the release's
    tracklist and return whichever track title best matches the query,
    instead of treating the album name as a song title.
    """
    if not release_id:
        return fallback, None

    try:
        data = _get_json("discogs", f"{DISCOGS_RELEASE_URL}/{release_id}", headers=_discogs_headers())
    except (requests.RequestException, ValueError) as e:
        log(f"Discogs release lookup failed for release {release_id}: {e}", "error")
        return fallback, None

    return pick_track_from_tracklist(data.get("tracklist") or [], query, fallback)


def pick_track_from_tracklist(tracklist, query, fallback):
    """Best fuzzy match for `query` in a Discogs tracklist, as
    (title, track_number). Falls back if nothing is close enough."""
    query_norm = normalize_text(query)
    best_track, best_score = None, 0.0
    for t in tracklist:
        if not t.get("title"):
            continue
        score = SequenceMatcher(None, query_norm, normalize_text(t["title"])).ratio()
        if score > best_score:
            best_track, best_score = t, score

    if best_track is None or best_score < 0.35:
        # Nothing resembles the query closely enough to trust.
        return fallback, None

    position = str(best_track.get("position") or "")
    return best_track["title"], int(position) if position.isdigit() else None


_SOURCE_FUNCS = {
    "itunes": search_itunes,
    "musicbrainz": search_musicbrainz,
    "discogs": search_discogs,
}


# ---------- Combined search ----------

def search_many(queries, limit_per_source=5):
    """Run every query against every configured source. Sources run in
    parallel; within a source, queries run in order (rate-limited).
    Results come back grouped by source in the configured priority order,
    then by query order."""
    order = [s for s in load_config()["metadata_source_order"] if s in _SOURCE_FUNCS]
    if not order or not queries:
        return []

    def run_source(source):
        results = []
        for q in queries:
            try:
                results.extend(_SOURCE_FUNCS[source](q, limit_per_source))
            except Exception as e:  # one broken source shouldn't sink the others
                log(f"{source} search crashed for '{q}': {e}", "error")
        return results

    per_source = list(_POOL.map(run_source, order))
    return [c for results in per_source for c in results]


def search_all(query, limit_per_source=5):
    """Search every configured source for one query."""
    return search_many([query], limit_per_source)


def download_art(url, dest_path):
    """Download album art to dest_path. Returns True on success."""
    try:
        resp = _session().get(url, timeout=15)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        return True
    except (requests.RequestException, OSError) as e:
        log(f"Failed to download art from {url}: {e}", "error")
        return False
