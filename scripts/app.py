"""
app.py - Flask routes for YTunes. Mirrors Article2Pod's app.py structure:
a queue-based web UI for adding URLs, reviewing metadata matches, and
browsing the finished library.
"""

import os

from flask import Flask, render_template, request, jsonify, send_file, abort
from werkzeug.utils import secure_filename

import queue_manager as qm
import pipeline
from utils import BASE_DIR, load_config, log, safe_filename

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
)
cfg = load_config()

qm.cleanup_stuck_tracks()


@app.route("/")
def index():
    tracks = qm.list_tracks()
    library = _list_library()
    return render_template("index.html", tracks=tracks, library=library)


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


@app.route("/process", methods=["POST"])
def process():
    pipeline.start_background_processing()
    return jsonify({"status": "started"})


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
    return jsonify({"status": "ok", "custom_art_path": dest_path})


@app.route("/track/<track_id>", methods=["DELETE"])
def remove_track(track_id):
    removed = qm.remove_track(track_id)
    return jsonify({"removed": removed})


@app.route("/library")
def library():
    return jsonify(_list_library())


@app.route("/library/<path:filename>/download")
def library_download(filename):
    path = os.path.join(cfg["output_folder"], filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True)


@app.route("/library/<path:filename>", methods=["DELETE"])
def library_delete(filename):
    path = os.path.join(cfg["output_folder"], filename)
    if not os.path.exists(path):
        abort(404)
    os.remove(path)
    return jsonify({"removed": True})


def _list_library():
    files = []
    for name in sorted(os.listdir(cfg["output_folder"])):
        if name.lower().endswith(".mp3"):
            files.append(name)
    return files


if __name__ == "__main__":
    port = cfg.get("web_port", 8090)
    log(f"Starting YTunes on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
