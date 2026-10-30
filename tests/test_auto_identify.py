"""Subtitle-based episode identification orchestration in the JobManager:
confident matches auto-proceed; ambiguous ones park for confirmation."""

import asyncio

import pytest

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, Title
from blurayripper.persistence import MemoryStore
from blurayripper.pipeline import Job, JobState
from blurayripper.web.manager import JobManager, WebJob


def _srt(lines):
    return "\n".join(
        f"{i}\n00:00:{i % 60:02d},000 --> 00:00:{(i + 1) % 60:02d},000\n{t}\n"
        for i, t in enumerate(lines, 1)
    )


EP = {
    10: [f"the reactor core is overheating fast {i}" for i in range(40)],
    11: [f"she walked along the quiet beach alone {i}" for i in range(40)],
    12: [f"they argued about money at dinner {i}" for i in range(40)],
}


async def _settle():
    for _ in range(40):
        await asyncio.sleep(0)


class _FakeOsub:
    configured = True

    async def fetch_episode_srt(self, tmdb_id, season, episode, lang):
        return _srt(EP[episode]) if episode in EP else None


def _make_manager(tmp_path, monkeypatch, rips):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    settings.opensubtitles_api_key = "key"
    m = JobManager(settings, store=MemoryStore())
    m.osub = _FakeOsub()

    wj = WebJob(id=1, source="disc:0", state=JobState.AWAITING_MAPPING)
    wj.disc = DiscInfo(source="disc:0", disc_name="Show",
                       titles=[Title(title_id=i, duration_seconds=1300) for i in (0, 1, 2)])
    wj.selected_title_ids = [0, 1, 2]
    job = Job(source="disc:0")
    job.disc = wj.disc
    wj._pipeline_job = job
    m.jobs[1] = wj

    async def fake_identify(job, staging_root, report):
        return rips

    monkeypatch.setattr(m.pipeline, "identify_phase", fake_identify)
    executed = {}

    async def fake_execute(job, scheduler, report, job_name=None):
        executed["assignments"] = {tid: a.episode for tid, a in job.assignments.items()}
        job.results = []
        return job

    monkeypatch.setattr(m.pipeline, "execute", fake_execute)
    return m, wj, executed


async def test_confident_match_auto_assigns_and_runs(tmp_path, monkeypatch):
    # Titles scrambled vs episode order, but each clearly matches one episode.
    rips = {0: _srt(EP[12]), 1: _srt(EP[10]), 2: _srt(EP[11])}
    m, wj, executed = _make_manager(tmp_path, monkeypatch, rips)

    m.set_auto_identify(1, "Show", 2020, 999, 1, [10, 11, 12], [0, 1, 2])
    await _settle()

    assert wj.state is JobState.DONE
    assert executed["assignments"] == {0: 12, 1: 10, 2: 11}  # correctly de-scrambled


async def test_ambiguous_match_parks_then_confirm_resumes(tmp_path, monkeypatch):
    # Two titles with identical dialogue -> can't be told apart -> needs confirm.
    same = [f"the same spoken line {i % 3}" for i in range(40)]
    rips = {0: _srt(same), 1: _srt(same), 2: _srt(EP[11])}
    m, wj, executed = _make_manager(tmp_path, monkeypatch, rips)

    m.set_auto_identify(1, "Show", 2020, 999, 1, [10, 11, 12], [0, 1, 2])
    await _settle()

    assert wj.state is JobState.AWAITING_MATCH
    assert "assignments" not in executed
    assert any(not c["confident"] for c in wj.match_candidates)

    # User resolves the ambiguous ones explicitly.
    m.confirm_match(1, {0: 10, 1: 12, 2: 11})
    await _settle()

    assert wj.state is JobState.DONE
    assert executed["assignments"] == {0: 10, 1: 12, 2: 11}


