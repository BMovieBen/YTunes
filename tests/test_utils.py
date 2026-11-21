import pytest

from utils import normalize_text, safe_filename, youtube_video_id


@pytest.mark.parametrize("name, expected", [
    ("AB/CD - Some Song", "ABCD - Some Song"),
    ('What? "Why": <Who>|*', "What Why Who"),
    ("  trailing dots... ", "trailing dots"),
    ("", "Unknown"),
    (None, "Unknown"),
    ("???", "Unknown"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("tab\there", "tabhere"),
])
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


def test_safe_filename_truncates():
    assert len(safe_filename("a" * 400)) == 150


def test_normalize_text_strips_annotations_and_punctuation():
    assert normalize_text("It's Only A Test (Remastered 2011) [Official Video]") == "it s only a test"
    assert normalize_text(None) == ""


@pytest.mark.parametrize("url, vid", [
    ("https://www.youtube.com/watch?v=abcDEF12345", "abcDEF12345"),
    ("https://www.youtube.com/watch?feature=share&v=abcDEF12345&list=PL1", "abcDEF12345"),
    ("https://youtu.be/abcDEF12345?si=abc", "abcDEF12345"),
    ("https://www.youtube.com/shorts/abcDEF12345", "abcDEF12345"),
    ("https://music.youtube.com/watch?v=abcDEF12345", "abcDEF12345"),
    ("https://www.youtube.com/playlist?list=PLabc", None),
    ("not a url", None),
])
def test_youtube_video_id(url, vid):
    assert youtube_video_id(url) == vid
