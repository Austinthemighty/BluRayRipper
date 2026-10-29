"""Is every episode of a box set accounted for?

Run by hand on How I Met Your Mother on 2026-10-28 before this existed, the
check found three discs never ripped (S01 E09-E16, S02 E17-E22, S09 E17-E24)
and a bonus title in job 419 named S03E01, which would have overwritten the
real S03E01 from job 417. These pin the verdicts that found them.
"""

import pytest

from blurayripper.services.season_check import (
    Claim, check_season, episode_ranges, library_episodes)

S1 = {n: f"Episode {n}" for n in range(1, 23)}           # 22 episodes


def test_ranges_collapse_runs():
    assert episode_ranges([9, 10, 11, 12, 16]) == [(9, 12), (16, 16)]
    assert episode_ranges([]) == []


def test_a_disc_never_ripped_shows_as_missing_with_the_disc_that_holds_it():
    claims = {n: [Claim(395, "ready")] for n in range(1, 9)}
    claims.update({n: [Claim(423, "ready")] for n in range(17, 23)})
    plan = {n: "S01 D02" for n in range(9, 17)}

    s = check_season(1, S1, set(), claims, plan)

    assert s["status"] == "incomplete"
    assert s["missing"] == [{"first": 9, "last": 16, "discs": ["S01 D02"]}]
    assert s["counts"] == {"library": 0, "queued": 14, "failed": 0, "missing": 8}


def test_two_unfinished_jobs_naming_one_episode_is_a_conflict():
    """Job 419's bonus, named S03E01 beside job 417's real one."""
    claims = {1: [Claim(417, "ready"), Claim(419, "ready")]}
    s = check_season(3, {1: "Wait for It"}, set(), claims)
    assert s["conflicts"] == [{"episode": 1, "jobs": [417, 419]}]
    assert s["remakes"] == []


def test_a_re_rip_of_a_finished_episode_is_a_remake_not_a_conflict():
    """Re-ripping a disc whose audio was ruined is deliberate: the new job
    replaces the file the finished one placed."""
    claims = {1: [Claim(236, "done"), Claim(500, "ready")]}
    s = check_season(3, {1: "x"}, {1}, claims)
    assert s["conflicts"] == []
    assert s["remakes"] == [{"episode": 1, "jobs": [236, 500], "by": 500}]


def test_failed_jobs_and_files_on_disk_each_count_for_what_they_are():
    claims = {1: [Claim(7, "failed")], 2: [Claim(8, "done")], 3: [Claim(9, "done")]}
    s = check_season(1, {1: "a", 2: "b", 3: "c"}, {2}, claims)
    states = {e["episode"]: e["state"] for e in s["episodes"]}
    assert states == {1: "failed", 2: "library", 3: "missing"}   # 3: done, file gone
    assert s["status"] == "incomplete" and s["failed"] == [1]


def test_complete_in_progress_and_not_started():
    assert check_season(1, {1: "a"}, {1}, {})["status"] == "complete"
    assert check_season(1, {1: "a"}, set(), {1: [Claim(1, "encoding")]})["status"] == "in_progress"
    assert check_season(5, {1: "a"}, set(), {})["status"] == "not_started"
    assert check_season(9, {}, set(), {})["status"] == "unknown"


def test_an_episode_tmdb_does_not_list_is_called_out():
    """A 22-episode season with something named E23 — "Fill in order" ran
    past the end of the season."""
    s = check_season(1, {1: "a"}, {1}, {23: [Claim(5, "ready")]})
    assert s["unexpected"] == [23]


def test_the_library_is_read_from_the_file_names_two_parters_included(tmp_path):
    d = tmp_path / "How I Met Your Mother (2005)"
    (d / "Season 08").mkdir(parents=True)
    for name in ("How I Met Your Mother - S08E10.mkv", "How I Met Your Mother - S08E11-E12.mkv",
                 "How I Met Your Mother - S08E10.en.sdh.srt", "How I Met Your Mother - S08E10.nfo"):
        (d / "Season 08" / name).write_bytes(b"x")
    assert library_episodes(d) == {8: {10, 11, 12}}
    assert library_episodes(tmp_path / "nothing here") == {}


# --- the whole check, through the manager --------------------------------------------

