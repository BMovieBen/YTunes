"""
store.py - Persistent track list, backed by SQLite.

Replaces the old log/queue.json. Every read-modify-write happens inside one
lock + one transaction, so the background worker and web requests can't
overwrite each other's changes, and a crash mid-write can't corrupt the
data. An existing queue.json is imported once, then renamed.

Track fields (as returned to callers, a plain dict):
  id                 str
  version            int    bumped on every change (the UI uses it to skip
                            re-rendering cards that didn't change)
  source_url         str    YouTube URL (the raw URL as typed until resolved)
  video_id           str|None  11-char YouTube ID, for duplicate detection
  playlist_title     str|None
  video_title        str    display title (the raw URL until resolved)
  resolved           bool
  status             str    see statuses.py
  error_message      str|None
  audio_path         str|None  downloaded MP3 in the temp folder
  youtube_reference  dict|None the video's own title/channel/thumbnail
  fp_matches         list   raw fingerprint guesses
  candidates         list   metadata match candidates
  selected           dict|None the metadata the user confirmed
  custom_art_path    str|None  user-uploaded album art
  added_at           str
"""

import json
import os
import sqlite3
import threading

import statuses as st
from utils import load_config, log, new_track_id, now_iso, youtube_video_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    id                TEXT NOT NULL UNIQUE,
    version           INTEGER NOT NULL DEFAULT 1,
    source_url        TEXT NOT NULL,
    video_id          TEXT,
    playlist_title    TEXT,
    video_title       TEXT,
    resolved          INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    error_message     TEXT,
    audio_path        TEXT,
    youtube_reference TEXT,
    fp_matches        TEXT NOT NULL DEFAULT '[]',
    candidates        TEXT NOT NULL DEFAULT '[]',
    selected          TEXT,
    custom_art_path   TEXT,
    added_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_video_id ON tracks(video_id);
"""

_JSON_FIELDS = ("youtube_reference", "fp_matches", "candidates", "selected")
_BOOL_FIELDS = ("resolved",)
_UPDATABLE = {
    "source_url", "video_id", "playlist_title", "video_title", "resolved", "status",
    "error_message", "audio_path", "youtube_reference", "fp_matches", "candidates",
    "selected", "custom_art_path",
}

_lock = threading.RLock()
_conn = None


class InvalidTransition(Exception):
    """Raised when a status change isn't allowed from the track's current status."""


# ---------- Connection / setup ----------

def init(db_path=None, legacy_queue_path="auto"):
    """Open (creating if needed) the database. Safe to call more than once;
    passing a different db_path reopens (used by tests). An old queue.json
    next to the database is imported once ("auto"); pass None to skip."""
    global _conn
    db_path = db_path or load_config()["db_path"]
    if legacy_queue_path == "auto":
        legacy_queue_path = os.path.join(os.path.dirname(db_path), "queue.json")
    with _lock:
        if _conn is not None:
            _conn.close()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        _conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(_SCHEMA)
        if legacy_queue_path:
            _import_legacy_queue(legacy_queue_path)


def close():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _db():
    if _conn is None:
        init()
    return _conn


class _transaction:
    """Lock + BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""

    def __enter__(self):
        _lock.acquire()
        self.conn = _db()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            _lock.release()
        return False


