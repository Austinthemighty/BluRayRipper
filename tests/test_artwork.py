"""TMDB artwork selection + placement (poster/fanart override of disc art)."""
import pytest

from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.services.metadata import MovieCandidate, ShowCandidate, _pick_art


def test_pick_art_prefers_exact_title_and_year():
    cands = [
        MovieCandidate(1, "WarGames", 2020, "remake", poster_path="/wrong.jpg"),
        MovieCandidate(2, "WarGames", 1983, "orig", poster_path="/right.jpg", backdrop_path="/bd.jpg"),
    ]
    poster, backdrop = _pick_art(cands, "WarGames", 1983, "title")
    assert poster.endswith("/right.jpg") and backdrop.endswith("/bd.jpg")
    assert poster.startswith("https://image.tmdb.org/t/p/")


def test_pick_art_falls_back_to_first_result():
    cands = [
        MovieCandidate(1, "Some Other Film", 2001, "", poster_path="/first.jpg"),
        MovieCandidate(2, "Also Not It", 2002, "", poster_path="/second.jpg"),
    ]
    poster, _ = _pick_art(cands, "Nonmatching", 1999, "title")
    assert poster.endswith("/first.jpg")   # TMDB relevance order


def test_pick_art_handles_no_results_and_missing_paths():
    assert _pick_art([], "X", 2000, "title") == (None, None)
    cands = [ShowCandidate(1, "Show", 2010, "", poster_path=None, backdrop_path=None)]
    assert _pick_art(cands, "Show", 2010, "name") == (None, None)


async def test_place_tmdb_art_overrides_disc_art(tmp_path, monkeypatch):
    from blurayripper.config import load_settings
    from blurayripper.pipeline import Job, Pipeline

    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    monkeypatch.setattr(settings, "tmdb_api_key", "fake-key")   # tmdb.configured -> True
    monkeypatch.setattr(settings, "fetch_tmdb_artwork", True)
    pipe = Pipeline(settings)

    # Stub the network: resolve URLs and "download" by writing marker bytes.
    async def fake_movie_artwork(title, year):
        return (f"https://img/{title}-poster.jpg", f"https://img/{title}-fanart.jpg")

    async def fake_save(url, dest):
        from pathlib import Path
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"TMDB:" + url.encode())
        return True

    monkeypatch.setattr(pipe.tmdb, "movie_artwork", fake_movie_artwork)
    monkeypatch.setattr(pipe.tmdb, "save_image", fake_save)

    job = Job(source="file:/x.mkv")
    job.selected_title_ids = [0]
    job.assignments = {0: EpisodeAssignment(
        title_id=0, kind=MediaKind.MOVIE, show_or_movie_title="WarGames", year=1983)}

    events = []
    class R:
        def on_event(self, m): events.append(m)
        def on_progress(self, *a): pass
    await pipe._place_tmdb_art(job, R())

    folder = pipe._poster_folder(job.assignments[0])
    assert (folder / "poster.jpg").read_bytes() == b"TMDB:https://img/WarGames-poster.jpg"
    assert (folder / "fanart.jpg").read_bytes() == b"TMDB:https://img/WarGames-fanart.jpg"
    assert any("TMDB artwork" in e for e in events)


async def test_place_tmdb_art_noop_without_key(tmp_path, monkeypatch):
    from blurayripper.config import load_settings
    from blurayripper.pipeline import Job, Pipeline
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    monkeypatch.setattr(settings, "tmdb_api_key", None)   # not configured
    pipe = Pipeline(settings)
    job = Job(source="file:/x.mkv")
    job.selected_title_ids = [0]
    job.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)}
    called = []
    async def boom(*a): called.append(1); return (None, None)
    monkeypatch.setattr(pipe.tmdb, "movie_artwork", boom)
    class R:
        def on_event(self, m): pass
        def on_progress(self, *a): pass
    await pipe._place_tmdb_art(job, R())
    assert not called   # short-circuits before any TMDB call
