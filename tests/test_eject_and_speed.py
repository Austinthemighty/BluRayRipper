"""Drive-device resolution (for auto-eject), rip-speed formatting, and ETA."""

import asyncio

from blurayripper.pipeline import _RipSpeed, _fmt_eta
from blurayripper.tools.makemkv import parse_info

DRV_OUTPUT = [
    'DRV:0,0,999,0,"BD-RE ASUS BW-16D1HT","WarGames","F:"',
    'DRV:1,256,999,0,"","",""',
    "CINFO:2,0,\"WarGames\"",
    "TINFO:0,9,0,\"1:52:42\"",
]


def test_drive_device_resolved_for_disc_source():
    disc = parse_info(DRV_OUTPUT, "disc:0")
    assert disc.drive_device == "F:"


def test_drive_device_from_dev_source():
    disc = parse_info(DRV_OUTPUT, "dev:/dev/sr0")
    assert disc.drive_device == "/dev/sr0"


def test_file_source_has_no_drive_device():
    disc = parse_info(DRV_OUTPUT, "file:/mnt/rip")
    assert disc.drive_device is None


def test_rip_speed_blank_until_enough_data():
    # No total bytes -> no speed string.
    assert _RipSpeed(total_bytes=0).update(0.5) == ""
    # With total but first sample, EMA not warmed -> blank.
    assert _RipSpeed(total_bytes=10_000_000_000).update(0.0) == ""


def test_fmt_eta():
    assert _fmt_eta(45) == "45s"
    assert _fmt_eta(125) == "2m05s"
    assert _fmt_eta(3725) == "1h02m"


class _Report:
    def on_event(self, m): pass
    def on_progress(self, *a): pass


async def test_eject_fires_when_last_title_reused_but_earlier_ripped(tmp_path, monkeypatch):
    """Regression: a resume where the LAST selected title is reused from a
    checkpoint while an earlier title re-rips must still eject the disc."""
    import blurayripper.pipeline as pl
    from blurayripper.config import load_settings
    from blurayripper.models import DiscInfo, Title
    from blurayripper.pipeline import Job, Pipeline, _rel_path

    s = load_settings()
    s.auto_eject = True
    s.rip_duration_tolerance_pct = 0          # skip the post-rip duration probe
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    pipe = Pipeline(s)

    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="X", drive_device="D:",
                        titles=[Title(title_id=0), Title(title_id=1)])
    job.selected_title_ids = [0, 1]
    staging_root = tmp_path / "job_1"

    # Title 1 (the LAST selected) already ripped on a prior run -> reused.
    t1_dir = staging_root / "rip" / "t001"
    t1_dir.mkdir(parents=True)
    t1_file = t1_dir / "title.mkv"
    t1_file.write_text("x")
    job.checkpoints[1] = {"ripped_path": _rel_path(tmp_path, t1_file)}

    async def fake_rip(source, title_id, rip_dir, on_progress=None, on_message=None):
        rip_dir.mkdir(parents=True, exist_ok=True)
        p = rip_dir / f"t{title_id}.mkv"
        p.write_text("x")
        return [p]
    monkeypatch.setattr(pipe.makemkv, "rip_title", fake_rip)

    ejected = []
    monkeypatch.setattr(pl, "eject", lambda dev: ejected.append(dev))

    async def fake_dev(_job):
        return "D:"
    monkeypatch.setattr(pipe, "_eject_device_for", fake_dev)

    r = _Report()
    await pipe._ensure_ripped(job, 0, staging_root, r)   # freshly rips
    assert ejected == []                                  # not the last title -> no eject
    await pipe._ensure_ripped(job, 1, staging_root, r)   # reused, but LAST -> eject
    assert ejected == ["D:"]


class _NotifyReport:
    def __init__(self): self.notes = []
    def on_event(self, m): pass
    def on_progress(self, *a): pass
    def notify(self, title, message): self.notes.append((title, message))


