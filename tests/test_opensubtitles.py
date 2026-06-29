"""OpenSubtitles reference-subtitle selection (pure parsing) and config wiring."""

from blurayripper.config import load_settings
from blurayripper.services.opensubtitles import OpenSubtitlesClient, best_file_id


def _item(file_id, *, lang="en", trusted=False, downloads=0):
    return {
        "attributes": {
            "language": lang,
            "from_trusted": trusted,
            "download_count": downloads,
            "files": [{"file_id": file_id, "file_name": f"{file_id}.srt"}],
        }
    }


def test_best_file_prefers_trusted_then_downloads():
    data = [
        _item(1, downloads=500),
        _item(2, trusted=True, downloads=10),  # trusted wins despite fewer downloads
        _item(3, downloads=900),
    ]
    assert best_file_id(data) == 2


def test_best_file_filters_language():
    data = [_item(1, lang="es", downloads=999), _item(2, lang="en", downloads=5)]
    assert best_file_id(data, language="en") == 2


def test_best_file_none_when_no_files():
    assert best_file_id([{"attributes": {"language": "en", "files": []}}]) is None
    assert best_file_id([]) is None


def test_client_unconfigured_without_key():
    assert OpenSubtitlesClient(None).configured is False
    assert OpenSubtitlesClient("k").configured is True


def test_settings_load_opensubtitles_from_env(monkeypatch):
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "abc123")
    monkeypatch.setenv("OPENSUBTITLES_USERNAME", "me")
    s = load_settings()
    assert s.opensubtitles_api_key == "abc123"
    assert s.opensubtitles_username == "me"
