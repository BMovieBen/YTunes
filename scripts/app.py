"""
app.py - Flask routes for YTunes: one list of track cards, persisted across
restarts. Once a track is confirmed and tagged, its file lives in the
output folder and the card is removed (see pipeline.confirm_track).

Run:  python scripts/app.py [--no-browser]
"""

import argparse
import os
import threading
import webbrowser

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import pipeline
import statuses as st
import store
from utils import BASE_DIR, load_config, log

ALLOWED_ART_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def create_app(start_worker=True):
    cfg = load_config()
    store.init()

    app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            auto_match_threshold=cfg["auto_match_confidence_threshold"],
            fingerprint_max_seconds=cfg["fingerprint_max_seconds"],
            status_groups=st.for_client(),
        )

    @app.route("/queue")
    def queue_status():
        return jsonify(store.list_tracks())

    @app.route("/add", methods=["POST"])
    def add():
        url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
        if not url:
            return jsonify({"error": "No URL provided"}), 400
        try:
            added = pipeline.add_url(url)
        except pipeline.DuplicateTrack as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"added": added})

    @app.route("/track/<track_id>/more_matches", methods=["POST"])
    def more_matches(track_id):
        """Deeper search: more fingerprint guesses, more results per source,
        optionally with a user-typed query. Merged into the saved list."""
        custom_query = ((request.get_json(silent=True) or {}).get("query") or "").strip() or None
        try:
            candidates = pipeline.find_more_matches(track_id, custom_query)
        except KeyError:
            return jsonify({"error": "Track not found"}), 404
        return jsonify({"candidates": candidates})

    @app.route("/track/<track_id>/confirm", methods=["POST"])
    def track_confirm(track_id):
        data = request.get_json(silent=True) or {}
        selected = data.get("selected") or {}
        if not selected.get("title") or not selected.get("artist"):
            return jsonify({"error": "Title and artist are required"}), 400
        try:
            output_path = pipeline.confirm_track(
                track_id, selected, use_custom_art=bool(data.get("use_custom_art", True))
            )
        except KeyError:
            return jsonify({"error": "Track not found"}), 404
        except store.InvalidTransition as e:
            return jsonify({"error": str(e)}), 409
        except Exception as e:
            return jsonify({"error": f"Tagging failed: {e}"}), 500
        return jsonify({"status": "removed", "output_path": output_path})

    @app.route("/track/<track_id>/upload_art", methods=["POST"])
    def upload_art(track_id):
        track = store.get_track(track_id)
        if not track:
            return jsonify({"error": "Track not found"}), 404
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"error": "No file uploaded"}), 400
        ext = os.path.splitext(secure_filename(file.filename))[1].lower()
        if ext not in ALLOWED_ART_EXTENSIONS:
            return jsonify({"error": f"Unsupported image type '{ext or '?'}'"}), 400

        dest_path = os.path.join(cfg["temp_folder"], f"{track_id}_custom_art{ext}")
        old = track.get("custom_art_path")
        file.save(dest_path)
        if old and old != dest_path and os.path.exists(old):
            os.remove(old)
        store.update_track(track_id, custom_art_path=dest_path)
        return jsonify({"status": "ok"})

    @app.route("/track/<track_id>/sample")
    def track_sample(track_id):
        """Serve the downloaded audio for the Original card's play button.
        Playback is capped client-side at fingerprint_max_seconds, the same
        portion that was fingerprinted."""
        track = store.get_track(track_id)
        if not track or not track.get("audio_path") or not os.path.exists(track["audio_path"]):
            abort(404)
        return send_file(track["audio_path"], mimetype="audio/mpeg", conditional=True)

    @app.route("/track/<track_id>/retry", methods=["POST"])
    def retry_track(track_id):
        try:
            pipeline.retry_track(track_id)
        except KeyError:
            return jsonify({"error": "Track not found"}), 404
        except store.InvalidTransition as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"status": "queued"})

    @app.route("/track/<track_id>", methods=["DELETE"])
    def remove_track(track_id):
        """Remove a card and its temp files. Not allowed while a worker is
        busy with it."""
        try:
            pipeline.remove_track(track_id)
        except KeyError:
            return jsonify({"error": "Track not found"}), 404
        except store.InvalidTransition as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"removed": True})

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"error": "File is too large"}), 413

    if start_worker:
        store.reset_interrupted()
        pipeline.cleanup_orphan_temp_files()
        if store.has_pending():
            pipeline.start_background_processing()

    return app


def main():
    parser = argparse.ArgumentParser(description="YTunes web app")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab on start")
    parser.add_argument("--config", help="use this config file instead of config.json")
    args = parser.parse_args()
    if args.config:
        os.environ["YTUNES_CONFIG"] = os.path.abspath(args.config)

    cfg = load_config()
    app = create_app()
    host, port = cfg["bind_host"], cfg["web_port"]
    url = f"http://localhost:{port}"
    log(f"Starting YTunes on {url} (listening on {host})")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
