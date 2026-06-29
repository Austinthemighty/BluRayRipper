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
