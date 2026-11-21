import io
import os
import shutil
import subprocess

import pytest
from mutagen.id3 import ID3
from PIL import Image

import statuses as st

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


@pytest.fixture
def client(store, no_worker):
    import app as app_module
    flask_app = app_module.create_app(start_worker=False)
    flask_app.testing = True
    return flask_app.test_client()


def test_index_renders_status_groups(client):
    html = client.get("/").get_data(as_text=True)
    assert '"not_deletable"' in html and "{{" not in html


def test_add_and_duplicate(client):
    assert client.post("/add", json={"url": URL}).status_code == 200
    assert client.post("/add", json={"url": "https://youtu.be/aaaaaaaaaaa"}).status_code == 409
    assert client.post("/add", json={"url": "  "}).status_code == 400


def test_delete_rules(client, store):
    t = store.add_track(URL, "Song")
    store.transition(t["id"], st.FETCHING_INFO)
    assert client.delete(f"/track/{t['id']}").status_code == 409
    assert client.delete("/track/nope").status_code == 404
    q = store.add_track("https://www.youtube.com/watch?v=bbbbbbbbbbb", "Other")
    assert client.delete(f"/track/{q['id']}").get_json() == {"removed": True}


def test_upload_art_validates_and_replaces(client, store, cfg):
    t = store.add_track(URL, "Song")
    bad = client.post(f"/track/{t['id']}/upload_art",
                      data={"file": (io.BytesIO(b"x"), "evil.exe")}, content_type="multipart/form-data")
    assert bad.status_code == 400

    for name in ("a.png", "b.jpg"):
        r = client.post(f"/track/{t['id']}/upload_art",
                        data={"file": (io.BytesIO(b"img"), name)}, content_type="multipart/form-data")
        assert r.status_code == 200
    files = sorted(os.listdir(cfg["temp_folder"]))
    assert files == [f"{t['id']}_custom_art.jpg"]   # the earlier .png was cleaned up


def test_confirm_requires_title_and_artist(client, store):
    t = store.add_track(URL, "Song")
    assert client.post(f"/track/{t['id']}/confirm", json={"selected": {"title": "x"}}).status_code == 400


def test_retry_only_from_error(client, store):
    t = store.add_track(URL, "Song")
    assert client.post(f"/track/{t['id']}/retry").status_code == 409


# ---------- Real tagging (needs ffmpeg to make a test MP3) ----------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_tag_and_move_writes_tags_and_default_art(cfg, tmp_path):
    import tag_mp3

    audio = os.path.join(cfg["temp_folder"], "0123456789ab.mp3")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "1", "-q:a", "9", audio], check=True)
    # 16:9 default art, which should be cropped square
    Image.new("RGB", (160, 90), "red").save(cfg["default_art"], format="JPEG")

    out = tag_mp3.tag_and_move({
        "id": "0123456789ab",
        "audio_path": audio,
        "custom_art_path": None,
        "video_title": "Video",
        "selected": {"title": "It's Only A Test", "artist": "AB/CD", "album": "LP", "year": "1979",
                     "genre": "Rock", "track_number": "3", "art_url": None},
    })

    assert out == os.path.join(cfg["output_folder"], "ABCD - It's Only A Test.mp3")
    assert not os.path.exists(audio)
    tags = ID3(out)
    assert str(tags["TIT2"]) == "It's Only A Test" and str(tags["TPE1"]) == "AB/CD"
    assert str(tags["TRCK"]) == "3"
    art = Image.open(io.BytesIO(tags.getall("APIC")[0].data))
    assert art.size[0] == art.size[1] == 90
