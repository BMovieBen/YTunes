"""
app.py - Flask routes for YTunes: a single unified list of track cards,
session-only (nothing persists across an app restart). Mirrors Article2Pod's
overall structure but without a separate "library" concept — finished
tracks just stay in the same list as "complete" cards until removed or the
app restarts.
"""

import os

from flask import Flask, render_template, request, jsonify, send_file, redirect, abort
from werkzeug.utils import secure_filename

import queue_manager as qm
import pipeline
from utils import BASE_DIR, load_config, log

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
)
cfg = load_config()

qm.reset_queue()


@app.route("/")
def index():
    tracks = qm.list_tracks()
    threshold = cfg.get("auto_match_confidence_threshold", 0.9)
    return render_template("index.html", tracks=tracks, auto_match_threshold=threshold)


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
    return jsonify({"status": "complete", "output_path": output_path})


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


@app.route("/track/<track_id>/art")
def track_art(track_id):
    """Serve album art for a card: uploaded custom art takes priority; else
    redirect to whatever external art URL was selected/matched."""
    track = qm.get_track(track_id)
    if not track:
        abort(404)
    if track.get("custom_art_path") and os.path.exists(track["custom_art_path"]):
        return send_file(track["custom_art_path"])
    selected = track.get("selected") or {}
    if selected.get("art_url"):
        return redirect(selected["art_url"])
    ref = track.get("youtube_reference") or {}
    if ref.get("art_url"):
        return redirect(ref["art_url"])
    abort(404)


@app.route("/track/<track_id>/download")
def track_download(track_id):
    track = qm.get_track(track_id)
    if not track or not track.get("output_path") or not os.path.exists(track["output_path"]):
        abort(404)
    return send_file(track["output_path"], as_attachment=True)


@app.route("/track/<track_id>", methods=["DELETE"])
def remove_track(track_id):
    """Removing a card also deletes any file it produced — there's no
    separate library to keep it around in, so nothing should be left behind."""
    track = qm.get_track(track_id)
    if track:
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