async def test_the_manager_checks_every_season_against_tmdb(tmp_path, monkeypatch):
    from blurayripper import boxset_plan
    from blurayripper.config import load_settings
    from blurayripper.models import EpisodeAssignment, MediaKind
    from blurayripper.persistence import MemoryStore
    from blurayripper.pipeline import JobState
    from blurayripper.services import metadata
    from blurayripper.web.manager import JobManager, WebJob

    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "staging")
    monkeypatch.setattr(settings, "config_dir", tmp_path / "config")
    settings.tmdb_api_key = "key"
    m = JobManager(settings, store=MemoryStore())

    async def search(self, q): return [metadata.ShowCandidate(1100, "Show", 2005, "")]
    async def seasons(self, tv): return [1, 2]
    async def season(self, tv, s):
        return [metadata.Episode(s, n, f"E{n}", 22, "") for n in range(1, 5)]
    monkeypatch.setattr(metadata.TMDBClient, "search_tv", search)
    monkeypatch.setattr(metadata.TMDBClient, "get_show_seasons", seasons)
    monkeypatch.setattr(metadata.TMDBClient, "get_season", season)

    def job(jid, state, eps):
        wj = WebJob(id=jid, source="disc:0", state=state)
        wj.assignments = {i: EpisodeAssignment(i, MediaKind.TV, "Show", 2005, season=1, episode=e)
                          for i, e in enumerate(eps)}
        m.jobs[jid] = wj
    job(1, JobState.DONE, [1, 2])
    job(2, JobState.READY, [2])                        # a re-make of E02
    lib = tmp_path / "TV Shows" / "Show (2005)" / "Season 01"
    lib.mkdir(parents=True)
    for e in (1, 2):
        (lib / f"Show - S01E{e:02d}.mkv").write_bytes(b"x")
    (tmp_path / "config").mkdir()
    boxset_plan.save_plan(settings, "show,year,season,disc,first_episode,episodes\n"
                                    "Show,2005,1,2,3,2\n")

    r = await m.season_check("Show", 2005)

    s1, s2 = r["seasons"]
    assert r["tmdb_id"] == 1100 and r["library_note"] is None
    assert s1["counts"] == {"library": 2, "queued": 0, "failed": 0, "missing": 2}
    assert s1["missing"] == [{"first": 3, "last": 4, "discs": ["S01 D02"]}]
    assert s1["remakes"] == [{"episode": 2, "jobs": [1, 2], "by": 2}]
    assert s2["status"] == "not_started"             # a season never touched shows up too
    assert m.tv_shows() == [{"show": "Show", "year": 2005, "jobs": 2}]
    assert set(m.episode_claims("Show", 1, 2005, exclude=2)) == {1, 2}
    assert m.episode_claims("Show", 1, 2005, exclude=1) == {
        2: [{"job": 2, "state": "ready", "name": m._display_name(m.jobs[2])}]}


async def test_without_a_tmdb_key_the_check_says_why(tmp_path, monkeypatch):
    from blurayripper.config import load_settings
    from blurayripper.persistence import MemoryStore
    from blurayripper.web.manager import JobManager
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    settings.tmdb_api_key = None
    with pytest.raises(ValueError, match="TMDB key"):
        await JobManager(settings, store=MemoryStore()).season_check("Show")


def test_a_remake_with_the_same_name_is_a_different_show(tmp_path, monkeypatch):
    """Hawaii Five-O (1968) and its 2010 remake share a title in this library."""
    from blurayripper.config import load_settings
    from blurayripper.models import EpisodeAssignment, MediaKind
    from blurayripper.persistence import MemoryStore
    from blurayripper.pipeline import JobState
    from blurayripper.web.manager import JobManager, WebJob
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    m = JobManager(settings, store=MemoryStore())
    for jid, year in ((1, 1968), (2, 2010)):
        wj = WebJob(id=jid, source="disc:0", state=JobState.READY)
        wj.assignments = {0: EpisodeAssignment(0, MediaKind.TV, "Hawaii Five-O", year,
                                               season=1, episode=1)}
        m.jobs[jid] = wj

    assert [c["job"] for c in m.episode_claims("Hawaii Five-O", 1, 1968)[1]] == [1]
    assert [s["year"] for s in m.tv_shows()] == [1968, 2010]
