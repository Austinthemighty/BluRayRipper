"""The failure hint used to read whatever was last in the log, whoever wrote it.

That produced confidently wrong advice — a drive told to be power-cycled when
the drive was fine — and, because a matched hint also cancels the retry budget,
it silently withheld the retry a transient failure deserved.
"""

from blurayripper.config import load_settings
from blurayripper.persistence import MemoryStore
from blurayripper.pipeline import JobState
from blurayripper.web.manager import JobManager, WebJob


def _mgr(tmp_path, monkeypatch):
    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path)
    s.auto_retry_max = 2
    return JobManager(s, store=MemoryStore())


def _job(log, error, mark=None):
    wj = WebJob(id=700, source="disc:0", state=JobState.FAILED, error=error)
    wj.log = list(log)
    wj._log_mark = len(log) if mark is None else mark
    return wj


def test_an_old_failure_line_does_not_diagnose_a_later_unrelated_one(tmp_path, monkeypatch):
    """A UHD disc with no volume key logs "Failed to open disc" during the scan.
    After a Retry, ANY later failure was reported as a drive that had dropped off
    the bus — the wrong remedy, pushed to the phone, about healthy hardware."""
    m = _mgr(tmp_path, monkeypatch)
    wj = _job(["Failed to open disc",                 # an EARLIER run
               "Retrying…",
               "ProcessError: HandBrakeCLI exited with 3"],
              error="ProcessError: HandBrakeCLI exited with 3",
              mark=1)                                  # this run starts after line 0
    assert m._rip_failure_hint(wj) is None


def test_this_run_s_own_drive_failure_is_still_caught(tmp_path, monkeypatch):
    """Scoping must not blind it to the real thing."""
    m = _mgr(tmp_path, monkeypatch)
    wj = _job(["Error 'Posix error - No such device' occurred while reading",
               "Failed to open disc"], error="ProcessError: Command failed (11)")
    wj._log_mark = 0
    hint, terminal = m._rip_failure_hint(wj)
    assert "dropped" in hint and terminal is True


async def test_a_disc_that_will_not_open_is_retried_rather_than_parked(tmp_path, monkeypatch):
    """"Failed to open disc" is what an unknown AACS volume key prints AND what a
    dead drive prints. Treating it as a dead drive parked the job and cancelled
    the retry, so a transient open failure never got its second chance."""
    m = _mgr(tmp_path, monkeypatch)
    wj = _job(["Failed to open disc"], error="ProcessError: Command failed (12)")
    wj._log_mark = 0
    m.jobs[700] = wj

    hint, terminal = m._rip_failure_hint(wj)
    assert terminal is False
    assert "volume key" in hint and "AACS" in hint

    m._schedule_auto_retry(wj)
    assert m._auto_retries.get(700, 0) == 1          # the retry budget was used
    assert any("volume key" in line for line in wj.log)   # ...and it still said why


def test_a_run_stamps_where_its_own_log_begins(tmp_path, monkeypatch):
    """The scoping only works if something sets the mark. A job that has never
    run reads its whole log, which is the old behaviour and the safe default."""
    m = _mgr(tmp_path, monkeypatch)
    wj = WebJob(id=701, source="disc:0", state=JobState.FAILED)
    wj.log = ["Failed to open disc"]
    assert m._run_log_mark(wj) == 0                  # no mark -> read everything
    wj._log_mark = 99                                # a mark past the end is clamped
    assert m._run_log_mark(wj) == 1


# --- a claimed job must be visibly claimed, immediately ----------------------

