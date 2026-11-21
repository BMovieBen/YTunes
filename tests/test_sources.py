"""metadata_lookup and fetch_youtube, with HTTP and yt-dlp faked out."""

import pytest

import fetch_youtube
import metadata_lookup as ml


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ml.requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        for prefix, data in self.routes.items():
            if url.startswith(prefix):
                return FakeResponse(data)
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture
def fast_limits(monkeypatch):
    for limiter in ml._LIMITS.values():
        monkeypatch.setattr(limiter, "min_interval", 0)


def test_pick_track_from_tracklist():
    tracklist = [
        {"position": "1", "title": "Intro"},
        {"position": "2", "title": "Midnight Harbor"},
        {"position": "A3", "title": "Paper Lanterns"},
    ]
    assert ml.pick_track_from_tracklist(tracklist, "The Placeholders Midnight Harbor", "LP") == ("Midnight Harbor", 2)
    assert ml.pick_track_from_tracklist(tracklist, "Paper Lanterns", "LP") == ("Paper Lanterns", None)
    assert ml.pick_track_from_tracklist(tracklist, "zzzzzz qqqq", "LP") == ("LP", None)


def test_discogs_token_sent_as_header_not_url(cfg, fast_limits, monkeypatch):
    session = FakeSession({
        ml.DISCOGS_SEARCH_URL: {"results": [{"id": 7, "title": "The Placeholders - Example Album",
                                             "year": 1975, "genre": ["Rock"]}]},
        ml.DISCOGS_RELEASE_URL: {"tracklist": [{"position": "11", "title": "Midnight Harbor"}]},
    })
    monkeypatch.setattr(ml, "_session", lambda: session)
    [c] = ml.search_discogs("The Placeholders Midnight Harbor", limit=1)
    assert (c["title"], c["artist"], c["album"], c["track_number"]) == (
        "Midnight Harbor", "The Placeholders", "Example Album", 11)
    for call in session.calls:
        assert "token" not in call["params"]
        assert call["headers"]["Authorization"] == "Discogs token=test-token"


def test_busy_responses_are_retried(cfg, fast_limits, monkeypatch):
    responses = [FakeResponse({}, 503), FakeResponse({"recordings": [{"title": "T"}]})]
    calls = []

    class Session:
        def get(self, *a, **kw):
            calls.append(1)
            return responses.pop(0)

    monkeypatch.setattr(ml, "_session", lambda: Session())
    monkeypatch.setattr(ml.time, "sleep", lambda s: None)
    assert [c["title"] for c in ml.search_musicbrainz("q")] == ["T"]
    assert len(calls) == 2


def test_persistent_busy_gives_up(cfg, fast_limits, monkeypatch):
    class Session:
        def get(self, *a, **kw):
            return FakeResponse({}, 503)

    monkeypatch.setattr(ml, "_session", lambda: Session())
    monkeypatch.setattr(ml.time, "sleep", lambda s: None)
    assert ml.search_musicbrainz("q") == []


def test_search_many_keeps_source_order(cfg, fast_limits, monkeypatch):
    calls = []

    def fake(name):
        def search(q, limit):
            calls.append((name, q))
            return [{"source": name, "q": q}]
        return search

    monkeypatch.setattr(ml, "_SOURCE_FUNCS", {s: fake(s) for s in ("itunes", "musicbrainz", "discogs")})
    results = ml.search_many(["q1", "q2"], limit_per_source=1)
    assert [(r["source"], r["q"]) for r in results] == [
        ("itunes", "q1"), ("itunes", "q2"),
        ("musicbrainz", "q1"), ("musicbrainz", "q2"),
        ("discogs", "q1"), ("discogs", "q2"),
    ]


def test_search_many_survives_a_crashing_source(cfg, fast_limits, monkeypatch):
    def broken(q, limit):
        raise KeyError("bad json")

    monkeypatch.setattr(ml, "_SOURCE_FUNCS", {
        "itunes": broken, "musicbrainz": lambda q, l: [{"source": "musicbrainz"}], "discogs": broken})
    assert ml.search_many(["q"]) == [{"source": "musicbrainz"}]


def test_rate_limiter_spaces_calls(monkeypatch):
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(ml.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ml.time, "sleep", lambda s: sleeps.append(s))
    limiter = ml._RateLimiter(1.0)
    limiter.wait()
    limiter.wait()
    clock[0] += 5
    limiter.wait()
    assert sleeps == [pytest.approx(1.0)]


def test_parse_resolved_playlist():
    data = {"_type": "playlist", "title": "Mix", "entries": [
        {"url": "abcDEF12345", "title": "Bare ID"},
        {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "Full"},
        None,
        {"title": "No URL"},
    ]}
    assert fetch_youtube.parse_resolved(data, "https://pl") == [
        {"url": "https://www.youtube.com/watch?v=abcDEF12345", "title": "Bare ID", "playlist_title": "Mix"},
        {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "Full", "playlist_title": "Mix"},
    ]


def test_parse_resolved_single_video():
    data = {"webpage_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "One"}
    assert fetch_youtube.parse_resolved(data, "https://youtu.be/aaaaaaaaaaa") == [
        {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "One", "playlist_title": None}]


def test_ytdlp_command_includes_cookies(cfg, monkeypatch):
    monkeypatch.setitem(cfg, "cookies_from_browser", "testbrowser")
    cmd = fetch_youtube._ytdlp("--flag", "URL")
    assert "yt-dlp" in cmd[0]
    assert cmd[cmd.index("--cookies-from-browser") + 1] == "testbrowser"
    assert cmd[-2:] == ["--flag", "URL"]