def _import_legacy_queue(path):
    """One-time import of the old queue.json, then rename it so it's never
    imported twice."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            tracks = json.load(f).get("tracks", [])
    except (OSError, ValueError) as e:
        log(f"Couldn't read legacy {path}; leaving it in place: {e}", "error")
        return

    with _transaction() as conn:
        for t in tracks:
            if conn.execute("SELECT 1 FROM tracks WHERE id = ?", (t["id"],)).fetchone():
                continue
            status = t.get("status", st.QUEUED)
            if status not in st.ALL:  # e.g. the long-gone "complete"
                continue
            row = _new_row(
                t["source_url"], t.get("video_title") or t["source_url"],
                t.get("playlist_title"), bool(t.get("resolved", True)),
            )
            row.update({
                "id": t["id"],
                "status": status,
                "error_message": t.get("error_message"),
                "audio_path": t.get("audio_path"),
                "youtube_reference": t.get("youtube_reference"),
                "fp_matches": t.get("fp_matches") or [],
                "candidates": t.get("candidates") or [],
                "selected": t.get("selected"),
                "custom_art_path": t.get("custom_art_path"),
                "added_at": t.get("added_at") or row["added_at"],
            })
            _insert(conn, row)
    os.replace(path, path + ".imported")
    log(f"Imported {len(tracks)} track(s) from {path} into the database")


# ---------- Row helpers ----------

def _new_row(source_url, video_title, playlist_title, resolved):
    return {
        "id": new_track_id(),
        "source_url": source_url,
        "video_id": youtube_video_id(source_url),
        "playlist_title": playlist_title,
        "video_title": video_title,
        "resolved": resolved,
        "status": st.QUEUED if resolved else st.RESOLVING,
        "error_message": None,
        "audio_path": None,
        "youtube_reference": None,
        "fp_matches": [],
        "candidates": [],
        "selected": None,
        "custom_art_path": None,
        "added_at": now_iso(),
    }


def _encode(field, value):
    if field in _JSON_FIELDS:
        return None if value is None else json.dumps(value)
    if field in _BOOL_FIELDS:
        return int(bool(value))
    return value


def _decode(row):
    if row is None:
        return None
    d = dict(row)
    d.pop("seq", None)
    for f in _JSON_FIELDS:
        if d[f] is not None:
            d[f] = json.loads(d[f])
    for f in _BOOL_FIELDS:
        d[f] = bool(d[f])
    return d


def _insert(conn, row):
    cols = list(row)
    conn.execute(
        f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [_encode(c, row[c]) for c in cols],
    )


def _get(conn, track_id):
    return _decode(conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone())


def _update(conn, track_id, fields):
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"Unknown track field(s): {', '.join(sorted(unknown))}")
    if "source_url" in fields and "video_id" not in fields:
        fields = dict(fields, video_id=youtube_video_id(fields["source_url"]))
    sets = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(
        f"UPDATE tracks SET {sets}, version = version + 1 WHERE id = ?",
        [_encode(k, v) for k, v in fields.items()] + [track_id],
    )
    return cur.rowcount > 0


# ---------- Public API ----------

def add_track(source_url, video_title, playlist_title=None, resolved=True):
    row = _new_row(source_url, video_title, playlist_title, resolved)
    with _transaction() as conn:
        _insert(conn, row)
        track = _get(conn, row["id"])
    log(f"Added track {track['id']} (resolved={resolved}): {video_title}")
    return track


def replace_with_entries(track_id, entries):
    """Atomically swap a playlist placeholder for one queued track per entry
    ({"url", "title", "playlist_title"}). Returns the new tracks."""
    with _transaction() as conn:
        conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        rows = [_new_row(e["url"], e["title"], e.get("playlist_title"), True) for e in entries]
        for row in rows:
            _insert(conn, row)
        return [_get(conn, r["id"]) for r in rows]


def get_track(track_id):
    with _lock:
        return _get(_db(), track_id)


def list_tracks(status=None):
    """All tracks (oldest first), optionally filtered to one status."""
    with _lock:
        if status is None:
            rows = _db().execute("SELECT * FROM tracks ORDER BY seq").fetchall()
        else:
            rows = _db().execute("SELECT * FROM tracks WHERE status = ? ORDER BY seq", (status,)).fetchall()
        return [_decode(r) for r in rows]


def update_track(track_id, **fields):
    """Update fields (not status changes that need checking — use
    transition() for those). Returns the updated track, or None if the
    track no longer exists."""
    with _transaction() as conn:
        if not _update(conn, track_id, fields):
            log(f"update_track: track {track_id} not found", "warning")
            return None
        return _get(conn, track_id)


def transition(track_id, new_status, **fields):
    """Atomically move a track to new_status (plus any other fields), but
    only if that's allowed from its current status. Raises KeyError if the
    track is gone, InvalidTransition if the move isn't allowed."""
    with _transaction() as conn:
        track = _get(conn, track_id)
        if track is None:
            raise KeyError(track_id)
        if not st.can_transition(track["status"], new_status):
            raise InvalidTransition(f"Can't go from {track['status']} to {new_status}")
        _update(conn, track_id, dict(fields, status=new_status))
        return _get(conn, track_id)


def claim_next_pending():
    """Pick the next track for the worker. A queued track is moved to
    fetching_info in the same transaction, so it can't be deleted between
    being picked and being worked on. A resolving track is returned as-is
    (it's already not deletable)."""
    with _transaction() as conn:
        for status in st.PENDING:
            row = conn.execute(
                "SELECT id FROM tracks WHERE status = ? ORDER BY seq LIMIT 1", (status,)
            ).fetchone()
            if row is None:
                continue
            if status == st.QUEUED:
                _update(conn, row["id"], {"status": st.FETCHING_INFO})
            return _get(conn, row["id"])
    return None


def has_pending():
    with _lock:
        marks = ",".join("?" for _ in st.PENDING)
        return _db().execute(
            f"SELECT 1 FROM tracks WHERE status IN ({marks}) LIMIT 1", st.PENDING
        ).fetchone() is not None


def remove_track(track_id):
    with _transaction() as conn:
        return conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,)).rowcount > 0


def remove_if_deletable(track_id):
    """Atomically delete a track unless a worker owns it. Returns the
    deleted track. Raises KeyError if missing, InvalidTransition if busy."""
    with _transaction() as conn:
        track = _get(conn, track_id)
        if track is None:
            raise KeyError(track_id)
        if not st.is_deletable(track["status"]):
            raise InvalidTransition(f"Can't remove a track while it's {track['status']}")
        conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        return track


def existing_video_ids(video_ids):
    """Which of these YouTube video IDs are already on a card."""
    ids = [v for v in video_ids if v]
    if not ids:
        return set()
    with _lock:
        found = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            found.update(
                r["video_id"] for r in _db().execute(
                    f"SELECT video_id FROM tracks WHERE video_id IN ({marks})", chunk
                )
            )
        return found


def all_track_ids():
    with _lock:
        return {r["id"] for r in _db().execute("SELECT id FROM tracks")}


def reset_interrupted():
    """At startup: anything left mid-flight when the app last stopped has no
    in-memory progress to resume, so put it back somewhere re-runnable (see
    statuses.RESET_ON_STARTUP)."""
    changed = 0
    with _transaction() as conn:
        for old, new in st.RESET_ON_STARTUP.items():
            changed += conn.execute(
                "UPDATE tracks SET status = ?, version = version + 1 WHERE status = ?", (new, old)
            ).rowcount
    if changed:
        log(f"Reset {changed} interrupted track(s) after restart")
    return changed