async def test_a_worker_publishes_the_job_it_claimed_before_it_starts_encoding(
        tmp_path, monkeypatch):
    """The heartbeat sleeps before its first touch, so for heartbeat_seconds the
    presence row still said job_id=None. serve decides "is anything working
    this?" by looking for a row naming the job, so a live encode read as stalled
    — and Requeue in that window releases the claim and runs a SECOND encode of
    the same title onto the same output path."""
    from blurayripper.persistence import JobRecord
    from blurayripper.worker import WorkerRunner

    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path)
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "stage")
    s.cluster.heartbeat_seconds = 3600          # the heartbeat will not fire

    registered = []

    class Store(MemoryStore):
        @property
        def durable(self): return True
        def record_worker(self, worker_id, version, job_id, stage, run_token, address=None):
            registered.append((worker_id, job_id, stage))

    runner = WorkerRunner(s, worker_id="encoder-A")
    runner.store = Store()

    async def encode_phase(job, staging_root, scheduler, report):
        # By the time any work runs, the claim must already be public.
        assert any(j == 42 for _w, j, _s in registered), registered
        return []

    monkeypatch.setattr(runner.pipeline, "encode_phase", encode_phase)
    monkeypatch.setattr("blurayripper.services.library.cleanup", lambda p: None)

    rec = JobRecord(id=42, source="disc:0", state="ready",
                    selected_title_ids=[], outputs=[])
    await runner._process(rec)

    assert registered[0][1] == 42               # first thing published, not None
    assert registered[0][2] == "claimed"


# --- a rip MakeMKV says failed must not reach the encode queue ---------------

async def test_a_rip_makemkv_reports_as_failed_is_not_queued_for_encoding(
        tmp_path, monkeypatch):
    """Jobs 334 and 335. MakeMKV hit a write error on the NAS mid-rip, said "0
    titles saved, 1 failed" — and both jobs were still logged as "Ripped +
    subtitles done — queued for an encode worker".

    It writes progressively, so the partial file it left behind defeated the
    "produced no output" guard, and the truncation check could not probe that
    file so it skipped. Believe what MakeMKV actually said instead."""
    import pytest

    from blurayripper.models import DiscInfo, Title
    from blurayripper.pipeline import Job, Pipeline

    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path)
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "stage")
    p = Pipeline(s)

    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="X", titles=[
        Title(title_id=0, duration_seconds=6000, size_bytes=1,
              source_filename="t00.mkv", streams=[])])
    job.selected_title_ids = [0]

    async def rip_title(source, title_id, dest_dir, *, on_progress=None,
                        on_message=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        partial = dest_dir / "C1_t00.mkv"
        partial.write_bytes(b"partial")        # MakeMKV wrote some of it
        on_message("Error 'Posix error - Resource temporarily unavailable' "
                   "occurred while writing data")
        on_message("Failed to save title 0 to file " + str(partial))
        on_message("0 titles saved, 1 failed")
        return [partial]                        # ...so it is NOT "no output"

    monkeypatch.setattr(p.makemkv, "rip_title", rip_title)

    with pytest.raises(RuntimeError, match="could not save title 0"):
        await p._ensure_ripped(job, 0, tmp_path / "stage" / "job_1", _Rep())

    assert "ripped_path" not in job.checkpoints.get(0, {})   # nothing to encode


async def test_a_clean_rip_is_still_checkpointed(tmp_path, monkeypatch):
    """The guard must not reject good rips — MakeMKV's normal chatter includes
    plenty of lines, and only the failure ones may count."""
    from blurayripper.models import DiscInfo, Title
    from blurayripper.pipeline import Job, Pipeline

    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path)
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "stage")
    s.auto_eject = False
    p = Pipeline(s)

    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="X", titles=[
        Title(title_id=0, duration_seconds=100, size_bytes=1,
              source_filename="t00.mkv", streams=[])])
    job.selected_title_ids = [0]

    async def rip_title(source, title_id, dest_dir, *, on_progress=None,
                        on_message=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        good = dest_dir / "C1_t00.mkv"
        good.write_bytes(b"a real rip")
        on_message("Operation successfully completed")
        on_message("1 titles saved")
        return [good]

    class _Probe:
        duration_seconds = 100

    async def probe(path): return _Probe()

    monkeypatch.setattr(p.makemkv, "rip_title", rip_title)
    monkeypatch.setattr(p.mkvtools, "probe", probe)

    await p._ensure_ripped(job, 0, tmp_path / "stage" / "job_2", _Rep())
    assert job.checkpoints[0]["ripped_path"].endswith("C1_t00.mkv")


class _Rep:
    def __init__(self): self.events = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a, **k): pass
    def checkpoint(self): pass
    def notify(self, *a, **k): pass
    def ejected(self, *a, **k): pass
