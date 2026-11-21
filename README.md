# YTunes

YTunes downloads audio from YouTube (single videos or entire playlists), identifies
the music using open-source audio fingerprinting, looks up metadata across multiple
sources, and tags the resulting MP3s so they're ready to import into Apple Music.

Built as a personal sister project to [Article2Pod](https://github.com/BMovieBen/Article2Pod),
reusing its queue-based web UI and config-driven pipeline pattern. This is a personal-use
tool, not intended for redistribution.

---

## How it works

1. Paste a YouTube video or playlist URL into the web UI. Playlists are expanded into
   individual queue entries automatically.
2. Processing starts automatically. For each track, YTunes:
   - Downloads the audio via `yt-dlp`
   - Fingerprints the first minute with **Chromaprint** and identifies it against
     the free **AcoustID** database
   - Searches **iTunes, MusicBrainz and Discogs** (in parallel, listed in your configured
     order) for metadata matches based on the fingerprint result (falling back to the raw YouTube title if
     fingerprinting comes up empty)
3. Every track goes to **Needs Review** — you confirm the correct match, or type in
   exact metadata yourself if nothing matches.
4. If no album art was found (or you just prefer your own), you can upload a custom
   image per track before confirming.
5. Confirmed tracks are tagged (ID3: title, artist, album, year, genre, track number,
   album art) and dropped into a single flat `output/` folder — Apple Music organizes
   from tags on import, so no folder hierarchy is needed.

---

## Prerequisites

- **Windows 11**
- **Python 3.10+**
- **[ffmpeg](https://ffmpeg.org/download.html)** — on PATH
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — `pip install yt-dlp` or standalone on PATH
- **[Chromaprint](https://acoustid.org/chromaprint)** (`fpcalc`) — on PATH, or point to it
  via `config.json`
- **A free [AcoustID API key](https://acoustid.org/api-key)** — required for fingerprint
  identification
- **(Optional) A free [Discogs personal access token](https://www.discogs.com/settings/developers)**
  — only needed if you want Discogs as a metadata source

---

## Installation

### 1. Clone the repository

```
git clone https://github.com/<your-username>/YTunes.git C:\YTunes
```

### 2. Install Python dependencies

```
pip install -r requirements.txt
```

### 3. Configure

Copy `config.sample.json` to `config.json` and fill in your values:

Missing keys fall back to the defaults shown.

| Key                       | Description                                                                 |
| ------------------------- | --------------------------------------------------------------------------- |
| `web_port`                | Port the web interface runs on. Default `8090`.                             |
| `bind_host`               | Address to listen on. Default `127.0.0.1` (this PC only). Use `0.0.0.0` to allow other devices on your network (there's no login). |
| `log_level`               | Logging verbosity: `verbose`, `on_error`, or `off`. The log rotates at 2 MB. |
| `output_folder`           | Where finished, tagged MP3s land (flat folder).                             |
| `temp_folder`             | Working folder for downloads, cleared as tracks are confirmed or removed.   |
| `db_path`                 | The track list database. Default `log/ytunes.db`.                           |
| `default_art`             | Fallback album art if no match is found and nothing is uploaded. Relative paths are inside the YTunes folder. |
| `ytdlp_path` / `ffmpeg_path` / `fpcalc_path` | Paths to each binary, if not on PATH.                    |
| `cookies_from_browser`    | Browser to borrow YouTube cookies from (e.g. `chrome` or `edge`), if YouTube blocks anonymous downloads. |
| `fingerprint_max_seconds` | How much of each track is decoded for fingerprinting. Default `60`.         |
| `acoustid_api_key`        | Your free AcoustID API key.                                                 |
| `discogs_token`           | Your Discogs personal access token (optional).                              |
| `metadata_source_order`   | Order results are listed in: `["itunes", "musicbrainz", "discogs"]`. Leave a source out to skip it. |
| `auto_match_confidence_threshold` | Matches scoring at least this (0–1) pre-fill the review form. Default `0.9`. |
| `user_agent`              | User agent string sent to metadata APIs (MusicBrainz requires a real one).  |

### 4. Add a default art image (optional)

Place a file named `default_art.jpg` in the `YTunes` root folder to use as a fallback
when no album art is found and none is uploaded.

---

## Usage

Double-click `ytunes.bat`. On first run it creates the virtual environment (and it
reinstalls dependencies whenever `requirements.txt` changes). It then starts the app and
opens `http://localhost:8090` (or your configured port). `ytunes-no-browser.bat` does the
same without opening a browser tab.

1. Paste a YouTube URL (video or playlist) and click **+**. A playlist becomes one card
   per video; videos already in the list are skipped.
2. Each track is downloaded, fingerprinted and looked up automatically.
3. Once a track reaches **Needs Review**, apply the best match, click **Find more** for
   more candidates, or type the metadata yourself.
4. Optionally upload custom album art before confirming. Applying a candidate afterwards
   switches back to that candidate's art.
5. **Confirm & Tag** writes the tags, moves the MP3 to your output folder and removes the
   card. If tagging fails, the card stays in review with the error shown.

---

## Folder Structure

```
C:\YTunes
  ytunes.bat              ← launcher (ytunes-no-browser.bat: same, no browser tab)
  config.json              ← your configuration (not in repo)
  config.sample.json       ← configuration template
  default_art.jpg          ← fallback album art (optional, not in repo)
  requirements.txt
  requirements-dev.txt     ← test dependencies
  scripts/
    app.py                 ← Flask routes and startup
    pipeline.py            ← background worker: download/fingerprint/lookup, confirm
    store.py               ← SQLite track list
    statuses.py            ← track statuses and allowed status changes
    fetch_youtube.py        ← YouTube video + playlist downloader
    fingerprint.py           ← Chromaprint/AcoustID clip identification
    metadata_lookup.py       ← iTunes/MusicBrainz/Discogs search
    tag_mp3.py               ← ID3 tagger and file mover
    utils.py                 ← shared utilities
  templates/
    index.html              ← web interface
  tests/                    ← pytest suite
  output/                   ← finished, tagged MP3s (flat folder)
  temp/                     ← working files, cleared as tracks complete
  log/
    ytunes.log
    ytunes.db
```

Older versions kept the list in `log/queue.json`. On first start it's imported into the
database and renamed to `queue.json.imported`.

---

## Running the tests

```
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest
```

The tests use a temporary config, database and folders. They never touch your real
output folder or the network.

---

## Troubleshooting

**Fingerprinting fails / "Chromaprint/fpcalc not found"**
Install Chromaprint and confirm `fpcalc` is on PATH, or set `fpcalc_path` in
`config.json`.

**No candidates show up for a track**
Check that `acoustid_api_key` is set correctly. Even without a fingerprint match,
the raw YouTube title is used as a fallback search query — if that still returns
nothing, use the manual entry fields.

**MusicBrainz requests are failing or rate-limited**
Set a real, identifying `user_agent` in `config.json` — MusicBrainz requires this.

**YouTube playlist doesn't expand into individual tracks**
Confirm `yt-dlp` is up to date: `venv\Scripts\python.exe -m pip install -U yt-dlp`.

**Can't open YTunes from another device**
By default it only listens on this PC. Set `"bind_host": "0.0.0.0"` in `config.json`.

---

## Acknowledgements

- [Article2Pod](https://github.com/BMovieBen/Article2Pod) — architectural sibling
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [Chromaprint](https://acoustid.org/chromaprint) / [AcoustID](https://acoustid.org)
- [pyacoustid](https://github.com/beetbox/pyacoustid)
- [MusicBrainz](https://musicbrainz.org) / [Cover Art Archive](https://coverartarchive.org)
- [Discogs API](https://www.discogs.com/developers)
- [iTunes Search API](https://performance-partners.apple.com/search-api)
- [mutagen](https://github.com/quodlibet/mutagen)
- [Flask](https://flask.palletsprojects.com/)

Built for personal use, vibe coded with the help of Claude.
