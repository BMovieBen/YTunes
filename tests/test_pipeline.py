import os

import pytest

import statuses as st

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


@pytest.fixture
def pipeline(store, no_worker):
    import pipeline as p
    return p


def test_confidence_uses_fingerprint_score_on_fuzzy_match(pipeline):
    fp = [{"title": "Midnight Harbor", "artist": "The Placeholders", "score": 0.93}]
    match = {"title": "Midnight Harbor (Remastered 2011)", "artist": "The Placeholders"}
    other = {"title": "Completely Different", "artist": "Someone Else"}
    assert pipeline._confidence_for(match, fp) == 0.93
    assert pipeline._confidence_for(other, fp) == 0.4
    assert pipeline._confidence_for(match, []) == 0.4


def test_add_url_rejects_duplicate_single_video(pipeline, store):
    pipeline.add_url(URL)
    store.update_track(store.list_tracks()[0]["id"], source_url=URL)
    with pytest.raises(pipeline.DuplicateTrack):
        pipeline.add_url("https://youtu.be/aaaaaaaaaaa")
    # A playlist URL that happens to name the same video is still allowed.
    pipeline.add_url(URL + "&list=PL123")


def test_resolve_playlist_skips_videos_already_listed(pipeline, store, monkeypatch):
    store.add_track(URL, "already here")
    placeholder = store.add_track("https://pl", "https://pl", resolved=False)
    monkeypatch.setattr(pipeline.fetch_youtube, "resolve_urls", lambda url: [
        {"url": URL, "title": "A", "playlist_title": "PL"},
        {"url": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "title": "B", "playlist_title": "PL"},
        {"url": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "title": "B again", "playlist_title": "PL"},
    ])
    pipeline._resolve_track(placeholder)
    titles = [t["video_title"] for t in store.list_tracks()]
    assert titles == ["already here", "B"]


def test_resolve_single_video_updates_in_place(pipeline, store, monkeypatch):
    placeholder = store.add_track("https://youtu.be/ccccccccccc", "https://youtu.be/ccccccccccc", resolved=False)
    monkeypatch.setattr(pipeline.fetch_youtube, "resolve_urls", lambda url: [
        {"url": "https://www.youtube.com/watch?v=ccccccccccc", "title": "C", "playlist_title": None},
    ])
    pipeline._resolve_track(placeholder)
    t = store.get_track(placeholder["id"])
    assert t["status"] == st.QUEUED and t["video_title"] == "C" and t["resolved"]


def _fake_externals(monkeypatch, pipeline, cfg):
    monkeypatch.setattr(pipeline.fetch_youtube, "get_video_metadata", lambda url: {
        "title": "Song", "uploader": "Some Channel", "thumbnail": "http://thumb", "year": "2020"})

    def fake_download(url, temp_folder, track_id):
        path = os.path.join(temp_folder, f"{track_id}.mp3")
        open(path, "wb").close()
        return path

    monkeypatch.setattr(pipeline.fetch_youtube, "download_audio", fake_download)
    monkeypatch.setattr(pipeline.fingerprint, "identify", lambda path, maxlength: [
        {"title": "Song", "artist": "Artist", "score": 0.95, "mb_recording_id": None}])
    monkeypatch.setattr(pipeline.metadata_lookup, "search_all", lambda q, limit_per_source: [
        {"source": "itunes", "title": "Song", "artist": "Artist", "album": "LP"}])


def test_worker_takes_track_to_review(pipeline, store, cfg, monkeypatch):
    _fake_externals(monkeypatch, pipeline, cfg)
    t = store.add_track(URL, "Song")
    pipeline._worker_running = True
    pipeline._worker_loop()   # runs until nothing is pending
    done = store.get_track(t["id"])
    assert done["status"] == st.NEEDS_REVIEW
    assert done["candidates"][0]["confidence"] == 0.95
    assert done["youtube_reference"]["artist"] == "Some Channel"
    assert pipeline._worker_running is False


