"""
app.py - Flask routes for YTunes: a single unified list of track cards,
persisted across app restarts. There's no separate "library" — once a
track is confirmed & tagged, its file lives in the configured output
folder and the card is removed entirely rather than lingering as
"complete" (see pipeline.confirm_track).
"""

import os

from flask import Flask, render_template, request, jsonify, send_file, abort
from werkzeug.utils import secure_filename

import queue_manager as qm
import pipeline
from utils import BASE_DIR, load_config, log

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
)
cfg = load_config()

qm.cleanup_stuck_tracks()
if qm.list_tracks(status="resolving") or qm.list_tracks(status="queued"):
    pipeline.start_background_processing()


@app.route("/")
def index():
    tracks = qm.list_tracks()
    threshold = cfg.get("auto_match_confidence_threshold", 0.9)
    fingerprint_max_seconds = cfg.get("fingerprint_max_seconds", 60)
    return render_template(
        "index.html",
        tracks=tracks,
        auto_match_threshold=threshold,
        fingerprint_max_seconds=fingerprint_max_seconds,
    )


@app.route("/add", methods=["POST"])
def add():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        added = pipeline.add_url(url)
    except Exception as e:
        log(f"Failed to add URL {url}: {e}", "error")
        return jsonify({"error": str(e)}), 400
    return jsonify({"added": added})


@app.route("/queue")
def queue_status():
    return jsonify(qm.list_tracks())


@app.route("/track/<track_id>/more_matches", methods=["POST"])
def more_matches(track_id):
    """On-demand deeper search: more fingerprint guesses, more results per
    source, optionally combined with a user-typed query. Merges into and
    persists the track's candidate list."""
    data = request.json or {}
    custom_query = (data.get("query") or "").strip() or None
    try:
        candidates = pipeline.find_more_matches(track_id, custom_query)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        log(f"more_matches failed for track {track_id}: {e}", "error")
        return jsonify({"error": str(e)}), 400
    return jsonify({"candidates": candidates})


@app.route("/track/<track_id>/confirm", methods=["POST"])
def track_confirm(track_id):
    data = request.json or {}
    selected = data.get("selected")
    if not selected or not selected.get("title") or not selected.get("artist"):
        return jsonify({"error": "Title and artist are required"}), 400
    try:
        output_path = pipeline.confirm_track(track_id, selected)
    except Exception as e:
        log(f"Failed to confirm track {track_id}: {e}", "error")
        return jsonify({"error": str(e)}), 400
    # No "complete" status to report back — the card is already gone.
    return jsonify({"status": "removed", "output_path": output_path})


@app.route("/track/<track_id>/upload_art", methods=["POST"])
def upload_art(track_id):
    track = qm.get_track(track_id)
    if not track:
        return jsonify({"error": "Track not found"}), 404
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1] or ".jpg"
    dest_path = os.path.join(cfg["temp_folder"], f"{track_id}_custom_art{ext}")
    file.save(dest_path)

    qm.update_track(track_id, custom_art_path=dest_path)
    return jsonify({"status": "ok"})


@app.route("/track/<track_id>/sample")
def track_sample(track_id):
    """Serve the downloaded audio so the Original (YouTube) card can play a
    sample. Playback is capped client-side at fingerprint_max_seconds, so
    what the user hears is exactly the portion that was actually sent for
    fingerprinting — no separate trimmed file to manage server-side."""
    track = qm.get_track(track_id)
    if not track or not track.get("audio_path") or not os.path.exists(track["audio_path"]):
        abort(404)
    return send_file(track["audio_path"], mimetype="audio/mpeg")


@app.route("/track/<track_id>", methods=["DELETE"])
def remove_track(track_id):
    """Removing a card also deletes any file it produced. Not allowed while
    a track is actively being worked on (downloading/analyzing/tagging/
    resolving) — only pending work, a paused review, or a failed attempt
    can be removed."""
    track = qm.get_track(track_id)
    if not track:
        return jsonify({"error": "Track not found"}), 404
    if not qm.is_deletable(track):
        return jsonify({"error": f"Can't remove a track while it's {track['status']}"}), 409

    for path_key in ("output_path", "audio_path", "custom_art_path"):
        path = track.get(path_key)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                log(f"Failed to delete {path}: {e}", "error")
    removed = qm.remove_track(track_id)
    return jsonify({"removed": removed})


if __name__ == "__main__":
    port = cfg.get("web_port", 8090)
    log(f"Starting YTunes on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