async def test_rip_finish_notifies_with_movie_name(tmp_path, monkeypatch):
    """Finishing the last rip pushes a 'finished ripping' notification (named after
    the movie/show mapping), even when auto-eject is off."""
    from blurayripper.config import load_settings
    from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
    from blurayripper.pipeline import Job, Pipeline

    s = load_settings()
    s.auto_eject = False
    s.rip_duration_tolerance_pct = 0
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    pipe = Pipeline(s)

    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="DISC LABEL", titles=[Title(title_id=0)])
    job.selected_title_ids = [0]
    job.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "WALL-E", 2008)}

    async def fake_rip(source, title_id, rip_dir, on_progress=None, on_message=None):
        rip_dir.mkdir(parents=True, exist_ok=True)
        p = rip_dir / "t.mkv"
        p.write_text("x")
        return [p]
    monkeypatch.setattr(pipe.makemkv, "rip_title", fake_rip)

    r = _NotifyReport()
    await pipe._ensure_ripped(job, 0, tmp_path / "job_1", r)
    assert r.notes, "expected a finished-ripping notification"
    title, _msg = r.notes[0]
    assert "Finished ripping" in title and "WALL-E" in title


async def test_no_rip_no_notify(tmp_path, monkeypatch):
    """A resume that reuses the already-ripped last title (nothing ripped this run)
    doesn't re-notify."""
    from blurayripper.config import load_settings
    from blurayripper.models import DiscInfo, Title
    from blurayripper.pipeline import Job, Pipeline, _rel_path

    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    pipe = Pipeline(s)
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="X", titles=[Title(title_id=0)])
    job.selected_title_ids = [0]
    d = tmp_path / "job_1" / "rip" / "t000"
    d.mkdir(parents=True)
    f = d / "x.mkv"
    f.write_text("x")
    job.checkpoints[0] = {"ripped_path": _rel_path(tmp_path, f)}

    r = _NotifyReport()
    await pipe._ensure_ripped(job, 0, tmp_path / "job_1", r)
    assert r.notes == []


async def _run_prep_grain(tmp_path, monkeypatch, *, decided):
    """Drive prep_phase with every heavy step faked, spying on grain resolution.
    ``decided`` maps title_id -> a pre-existing tune_grain checkpoint."""
    from blurayripper.config import load_settings
    from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
    from blurayripper.pipeline import Job, Pipeline

    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    pipe = Pipeline(s)

    job = Job(source="disc:0", grain_mode="preview")
    job.disc = DiscInfo(source="disc:0", disc_name="X",
                        titles=[Title(title_id=i) for i in range(3)])
    job.selected_title_ids = [0, 1, 2]
    job.assignments = {i: EpisodeAssignment(i, MediaKind.TV, "Show", season=1, episode=i + 1)
                       for i in range(3)}
    for tid, val in decided.items():
        job.checkpoints[tid] = {"tune_grain": val}

    async def noop(*a, **k): return None
    for name in ("_resolve_source_drive", "_place_cover_art", "_place_tmdb_art",
                 "_subtitles_for_titles"):
        monkeypatch.setattr(pipe, name, noop)

    async def fake_ripped(job, title_id, staging_root, report):
        return tmp_path / f"t{title_id}.mkv"
    monkeypatch.setattr(pipe, "_ensure_ripped", fake_ripped)

    asked = []
    async def fake_resolve(job, ripped, title_id, staging_root, report):
        asked.append(title_id)
        return True
    monkeypatch.setattr(pipe, "_resolve_grain", fake_resolve)

    await pipe.prep_phase(job, tmp_path / "job_1", _Report(), include_subs=True)
    return job, asked


async def test_prep_phase_asks_grain_once_per_disc(tmp_path, monkeypatch):
    """A multi-title disc must ask for grain ONCE (not once per episode, which read
    as an endless 'pick grain' loop) and apply the choice to every title."""
    job, asked = await _run_prep_grain(tmp_path, monkeypatch, decided={})
    assert asked == [0]                                              # asked a single time
    assert all(job.checkpoints[i]["tune_grain"] is True for i in range(3))  # applied to all


