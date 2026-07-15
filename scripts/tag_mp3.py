"""
tag_mp3.py - Embed ID3 metadata + album art into the downloaded MP3 and move
it into the flat output folder. Mirrors Article2Pod's tag-mp3.py.
"""

import io
import os
import shutil

from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, TRCK, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from PIL import Image

from utils import load_config, log, safe_filename
from metadata_lookup import download_art


def _crop_to_square(image_bytes):
    """
    Center-crop art to a square, re-encoded as JPEG. Album art is
    conventionally square, but source images rarely are on their own —
    YouTube thumbnails in particular are 16:9. Apple Music (and most
    players) won't crop cover art for you; it'll just display it
    letterboxed/stretched as-is, so we do the cropping ourselves before
    it ever gets embedded.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")  # normalize mode — JPEG needs RGB, sources vary (RGBA/P/etc.)
        width, height = img.size
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        img = img.crop((left, top, left + side, top + side))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        log(f"Failed to center-crop album art, embedding original instead: {e}", "error")
        return image_bytes


def tag_and_move(track):
    """
    track is a queue dict with 'audio_path', 'selected' (final metadata),
    and optionally 'custom_art_path'. Returns the final output path.
    """
    cfg = load_config()
    audio_path = track["audio_path"]
    meta = track["selected"] or {}

    if not audio_path or not os.path.exists(audio_path):
        raise RuntimeError("No downloaded audio file to tag")

    # Resolve art: user upload > candidate art_url > default fallback image
    art_bytes = None
    if track.get("custom_art_path") and os.path.exists(track["custom_art_path"]):
        with open(track["custom_art_path"], "rb") as f:
            art_bytes = f.read()
    elif meta.get("art_url"):
        temp_art_path = os.path.join(cfg["temp_folder"], f"{track['id']}_art.jpg")
        if download_art(meta["art_url"], temp_art_path):
            with open(temp_art_path, "rb") as f:
                art_bytes = f.read()
    if art_bytes is None:
        default_art = os.path.join(os.path.dirname(cfg["output_folder"]), cfg.get("default_art", "default_art.jpg"))
        if os.path.exists(default_art):
            with open(default_art, "rb") as f:
                art_bytes = f.read()

    if art_bytes:
        art_bytes = _crop_to_square(art_bytes)

    # Write ID3 tags
    try:
        tags = ID3(audio_path)
    except ID3NoHeaderError:
        tags = ID3()

    title = meta.get("title") or track.get("video_title") or "Unknown Title"
    artist = meta.get("artist") or "Unknown Artist"
    album = meta.get("album") or ""
    year = meta.get("year") or ""
    genre = meta.get("genre") or ""
    track_number = meta.get("track_number")

    tags.delall("TIT2")
    tags.add(TIT2(encoding=3, text=title))
    tags.delall("TPE1")
    tags.add(TPE1(encoding=3, text=artist))
    if album:
        tags.delall("TALB")
        tags.add(TALB(encoding=3, text=album))
    if year:
        tags.delall("TDRC")
        tags.add(TDRC(encoding=3, text=str(year)))
    if genre:
        tags.delall("TCON")
        tags.add(TCON(encoding=3, text=genre))
    if track_number:
        tags.delall("TRCK")
        tags.add(TRCK(encoding=3, text=str(track_number)))
    if art_bytes:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=art_bytes))

    tags.save(audio_path, v2_version=3)

    # Move to flat output folder as "Artist - Title.mp3"
    filename = safe_filename(f"{artist} - {title}") + ".mp3"
    output_path = os.path.join(cfg["output_folder"], filename)

    # Avoid collisions
    counter = 2
    base_output_path = output_path
    while os.path.exists(output_path):
        name, ext = os.path.splitext(base_output_path)
        output_path = f"{name} ({counter}){ext}"
        counter += 1

    shutil.move(audio_path, output_path)
    log(f"Tagged and moved track {track['id']} -> {output_path}")
    return output_path