def test_worker_marks_failures_as_error(pipeline, store, cfg, monkeypatch):
    _fake_externals(monkeypatch, pipeline, cfg)

    def boom(*a):
        raise RuntimeError("Download failed: 403")

    monkeypatch.setattr(pipeline.fetch_youtube, "download_audio", boom)
    t = store.add_track(URL, "Song")
    pipeline._worker_running = True
    pipeline._worker_loop()
    failed = store.get_track(t["id"])
    assert failed["status"] == st.ERROR and "403" in failed["error_message"]

    pipeline.retry_track(t["id"])
    assert store.get_track(t["id"])["status"] == st.QUEUED


def _review_track(store, cfg):
    t = store.add_track(URL, "Song")
    audio = os.path.join(cfg["temp_folder"], f"{t['id']}.mp3")
    art = os.path.join(cfg["temp_folder"], f"{t['id']}_custom_art.png")
    for p in (audio, art):
        open(p, "wb").close()
    store.transition(t["id"], st.FETCHING_INFO)
    for s in (st.DOWNLOADING, st.ANALYZING, st.NEEDS_REVIEW):
        store.transition(t["id"], s)
    store.update_track(t["id"], audio_path=audio, custom_art_path=art)
    return t, audio, art


def test_confirm_success_removes_card_and_temp_files(pipeline, store, cfg, monkeypatch):
    t, audio, art = _review_track(store, cfg)
    seen = {}

    def fake_tag(track):
        seen.update(track)
        return "out.mp3"

    monkeypatch.setattr(pipeline.tag_mp3, "tag_and_move", fake_tag)
    assert pipeline.confirm_track(t["id"], {"title": "T", "artist": "A"}, use_custom_art=False) == "out.mp3"
    assert seen["custom_art_path"] is None          # applied candidate art wins over the upload
    assert store.get_track(t["id"]) is None
    assert not os.path.exists(audio) and not os.path.exists(art)


def test_confirm_failure_returns_to_review(pipeline, store, cfg, monkeypatch):
    t, audio, _ = _review_track(store, cfg)

    def broken(track):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline.tag_mp3, "tag_and_move", broken)
    with pytest.raises(OSError):
        pipeline.confirm_track(t["id"], {"title": "T", "artist": "A"})
    after = store.get_track(t["id"])
    assert after["status"] == st.NEEDS_REVIEW
    assert "disk full" in after["error_message"]
    assert os.path.exists(audio)
    # ...and the card can be removed or confirmed again, not stuck.
    pipeline.remove_track(t["id"])


def test_double_confirm_is_rejected(pipeline, store, cfg, monkeypatch):
    t, _, _ = _review_track(store, cfg)
    store.transition(t["id"], st.TAGGING)
    with pytest.raises(store.InvalidTransition):
        pipeline.confirm_track(t["id"], {"title": "T", "artist": "A"})


def test_cleanup_orphan_temp_files(pipeline, store, cfg):
    t = store.add_track(URL, "Song")
    keep = os.path.join(cfg["temp_folder"], f"{t['id']}.mp3")
    orphan = os.path.join(cfg["temp_folder"], "0123456789ab_art.jpg")
    unrelated = os.path.join(cfg["temp_folder"], "notes.txt")
    for p in (keep, orphan, unrelated):
        open(p, "wb").close()
    pipeline.cleanup_orphan_temp_files()
    assert os.path.exists(keep) and os.path.exists(unrelated) and not os.path.exists(orphan)


def test_find_more_matches_merges_and_dedupes(pipeline, store, monkeypatch):
    t = store.add_track(URL, "Video Title")
    store.update_track(t["id"], candidates=[
        {"source": "itunes", "title": "Song", "artist": "Artist", "album": "LP", "confidence": 0.4}])
    queries = []

    def fake_search_many(qs, limit_per_source):
        queries.extend(qs)
        return [
            {"source": "itunes", "title": "Song", "artist": "Artist", "album": "LP"},      # dupe
            {"source": "discogs", "title": "Song", "artist": "Artist", "album": "Single"},
        ]

    monkeypatch.setattr(pipeline.metadata_lookup, "search_many", fake_search_many)
    result = pipeline.find_more_matches(t["id"], "my query")
    assert queries == ["my query", "Video Title"]
    assert len(result) == 2
    assert len(store.get_track(t["id"])["candidates"]) == 2
