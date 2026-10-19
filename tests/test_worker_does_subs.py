"""cluster.worker_does_subs: the rip box defers subtitle extraction/OCR, and the
worker runs it right before encoding (so restarting serve doesn't kill it)."""

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.pipeline import Job, Pipeline, TitleResult
from blurayripper.scheduler import EncodeScheduler


class _R:
    def on_event(self, *a): pass
    def on_progress(self, *a): pass
    def checkpoint(self): pass


def _job(ripped):
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="M",
                        titles=[Title(title_id=0, duration_seconds=6000)])
    job.selected_title_ids = [0]
    job.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "M", 2000)}
    job.checkpoints = {0: {"ripped_path": str(ripped)}}
    return job


def _pipe(tmp_path, monkeypatch, worker_does_subs):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "out")
    settings.cluster.worker_does_subs = worker_does_subs
    return Pipeline(settings)


async def test_prep_defers_subs_worker_runs_them_before_encode(tmp_path, monkeypatch):
    pipe = _pipe(tmp_path, monkeypatch, worker_does_subs=True)
    ripped = tmp_path / "m.mkv"; ripped.write_bytes(b"x")
    order = []

    async def fake_ripped(job, tid, sr, report):
        order.append("rip"); return ripped

    async def fake_subs(job, todo, report, staging_root=None):
        order.append("subs")

    async def fake_grain(job, r, tid, sr, report):
        return False

    async def fake_encode(job, tid, a, sr, r, sch, report):
        order.append("encode"); return TitleResult(tid, video_path=tmp_path / "o.mkv")

    monkeypatch.setattr(pipe, "_ensure_ripped", fake_ripped)
    monkeypatch.setattr(pipe, "_subtitles_for_titles", fake_subs)
    monkeypatch.setattr(pipe, "_resolve_grain", fake_grain)
    monkeypatch.setattr(pipe, "_encode_title", fake_encode)

    job = _job(ripped)
    # Rip box prep: rip + grain, NO subtitles.
    await pipe.prep_phase(job, tmp_path / "stage" / "job_1", _R(), include_subs=False)
    assert order == ["rip"]                      # subs deferred

    # Worker: subtitles first, then encode.
    order.clear()
    await pipe.encode_phase(job, tmp_path / "stage" / "job_1", EncodeScheduler(3), _R())
    assert order == ["subs", "encode"]


async def test_flag_off_worker_skips_subs(tmp_path, monkeypatch):
    pipe = _pipe(tmp_path, monkeypatch, worker_does_subs=False)
    ripped = tmp_path / "m.mkv"; ripped.write_bytes(b"x")
    order = []

    async def fake_subs(job, todo, report, staging_root=None):
        order.append("subs")

    async def fake_encode(job, tid, a, sr, r, sch, report):
        order.append("encode"); return TitleResult(tid, video_path=tmp_path / "o.mkv")

    monkeypatch.setattr(pipe, "_subtitles_for_titles", fake_subs)
    monkeypatch.setattr(pipe, "_encode_title", fake_encode)

    await pipe.encode_phase(_job(ripped), tmp_path / "stage" / "job_1", EncodeScheduler(3), _R())
    assert order == ["encode"]                   # worker does NOT do subs when flag off