async def test_a_two_parter_on_the_disc_is_named_for_both_episodes(tmp_path, monkeypatch):
    """Job 399's disc, in miniature: one title twice the usual length holding
    E11 and E12, the rest one episode each. It used to park every time, with
    the two-parter unsure and the episode after it unsearched."""
    monkeypatch.setitem(EP, 13, [f"the old lighthouse keeper sang at night {i}"
                                 for i in range(40)])
    rips = {0: _srt(EP[10]), 1: _srt(EP[11] + EP[12]), 2: _srt(EP[13])}
    m, wj, executed = _make_manager(tmp_path, monkeypatch, rips)
    wj.disc.titles[1].duration_seconds = 2600           # the long one
    named = {}

    async def fake_execute(job, scheduler, report, job_name=None):
        named.update({t: a.episodes for t, a in job.assignments.items()})
        job.results = []
        return job
    monkeypatch.setattr(m.pipeline, "execute", fake_execute)

    m.set_auto_identify(1, "Show", 2020, 999, 1, [10, 11, 12, 13], [0, 1, 2])
    await _settle()

    assert wj.state is JobState.DONE
    assert named == {0: [10], 1: [11, 12], 2: [13]}
    assert any("title 1 -> E11–E12" in line for line in wj.log)


async def test_a_confirmed_two_parter_keeps_both_episodes(tmp_path, monkeypatch):
    m, wj, executed = _make_manager(tmp_path, monkeypatch, {})
    wj.state = JobState.AWAITING_MATCH
    wj.show_title, wj.season = "Show", 1

    m.confirm_match(1, {0: 10, 1: 11, 2: 13}, ends={1: 12})

    assert wj.assignments[1].episodes == [11, 12]
    assert wj.assignments[2].episodes == [13]


def test_confirming_a_two_parter_over_its_neighbour_is_refused(tmp_path, monkeypatch):
    import pytest as _pytest
    m, wj, executed = _make_manager(tmp_path, monkeypatch, {})
    wj.state = JobState.AWAITING_MATCH
    wj.show_title, wj.season = "Show", 1

    with _pytest.raises(ValueError, match="both include S01E12"):
        m.confirm_match(1, {0: 10, 1: 11, 2: 12}, ends={1: 12})
    assert wj.state is JobState.AWAITING_MATCH


async def test_the_next_disc_rips_while_this_one_is_still_being_identified(
        tmp_path, monkeypatch):
    """The OCR, the reference downloads and the match read the ripped files.
    Holding the drive through them kept the next disc in that drive waiting
    for all of it; on a Blu-ray box set the OCR alone is minutes a title."""
    rips = {0: _srt(EP[10]), 1: _srt(EP[11]), 2: _srt(EP[12])}
    m, wj, executed = _make_manager(tmp_path, monkeypatch, rips)
    ocr_running, ocr_may_finish = asyncio.Event(), asyncio.Event()

    async def identify(job, staging_root, report):
        report.ripped()                     # what identify_phase does after its rips
        ocr_running.set()
        await ocr_may_finish.wait()
        return rips
    monkeypatch.setattr(m.pipeline, "identify_phase", identify)

    m.set_auto_identify(1, "Show", 2020, 999, 1, [10, 11, 12], [0, 1, 2])
    await asyncio.wait_for(ocr_running.wait(), 5)
    assert any("drive released" in line for line in wj.log)

    next_disc = WebJob(id=2, source="disc:0", state=JobState.READY)
    async def rip_next_disc():
        async with m._prep_slot(next_disc):
            return True
    assert await asyncio.wait_for(rip_next_disc(), 1)

    ocr_may_finish.set()
    await _settle()
    assert wj.state is JobState.DONE


async def test_waiting_on_reference_downloads_holds_no_prep_slot(tmp_path, monkeypatch):
    """The downloads are network waits, not work. Inside a prep slot they kept
    another disc's rip or OCR from starting."""
    rips = {0: _srt(EP[10]), 1: _srt(EP[11]), 2: _srt(EP[12])}
    m, wj, executed = _make_manager(tmp_path, monkeypatch, rips)
    m._prep_sem = asyncio.Semaphore(1)
    downloading, may_answer = asyncio.Event(), asyncio.Event()

    class SlowOsub(_FakeOsub):
        async def fetch_episode_srt(self, tmdb_id, season, episode, lang):
            downloading.set()
            await may_answer.wait()
            return await super().fetch_episode_srt(tmdb_id, season, episode, lang)
    m.osub = SlowOsub()

    m.set_auto_identify(1, "Show", 2020, 999, 1, [10, 11, 12], [0, 1, 2])
    await asyncio.wait_for(downloading.wait(), 5)

    other = WebJob(id=2, source="/media/other.mkv", state=JobState.READY)
    async def prep_other():
        async with m._prep_slot(other):
            return True
    assert await asyncio.wait_for(prep_other(), 1)

    may_answer.set()
    await _settle()
    assert wj.state is JobState.DONE
