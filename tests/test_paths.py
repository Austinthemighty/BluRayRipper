"""Portable path storage so a rip on one machine resolves on another with a
different staging mount / OS."""

from pathlib import Path, PureWindowsPath

from blurayripper.pipeline import _rel_path, _resolve_path


def test_rel_path_is_posix_relative():
    root = Path("/mnt/nas/staging")
    p = root / "job_5" / "rip" / "t000" / "WarGames_t00.mkv"
    assert _rel_path(root, p) == "job_5/rip/t000/WarGames_t00.mkv"


def test_relative_resolves_against_a_different_root():
    rel = "job_5/rip/t000/WarGames_t00.mkv"
    # Stored by a Windows rip box, resolved by a Linux worker:
    assert _resolve_path(Path("/mnt/nas/staging"), rel) == Path("/mnt/nas/staging/job_5/rip/t000/WarGames_t00.mkv")


def test_absolute_stored_path_passes_through():
    # Files outside the staging root (e.g. a manual MKV) stay absolute.
    assert _resolve_path(Path("/mnt/nas/staging"), "/other/movie.mkv") == Path("/other/movie.mkv")
    # A Windows drive path is recognized as absolute, not joined to the root.
    out = _resolve_path(Path("/mnt/nas/staging"), "C:/rips/movie.mkv")
    assert str(out) in ("C:/rips/movie.mkv", str(Path("C:/rips/movie.mkv")))


def test_path_outside_root_falls_back_to_absolute():
    root = Path("/mnt/nas/staging")
    outside = Path("/somewhere/else/file.mkv")
    stored = _rel_path(root, outside)
    assert _resolve_path(root, stored) == outside
