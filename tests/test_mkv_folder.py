"""Is a folder of .mkv files one show's episodes, or unrelated titles?"""

import asyncio
from pathlib import Path

from blurayripper.mkv_folder import analyse


def _p(*names):
    return [Path("Z:/x") / n for n in names]


def test_makemkv_season_folder_is_one_set():
    """The reported case: MakeMKV writes <base>_tNN.mkv for a ripped season."""
    s = analyse(_p("Rick and Morty Season 1_t04.mkv", "Rick and Morty Season 1_t05.mkv",
                   "Rick and Morty Season 1_t06.mkv"))
    assert s is not None
    assert s.base == "Rick and Morty Season 1"
    assert s.show == "Rick and Morty"     # cleaned for TMDB
    assert s.season == 1                  # read off the base
    assert [p.name for p in s.files] == [
        "Rick and Morty Season 1_t04.mkv",
        "Rick and Morty Season 1_t05.mkv",
        "Rick and Morty Season 1_t06.mkv",
    ]


def test_files_are_ordered_numerically_not_alphabetically():
    """_t10 must come after _t9, or every episode after the ninth is misnumbered."""
    s = analyse(_p("Show_t9.mkv", "Show_t10.mkv", "Show_t2.mkv"))
    assert [p.stem for p in s.files] == ["Show_t2", "Show_t9", "Show_t10"]


def test_a_folder_of_unrelated_movies_is_not_a_set():
    """The existing To Process folder must keep one job per file."""
    assert analyse(_p("Black Hawk Down (2001).mkv", "Bumblebee (2018).mkv",
                      "Cars 3 (2017).mkv")) is None


def test_a_single_file_is_never_a_set():
    assert analyse(_p("Rick and Morty Season 1_t04.mkv")) is None
    assert analyse([]) is None


def test_a_file_without_a_counter_breaks_the_set():
    """Mixed content shouldn't be forced into one show."""
    assert analyse(_p("Show_t01.mkv", "Show_t02.mkv", "Behind the Scenes.mkv")) is None


def test_a_numbered_movie_franchise_is_not_a_show():
    """The dangerous false positive: sequels share a stem and end in a number, so a
    naive counter rule would name Rocky 2/3/4 as episodes of "Rocky". A bare space
    before the number doesn't count as a counter — punctuation or a tag must."""
    assert analyse(_p("Rocky 2.mkv", "Rocky 3.mkv", "Rocky 4.mkv")) is None
    assert analyse(_p("Apollo 13.mkv", "Apollo 14.mkv")) is None


def test_numbers_inside_the_title_survive():
    s = analyse(_p("Rick and Morty Season 1_t04.mkv", "Rick and Morty Season 1_t05.mkv"))
    assert s.base.endswith("Season 1")     # the season number survived


def test_other_common_counter_styles():
    for names in (("Show - 01.mkv", "Show - 02.mkv"),
                  ("Show.E01.mkv", "Show.E02.mkv"),
                  ("Show_01.mkv", "Show_02.mkv")):
        s = analyse(_p(*names))
        assert s is not None and s.base.casefold().startswith("show")


def test_season_absent_from_the_name_is_left_unknown():
    s = analyse(_p("Some Show_t01.mkv", "Some Show_t02.mkv"))
    assert s is not None and s.season is None    # the form asks, rather than guessing


def _mgr(tmp_path, sources):
    """A JobManager holding file jobs parked at naming — the state N separately
    added episode files sit in."""
    from blurayripper.config import load_settings
    from blurayripper.persistence import MemoryStore
    from blurayripper.pipeline import JobState
    from blurayripper.web.manager import JobManager, WebJob

    settings = load_settings()
    settings.storage.output_dir = tmp_path
    m = JobManager(settings, store=MemoryStore())
    for i, src in enumerate(sources, start=1):
        m.jobs[i] = WebJob(id=i, source=str(src), state=JobState.AWAITING_MAPPING)
    return m


def test_separately_added_episode_files_offer_to_group(tmp_path):
    """The rick-and-morty case: 10 files added one at a time became 10 jobs, each
    wanting the same show name typed again. They can be folded back into the one
    job a folder scan would have made."""
    folder = tmp_path / "s1"
    folder.mkdir()
    files = [folder / f"Rick and Morty Season 1_t{n:02d}.mkv" for n in range(3)]
    for f in files:
        f.write_bytes(b"")
    m = _mgr(tmp_path, files)

    members, mkv_set = m._group_candidates(m.jobs[1])
    assert [o.id for o in members] == [1, 2, 3]
    assert mkv_set.show == "Rick and Morty"
    assert m._job_dict(m.jobs[1])["group_with"] == 3


def test_unrelated_films_in_one_folder_are_never_grouped(tmp_path):
    """The guard that matters: grouping these would name Rocky III as an episode
    of Rocky II. Offer nothing, and refuse if asked directly."""
    import pytest

    folder = tmp_path / "movies"
    folder.mkdir()
    files = [folder / f"Rocky {n}.mkv" for n in (2, 3, 4)]
    for f in files:
        f.write_bytes(b"")
    m = _mgr(tmp_path, files)

    assert m._group_candidates(m.jobs[1]) == ([], None)
    assert m._job_dict(m.jobs[1])["group_with"] == 0
    with pytest.raises(ValueError, match="don't look like one show's episodes"):
        asyncio.run(m.group_season(1))


def test_a_job_already_being_worked_is_left_out_of_a_group(tmp_path):
    """Grouping replaces jobs, so it must only ever touch ones with no rip or
    encode work to throw away."""
    from blurayripper.pipeline import JobState

    folder = tmp_path / "s2"
    folder.mkdir()
    files = [folder / f"Show Season 2_t{n:02d}.mkv" for n in range(3)]
    for f in files:
        f.write_bytes(b"")
    m = _mgr(tmp_path, files)
    m.jobs[3].state = JobState.ENCODING            # already running — hands off

    members, _ = m._group_candidates(m.jobs[1])
    assert [o.id for o in members] == [1, 2]
