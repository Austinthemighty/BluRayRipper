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


# --- waiting out OpenSubtitles' download quota -----------------------------------------

def _soon(hours):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) + timedelta(hours=hours)


class _QuotaOsub(_FakeOsub):
    """Gives ``allowance`` downloads, then the quota is spent until ``refill``."""

    def __init__(self, allowance, reset_at=None, status=406):
        self.allowance, self.reset_at, self.status = allowance, reset_at, status
        self.cache: dict[int, str] = {}

    def refill(self, n=99):
        self.allowance = n

    async def fetch_episode_srt(self, tmdb_id, season, episode, lang):
        from blurayripper.services.opensubtitles import QuotaExhausted
        if episode in self.cache:
            return self.cache[episode]
        if self.allowance <= 0:
            raise QuotaExhausted("quota", reset_at=self.reset_at, status=self.status)
        self.allowance -= 1
        text = await super().fetch_episode_srt(tmdb_id, season, episode, lang)
        if text:
            self.cache[episode] = text
        return text

    def cached_episode_srt(self, tmdb_id, season, episode, lang):
        return self.cache.get(episode)


async def test_a_spent_quota_waits_for_the_reset_and_then_finishes(tmp_path, monkeypatch):
    """It used to fail the job, and auto-retry tried again every few minutes all
    day — every try spending nothing and getting nowhere until the reset."""
    rips = {0: _srt(EP[12]), 1: _srt(EP[10]), 2: _srt(EP[11])}
    m, wj, executed = _make_manager(tmp_path, monkeypatch, rips)
    m.osub = osub = _QuotaOsub(allowance=2, reset_at=_soon(5))
    notes, slept = [], []
    m._notify = lambda title, msg, priority=None, job_id=None: notes.append(title)
    seen_while_waiting = {}

    async def sleep(seconds):
        slept.append(seconds)
        d = m._job_dict(wj)
        seen_while_waiting.update(state=d["state"], waiting=d["waiting_until"],
                                  stalled=d["stalled"], stage=wj.stage)
        osub.refill()                                  # the reset comes
    monkeypatch.setattr(m, "_quota_sleep", sleep)

    m.set_auto_identify(1, "Show", 2020, 999, 1, [10, 11, 12], [0, 1, 2])
    await _settle()

    assert wj.state is JobState.DONE
    assert executed["assignments"] == {0: 12, 1: 10, 2: 11}
    assert len(slept) == 1 and 5 * 3600 < slept[0] <= 5 * 3600 + 61
    assert seen_while_waiting["state"] == "identifying"
    assert seen_while_waiting["waiting"] and seen_while_waiting["stalled"] is False
    assert seen_while_waiting["stage"].startswith("Waiting for OpenSubtitles until")
    assert any("Waiting for OpenSubtitles — the download quota resets at" in line
               for line in wj.log)
    assert wj._wait_until is None and m._job_dict(wj)["waiting_until"] is None
    assert m._auto_retries == {}
    assert sum("Waiting for OpenSubtitles" in t for t in notes) == 1


async def test_a_partial_set_that_names_every_title_does_not_wait(tmp_path, monkeypatch):
    """The quota ran out on an episode none of the titles turned out to be."""
    monkeypatch.setitem(EP, 13, [f"the old lighthouse keeper sang at night {i}"
                                 for i in range(40)])
    rips = {0: _srt(EP[10]), 1: _srt(EP[11]), 2: _srt(EP[12])}
    m, wj, executed = _make_manager(tmp_path, monkeypatch, rips)
    m.osub = _QuotaOsub(allowance=3, reset_at=_soon(5))

    async def sleep(seconds):
        raise AssertionError("waited for a quota it didn't need")
    monkeypatch.setattr(m, "_quota_sleep", sleep)

    m.set_auto_identify(1, "Show", 2020, 999, 1, [10, 11, 12, 13], [0, 1, 2])
    await _settle()

    assert wj.state is JobState.DONE
    assert executed["assignments"] == {0: 10, 1: 11, 2: 12}