async def test_prep_phase_skips_grain_already_decided(tmp_path, monkeypatch):
    """A resume must not re-park at the grain gate for titles already decided —
    only the remaining titles get one shared prompt."""
    job, asked = await _run_prep_grain(tmp_path, monkeypatch, decided={0: False})
    assert asked == [1]                                # first UNdecided title, once
    assert job.checkpoints[0]["tune_grain"] is False   # prior pick preserved
    assert job.checkpoints[1]["tune_grain"] is True
    assert job.checkpoints[2]["tune_grain"] is True    # shared with title 1


async def test_prep_phase_no_grain_prompt_when_all_decided(tmp_path, monkeypatch):
    """Every title already decided -> a resume never opens the grain gate at all."""
    _job, asked = await _run_prep_grain(
        tmp_path, monkeypatch, decided={0: True, 1: False, 2: True})
    assert asked == []


async def test_a_tray_that_never_opens_neither_freezes_serve_nor_strands_the_job(
        tmp_path, monkeypatch):
    """Ejecting talks to the drive, so a drive that has stopped answering blocks
    for as long as it likes. This ran INLINE on the event loop, so one stuck
    tray froze the entire server — every other job, the UI, the SSE stream — over
    a disc that had already been ripped. And since it ran straight after the rip
    was verified and checkpointed, the job itself stopped there: complete work,
    no error, no log line, just stopped.

    So: off the loop, and bounded. By this point the rip is safe on disk, so a
    tray that won't open costs a log line and nothing else — the job goes on to
    subtitles and the encode, and the disc can be taken out by hand.
    """
    import threading
    import time

    import blurayripper.pipeline as pl
    from blurayripper.config import load_settings
    from blurayripper.models import DiscInfo, Title
    from blurayripper.pipeline import Job, Pipeline

    s = load_settings()
    s.auto_eject = True
    s.rip_duration_tolerance_pct = 0          # skip the post-rip duration probe
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    monkeypatch.setattr(pl, "_EJECT_TIMEOUT", 0.05)
    pipe = Pipeline(s)

    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="X", drive_device="/dev/sr1",
                        titles=[Title(title_id=0)])
    job.selected_title_ids = [0]

    async def fake_rip(source, title_id, rip_dir, on_progress=None, on_message=None):
        rip_dir.mkdir(parents=True, exist_ok=True)
        out = rip_dir / f"t{title_id}.mkv"
        out.write_text("x")
        return [out]
    monkeypatch.setattr(pipe.makemkv, "rip_title", fake_rip)

    async def fake_dev(_job):
        return "/dev/sr1"
    monkeypatch.setattr(pipe, "_eject_device_for", fake_dev)

    ran_on: list[int] = []

    def never_opens(_device):
        ran_on.append(threading.get_ident())
        time.sleep(0.4)          # the drive never answers
    monkeypatch.setattr(pl, "eject", never_opens)

    class _Rec:
        def __init__(self): self.events = []
        def on_event(self, m): self.events.append(m)
        def on_progress(self, *a): pass

    # Something else on the loop, to prove the loop kept turning throughout.
    turns = 0

    async def _other_work():
        nonlocal turns
        while True:
            turns += 1
            await asyncio.sleep(0)

    beat = asyncio.ensure_future(_other_work())
    r = _Rec()
    ripped = await asyncio.wait_for(
        pipe._ensure_ripped(job, 0, tmp_path / "job_1", r), 5)
    beat.cancel()

    assert ripped.exists()                               # the rip is kept
    assert ran_on and ran_on[0] != threading.get_ident()  # ejected off the loop
    assert turns > 0                                     # ...which kept turning
    assert any("Auto-eject timed out" in m for m in r.events)
    assert not any("Ejected disc" in m for m in r.events)  # and never claimed success
