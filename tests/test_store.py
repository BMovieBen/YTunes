import json
import threading

import pytest

import statuses as st

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


def test_add_get_update_remove(store):
    t = store.add_track(URL, "Song", playlist_title="PL", resolved=True)
    assert t["status"] == st.QUEUED
    assert t["video_id"] == "aaaaaaaaaaa"
    assert t["candidates"] == [] and t["resolved"] is True

    updated = store.update_track(t["id"], candidates=[{"title": "x"}], youtube_reference={"a": 1})
    assert updated["candidates"] == [{"title": "x"}]
    assert updated["version"] == t["version"] + 1

    assert store.remove_track(t["id"]) is True
    assert store.get_track(t["id"]) is None
    assert store.update_track(t["id"], error_message="x") is None


def test_update_rejects_unknown_fields(store):
    t = store.add_track(URL, "Song")
    with pytest.raises(ValueError):
        store.update_track(t["id"], bogus=1)


def test_unresolved_track_starts_resolving(store):
    assert store.add_track("https://x", "https://x", resolved=False)["status"] == st.RESOLVING


def test_transition_enforces_state_machine(store):
    t = store.add_track(URL, "Song")
    with pytest.raises(store.InvalidTransition):
        store.transition(t["id"], st.TAGGING)
    moved = store.transition(t["id"], st.FETCHING_INFO)
    assert moved["status"] == st.FETCHING_INFO
    with pytest.raises(KeyError):
        store.transition("missing", st.QUEUED)


def test_claim_next_pending_prefers_resolving_and_claims_queued(store):
    q1 = store.add_track(URL, "first")
    store.add_track("https://www.youtube.com/watch?v=bbbbbbbbbbb", "second")
    r = store.add_track("https://x", "https://x", resolved=False)

    claimed = store.claim_next_pending()
    assert claimed["id"] == r["id"] and claimed["status"] == st.RESOLVING
    store.transition(r["id"], st.ERROR, error_message="nope")

    claimed = store.claim_next_pending()
    assert claimed["id"] == q1["id"]
    assert claimed["status"] == st.FETCHING_INFO   # claimed atomically
    # A claimed track can't be deleted out from under the worker.
    with pytest.raises(store.InvalidTransition):
        store.remove_if_deletable(q1["id"])


def test_has_pending(store):
    assert not store.has_pending()
    store.add_track(URL, "Song")
    assert store.has_pending()


def test_remove_if_deletable(store):
    t = store.add_track(URL, "Song")
    assert store.remove_if_deletable(t["id"])["id"] == t["id"]
    with pytest.raises(KeyError):
        store.remove_if_deletable(t["id"])


def test_replace_with_entries(store):
    placeholder = store.add_track("https://pl", "https://pl", resolved=False)
    new = store.replace_with_entries(placeholder["id"], [
        {"url": "https://www.youtube.com/watch?v=ccccccccccc", "title": "C", "playlist_title": "PL"},
        {"url": "https://www.youtube.com/watch?v=ddddddddddd", "title": "D", "playlist_title": "PL"},
    ])
    assert [t["video_title"] for t in new] == ["C", "D"]
    assert store.get_track(placeholder["id"]) is None
    assert store.existing_video_ids(["ccccccccccc", "zzzzzzzzzzz"]) == {"ccccccccccc"}


def test_reset_interrupted(store):
    a = store.add_track(URL, "a")
    b = store.add_track("https://www.youtube.com/watch?v=bbbbbbbbbbb", "b")
    store.transition(a["id"], st.FETCHING_INFO)
    store.transition(b["id"], st.FETCHING_INFO)
    for s in (st.DOWNLOADING, st.ANALYZING, st.NEEDS_REVIEW, st.TAGGING):
        store.transition(b["id"], s)
    assert store.reset_interrupted() == 2
    assert store.get_track(a["id"])["status"] == st.QUEUED
    assert store.get_track(b["id"])["status"] == st.NEEDS_REVIEW


def test_concurrent_writes_lose_nothing(store):
    """The old JSON queue could drop tracks when the worker and a request
    wrote at the same time. Hammer the store from several threads."""
    seed = store.add_track(URL, "seed")
    errors = []

    def adder(n):
        try:
            for i in range(25):
                store.add_track(f"https://www.youtube.com/watch?v=t{n:02d}{i:08d}", f"{n}-{i}")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def updater():
        try:
            for i in range(100):
                store.update_track(seed["id"], error_message=str(i))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=adder, args=(n,)) for n in range(6)]
    threads.append(threading.Thread(target=updater))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(store.list_tracks()) == 1 + 6 * 25
    assert store.get_track(seed["id"])["version"] == 101


def test_legacy_queue_import(cfg, tmp_path):
    import store
    legacy = tmp_path / "queue.json"
    legacy.write_text(json.dumps({"tracks": [
        {"id": "aaaaaaaaaaaa", "source_url": URL, "video_title": "Old", "resolved": True,
         "status": "needs_review", "candidates": [{"title": "c"}], "added_at": "2025-01-01T00:00:00"},
        {"id": "bbbbbbbbbbbb", "source_url": URL, "status": "complete"},  # long-gone status: skipped
    ]}), encoding="utf-8")

    store.init(cfg["db_path"], legacy_queue_path=str(legacy))
    try:
        tracks = store.list_tracks()
        assert [t["id"] for t in tracks] == ["aaaaaaaaaaaa"]
        assert tracks[0]["candidates"] == [{"title": "c"}]
        assert not legacy.exists() and (tmp_path / "queue.json.imported").exists()
    finally:
        store.close()