async def test_set_naming_stops_the_wait(tmp_path, monkeypatch):
    rips = {0: _srt(EP[12]), 1: _srt(EP[10]), 2: _srt(EP[11])}
    m, wj, executed = _make_manager(tmp_path, monkeypatch, rips)
    m.osub = _QuotaOsub(allowance=0, reset_at=_soon(5))
    waiting, forever = asyncio.Event(), asyncio.Event()

    async def sleep(seconds):
        waiting.set()
        await forever.wait()
    monkeypatch.setattr(m, "_quota_sleep", sleep)

    m.set_auto_identify(1, "Show", 2020, 999, 1, [10, 11, 12], [0, 1, 2])
    await asyncio.wait_for(waiting.wait(), 5)

    m.remap(1)
    await _settle()

    assert wj.state is JobState.AWAITING_MAPPING
    assert wj._task.cancelled()
    assert wj._wait_until is None and "assignments" not in executed


@pytest.mark.parametrize("reset_in_hours, status, expected", [
    (2, 406, 2 * 3600 + 60),        # until the reset, plus a minute's grace
    (None, 429, 60),                # a bare 429: the rate limit, over in moments
    (None, 406, 3600),              # the daily quota with no time given: look hourly
    (-1, 406, 60),                  # a reset already past never means "don't wait"
    (30, 406, 24 * 3600),           # nor more than a day
])
async def test_how_long_a_quota_wait_lasts(tmp_path, monkeypatch, reset_in_hours,
                                           status, expected):
    from blurayripper.services.opensubtitles import QuotaExhausted
    m, wj, _ = _make_manager(tmp_path, monkeypatch, {})
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(m, "_quota_sleep", sleep)
    reset = _soon(reset_in_hours) if reset_in_hours is not None else None

    class _Rep:
        def on_event(self, *_): pass

    await m._wait_for_quota(wj, _Rep(), QuotaExhausted("q", reset_at=reset, status=status))
    assert slept[0] == pytest.approx(expected, abs=2)


async def test_waking_from_the_wait_is_not_read_as_a_stall(tmp_path, monkeypatch):
    """Hours of quiet while it slept would otherwise flag it the moment it woke."""
    from blurayripper.services.opensubtitles import QuotaExhausted
    m, wj, _ = _make_manager(tmp_path, monkeypatch, {})
    wj._activity_at = wj._progress_at = 0.0           # last heard from long ago

    async def sleep(seconds):
        assert m._is_stalled(wj, JobState.IDENTIFYING) is False
    monkeypatch.setattr(m, "_quota_sleep", sleep)

    class _Rep:
        def on_event(self, *_): pass

    await m._wait_for_quota(wj, _Rep(), QuotaExhausted("q", reset_at=_soon(1), status=406))
    assert wj._activity_at > 0 and wj._progress_at > 0
    assert m._is_stalled(wj, JobState.IDENTIFYING) is False


async def test_one_push_for_a_spent_quota_however_many_discs_wait_on_it(
        tmp_path, monkeypatch):
    """The quota is one thing running out. A box set of discs waiting on it is
    one piece of news, not one notification each."""
    from blurayripper.services.opensubtitles import QuotaExhausted
    m, wj, _ = _make_manager(tmp_path, monkeypatch, {})
    notes = []
    m._notify = lambda title, msg, priority=None, job_id=None: notes.append(job_id)

    async def sleep(seconds): pass
    monkeypatch.setattr(m, "_quota_sleep", sleep)

    class _Rep:
        def on_event(self, *_): pass

    cut = QuotaExhausted("q", reset_at=_soon(3), status=406)
    await m._wait_for_quota(wj, _Rep(), cut)
    assert notes == [1]

    earlier = WebJob(id=2, source="disc:1", state=JobState.READY)
    earlier._wait_until = _soon(3)                 # already waiting on the same quota
    m.jobs[2] = earlier
    await m._wait_for_quota(wj, _Rep(), cut)
    assert notes == [1]
