"""Shared fixtures: every test gets its own config, database and folders, so
nothing touches the real config.json, output folder, or the network."""

import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import utils  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    config = {
        "log_level": "off",
        "output_folder": str(tmp_path / "output"),
        "temp_folder": str(tmp_path / "temp"),
        "db_path": str(tmp_path / "ytunes.db"),
        "default_art": str(tmp_path / "default_art.jpg"),
        "acoustid_api_key": "",
        "discogs_token": "test-token",
        "metadata_source_order": ["itunes", "musicbrainz", "discogs"],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("YTUNES_CONFIG", str(path))
    utils.reset_config_cache()
    loaded = utils.load_config()
    yield loaded
    utils.reset_config_cache()


@pytest.fixture
def store(cfg):
    import store as store_module
    store_module.init(cfg["db_path"], legacy_queue_path=None)
    yield store_module
    store_module.close()


@pytest.fixture
def no_worker(monkeypatch):
    """Stop the pipeline from starting its background thread."""
    import pipeline
    started = []
    monkeypatch.setattr(pipeline, "start_background_processing", lambda: started.append(1))
    return started
