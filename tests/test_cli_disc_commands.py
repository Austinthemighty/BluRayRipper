"""The disc-facing CLI commands are what a script (or a person at 1am with a
stack of discs) actually drives: scan, rip, queue, coverart, resub, eject,
close, verify-subs.

They are the only interface that reports by exit code. A command that prints a
red error and still exits 0 is worse than one that crashes — the batch file
keeps going, the next disc goes in, and nobody finds out until the library is
short. These tests pin the exit codes, the selector each command hands to the
tool, and the messages that say what to do next.

``check`` is covered by tests/test_check_diagnostics.py and is not repeated here.
"""

import argparse
import asyncio
import struct

import pytest

from blurayripper import cli
from blurayripper.config import ToolNotFoundError, load_settings
from blurayripper.models import DiscInfo, Stream, StreamType, Title
from blurayripper.persistence import JobRecord, MemoryStore, disc_to_dict
from blurayripper.pipeline import Job, TitleResult
from blurayripper.tools.discinfo import disc_fingerprint
from blurayripper.tools.makemkv import Drive

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _settings(tmp_path, **over):
    """Settings that are otherwise fine, so a non-zero exit is attributable.

    load_settings() reads the repo's real config, which on a dev box may carry a
    database URL or a TMDB key; both change what these commands print, so pin
    them here rather than inheriting whatever the machine has.
    """
    s = load_settings()
    s.config_dir = tmp_path
    s.storage.staging_dir = tmp_path / "staging"
    s.storage.output_dir = tmp_path / "out"
    s.database_url = None
    s.tmdb_api_key = "tmdb-key"
    s.languages.keep = ["eng"]
    for key, value in over.items():
        setattr(s, key, value)
    return s


def _use(monkeypatch, settings):
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    return settings


def _flat(captured) -> str:
    """Rich word-wraps to 80 columns when stdout isn't a terminal, so a message
    that reads as one sentence arrives split across lines. Collapse it back."""
    return " ".join(captured.out.split())


def _ns(**kw) -> argparse.Namespace:
    kw.setdefault("config", None)
    return argparse.Namespace(**kw)


def _title(title_id, *, seconds=7200, size=0, subs=(), name=None):
    streams = [Stream(stream_id=i, stream_type=StreamType.SUBTITLE, language=lang)
               for i, lang in enumerate(subs)]
    return Title(title_id=title_id, duration_seconds=seconds, chapter_count=12,
                 size_bytes=size, source_filename=f"{title_id:05d}.mpls",
                 name=name, streams=streams)


def _png(path, width, height):
    """A 24-byte PNG header — enough for coverart.image_size to read w/h, which
    is what decides poster (portrait) vs fanart (landscape)."""
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">II", width, height))


def _fake_makemkv(discs, calls=None):
    """A MakeMKV whose scan result depends on the selector it was given, so an
    assertion about what got printed is also an assertion about what was asked."""

    class _FakeMakeMKV:
        def __init__(self, settings):
            self.settings = settings

        async def scan(self, source, *, on_message=None):
            if calls is not None:
                calls.append(source)
            disc, messages = discs[source]
            for m in messages:
                if on_message is not None:
                    on_message(m)
            return disc

    return _FakeMakeMKV


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


def test_scan_probes_the_drive_the_user_named(tmp_path, monkeypatch, capsys):
    """`scan disc:1` must open drive 1. Two drives in the same machine with two
    discs in them is the normal case here, and scanning the wrong one silently
    lists the wrong titles — which then get ripped."""
    _use(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "MakeMKV", _fake_makemkv({
        "disc:0": (DiscInfo(source="disc:0", disc_name="ALIEN_RESURRECTION",
                            titles=[_title(1)]), []),
        "disc:1": (DiscInfo(source="disc:1", disc_name="THE_THING",
                            titles=[_title(4)]), []),
    }))

    rc = asyncio.run(cli._cmd_scan(_ns(source="disc:1")))

    out = _flat(capsys.readouterr())
    assert rc == 0
    assert "THE_THING" in out
    assert "ALIEN_RESURRECTION" not in out


def test_scan_of_a_disc_with_no_titles_repeats_what_makemkv_said(
        tmp_path, monkeypatch, capsys):
    """An empty title list is the single most common failure, and MakeMKV always
    explains it (expired registration, unreadable disc, a UHD drive that won't
    open). Those MSG lines are thrown away unless scan prints them, leaving the
    user with a blank table and no reason."""
    _use(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "MakeMKV", _fake_makemkv({
        "disc:0": (DiscInfo(source="disc:0", disc_name="UNKNOWN", titles=[]),
                   ["Evaluation period has expired", "Failed to open disc"]),
    }))

    rc = asyncio.run(cli._cmd_scan(_ns(source="disc:0")))

    out = _flat(capsys.readouterr())
    assert rc == 0                       # scan is a diagnostic; it never fails
    assert "Evaluation period has expired" in out
    assert "Failed to open disc" in out
    assert "Common causes" in out


def test_scan_of_a_readable_disc_does_not_print_the_empty_disc_advice(
        tmp_path, monkeypatch, capsys):
    """The hint block is only useful when there is nothing to show. Printing it
    after a good scan trains the user to skip past it — and past the real one."""
    _use(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "MakeMKV", _fake_makemkv({
        "disc:0": (DiscInfo(source="disc:0", disc_name="GOOD_DISC",
                            titles=[_title(3, seconds=5400, size=24_100_000_000)]),
                   ["Operation successfully completed"]),
    }))

    asyncio.run(cli._cmd_scan(_ns(source="disc:0")))

    out = _flat(capsys.readouterr())
    assert "Common causes" not in out
    assert "Operation successfully completed" not in out
    assert "1:30:00" in out              # the title table, not the hint


def test_scan_prints_the_fingerprint_that_recalls_a_saved_mapping(
        tmp_path, monkeypatch, capsys):
    """The fingerprint is how a re-rip skips the naming gate. When a disc is
    NOT recognised, the printed value is the only way to see whether the disc
    hashed differently than last time."""
    disc = DiscInfo(source="disc:0", volume_label="SEASON_1",
                    titles=[_title(1, seconds=1500), _title(2, seconds=1490)])
    _use(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "MakeMKV", _fake_makemkv({"disc:0": (disc, [])}))

    asyncio.run(cli._cmd_scan(_ns(source="disc:0")))

    assert disc_fingerprint(disc) in _flat(capsys.readouterr())


def test_a_title_of_unknown_size_is_shown_as_unknown_not_as_zero(
        tmp_path, monkeypatch, capsys):
    """MakeMKV reports no size for some playlists. Rendering that as '0.0 GB'
    reads as a broken title and gets deselected; '?' reads as 'not reported'."""
    _use(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "MakeMKV", _fake_makemkv({
        "disc:0": (DiscInfo(source="disc:0", disc_name="D",
                            titles=[_title(1, size=0), _title(2, size=8_000_000_000)]), []),
    }))

    asyncio.run(cli._cmd_scan(_ns(source="disc:0")))

    out = _flat(capsys.readouterr())
    assert "0.0 GB" not in out
    assert "8.0 GB" in out
    assert "?" in out


# --------------------------------------------------------------------------
# rip
# --------------------------------------------------------------------------


def _fake_pipeline(*, run=None, prepare=None, execute=None, resubtitle=None, seen=None):
    class _FakePipeline:
        def __init__(self, settings, callbacks=None):
            self.settings = settings
            self.cb = callbacks
            if seen is not None:
                seen.append(self)

        async def run(self, source, job_name=None):
            return await run(self, source, job_name)

        async def prepare(self, source):
            return await prepare(self, source)

        async def execute(self, job, scheduler, report, job_name=None):
            return await execute(self, job, scheduler, report, job_name)

        async def resubtitle(self, video, report, source_mkv=None):
            return await resubtitle(self, video, report, source_mkv)

    return _FakePipeline


def test_rip_lists_every_file_it_placed(tmp_path, monkeypatch, capsys):
    """A rip of a TV disc places several files. Printing only the count leaves
    the user unable to tell WHICH episodes landed where without going to the
    library — and a mis-numbered episode is exactly what you want to catch."""
    _use(monkeypatch, _settings(tmp_path))
    job = Job(source="disc:0", results=[
        TitleResult(title_id=1, video_path=tmp_path / "S01E01.mkv"),
        TitleResult(title_id=2, video_path=tmp_path / "S01E02.mkv"),
    ])

    async def _run(self, source, job_name):
        return job

    monkeypatch.setattr(cli, "Pipeline", _fake_pipeline(run=_run))
    rc = asyncio.run(cli._cmd_rip(_ns(source="disc:0", name=None)))

    out = _flat(capsys.readouterr())
    assert rc == 0
    assert "2 file(s)" in out
    assert "S01E01.mkv" in out and "S01E02.mkv" in out


def test_rip_uses_the_job_name_for_the_staging_folder(tmp_path, monkeypatch, capsys):
    """--name is how two discs ripped in the same session keep separate staging
    folders. Dropping it collides them, and the second rip overwrites the first
    rip's .mkv before it has been encoded."""
    _use(monkeypatch, _settings(tmp_path))
    got = {}

    async def _run(self, source, job_name):
        got["name"] = job_name
        return Job(source=source)

    monkeypatch.setattr(cli, "Pipeline", _fake_pipeline(run=_run))
    asyncio.run(cli._cmd_rip(_ns(source="disc:0", name="thething-1982")))
    capsys.readouterr()

    assert got["name"] == "thething-1982"

    asyncio.run(cli._cmd_rip(_ns(source="disc:0", name=None)))
    capsys.readouterr()
    assert got["name"] is None


def test_rip_warns_when_naming_will_be_manual(tmp_path, monkeypatch, capsys):
    """Without a TMDB key every title has to be typed by hand. Learning that at
    the first prompt — after the disc has already been scanned — is the wrong
    time; the warning is what makes it a decision instead of a surprise."""
    async def _run(self, source, job_name):
        return Job(source=source)

    monkeypatch.setattr(cli, "Pipeline", _fake_pipeline(run=_run))

    _use(monkeypatch, _settings(tmp_path, tmdb_api_key=None))
    asyncio.run(cli._cmd_rip(_ns(source="disc:0", name=None)))
    without = _flat(capsys.readouterr())

    _use(monkeypatch, _settings(tmp_path, tmdb_api_key="abc"))
    asyncio.run(cli._cmd_rip(_ns(source="disc:0", name=None)))
    with_key = _flat(capsys.readouterr())

    assert "No TMDB key" in without
    assert "No TMDB key" not in with_key


def test_a_failed_rip_still_tears_down_the_live_progress_bar(
        tmp_path, monkeypatch, capsys):
    """rich's Live display owns the terminal until it is stopped. If a rip
    raises and the bar is left running, the traceback is drawn over by the bar
    and the shell is left with a hidden cursor — the error becomes unreadable
    at the exact moment it matters."""
    _use(monkeypatch, _settings(tmp_path))
    made = []

    class _Recording(cli.CliCallbacks):
        def __init__(self, settings):
            super().__init__(settings)
            made.append(self)

    monkeypatch.setattr(cli, "CliCallbacks", _Recording)

    async def _run(self, source, job_name):
        self.cb.on_progress("ripping title 1", 0.25)   # starts the Live display
        raise RuntimeError("drive dropped off the bus")

    monkeypatch.setattr(cli, "Pipeline", _fake_pipeline(run=_run))

    with pytest.raises(RuntimeError):
        asyncio.run(cli._cmd_rip(_ns(source="disc:0", name=None)))
    capsys.readouterr()

    assert made and made[0]._progress is None, "the progress display was left running"


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------


def _queue_settings(tmp_path, **over):
    s = _settings(tmp_path, **over)
    s.queue.encode_capacity = 3
    return s


def test_queue_skips_a_disc_that_fails_setup_and_runs_the_rest(
        tmp_path, monkeypatch, capsys):
    """One unreadable disc out of four must not cost the other three their
    (already given) title selections. The name of the skipped source and the
    reason are the whole record of what did not happen."""
    _use(monkeypatch, _queue_settings(tmp_path))

    async def _prepare(self, source):
        if source == "disc:9":
            raise RuntimeError("no disc in drive")
        return Job(source=source, disc=DiscInfo(source=source, disc_name="OK_DISC"))

    async def _execute(self, job, scheduler, report, job_name=None):
        job.results = [TitleResult(title_id=1, video_path=tmp_path / "Movie.mkv")]
        return job

    monkeypatch.setattr(cli, "Pipeline",
                        _fake_pipeline(prepare=_prepare, execute=_execute))
    rc = asyncio.run(cli._cmd_queue(_ns(sources=["disc:9", "disc:0"])))

    out = _flat(capsys.readouterr())
    assert "Skipping disc:9" in out
    assert "no disc in drive" in out
    assert "Movie.mkv" in out            # the good disc still ran
    # The skip is a failure of the run, not a smaller run: exiting 0 here let a
    # batch report success having ripped one disc of two.
    assert rc == 1
    assert "1/2 succeeded" in out


def test_queue_exits_nonzero_when_no_disc_could_be_set_up(
        tmp_path, monkeypatch, capsys):
    """Every source failing means nothing was queued at all. Exiting 0 there
    would let a nightly batch report success having ripped nothing."""
    _use(monkeypatch, _queue_settings(tmp_path))

    async def _prepare(self, source):
        raise RuntimeError("drive not ready")

    monkeypatch.setattr(cli, "Pipeline", _fake_pipeline(prepare=_prepare))
    rc = asyncio.run(cli._cmd_queue(_ns(sources=["disc:0", "disc:1"])))

    assert rc == 1
    assert "Nothing to do" in _flat(capsys.readouterr())


def test_queue_exits_nonzero_when_a_job_fails_mid_run(tmp_path, monkeypatch, capsys):
    """A job that dies during encode must not take the other lanes down with it,
    but the run as a whole did not succeed and has to say so."""
    _use(monkeypatch, _queue_settings(tmp_path))

    async def _prepare(self, source):
        return Job(source=source)

    async def _execute(self, job, scheduler, report, job_name=None):
        if job.source == "disc:1":
            raise RuntimeError("encode failed")
        job.results = [TitleResult(title_id=1, video_path=tmp_path / "Good.mkv")]
        return job

    monkeypatch.setattr(cli, "Pipeline",
                        _fake_pipeline(prepare=_prepare, execute=_execute))
    rc = asyncio.run(cli._cmd_queue(_ns(sources=["disc:0", "disc:1"])))

    out = _flat(capsys.readouterr())
    assert rc == 1
    assert "1/2 succeeded" in out
    assert "Good.mkv" in out             # the surviving lane still placed its file


def test_queue_runs_the_discs_concurrently(tmp_path, monkeypatch, capsys):
    """Running the queue one disc after another would make it identical to
    looping `rip`, and would idle the second drive for the whole first encode.
    Concurrency plus the shared scheduler is the entire feature."""
    _use(monkeypatch, _queue_settings(tmp_path))
    order = []

    async def _prepare(self, source):
        return Job(source=source)

    async def _execute(self, job, scheduler, report, job_name=None):
        order.append(("start", job.source))
        if job.source == "disc:0":
            for _ in range(500):         # yield until the other lane starts
                if ("start", "disc:1") in order:
                    break
                await asyncio.sleep(0)
        order.append(("done", job.source))
        return job

    monkeypatch.setattr(cli, "Pipeline",
                        _fake_pipeline(prepare=_prepare, execute=_execute))
    asyncio.run(cli._cmd_queue(_ns(sources=["disc:0", "disc:1"])))
    capsys.readouterr()

    assert order.index(("start", "disc:1")) < order.index(("done", "disc:0"))


def test_every_queued_job_shares_one_encode_budget(tmp_path, monkeypatch, capsys):
    """The budget is what stops two UHD encodes landing on the same CPU. A
    per-job scheduler would enforce nothing at all while looking like it did."""
    _use(monkeypatch, _queue_settings(tmp_path))
    schedulers = []

    async def _prepare(self, source):
        return Job(source=source)

    async def _execute(self, job, scheduler, report, job_name=None):
        schedulers.append(scheduler)
        return job

    monkeypatch.setattr(cli, "Pipeline",
                        _fake_pipeline(prepare=_prepare, execute=_execute))
    asyncio.run(cli._cmd_queue(_ns(sources=["disc:0", "disc:1"])))
    capsys.readouterr()

    assert len(schedulers) == 2
    assert schedulers[0] is schedulers[1]
    assert schedulers[0].capacity == 3


def test_a_queue_lane_is_labelled_with_the_disc_not_the_selector(
        tmp_path, monkeypatch, capsys):
    """Four bars all reading 'disc:0'..'disc:3' tell you nothing about which
    film is which. The disc label does — and a scan that produced no label has
    to fall back to something rather than an empty lane."""
    _use(monkeypatch, _queue_settings(tmp_path))
    lanes = {}

    async def _prepare(self, source):
        if source == "disc:0":
            return Job(source=source, disc=DiscInfo(
                source=source,
                disc_name="THE_LORD_OF_THE_RINGS_FELLOWSHIP_EXTENDED"))
        return Job(source=source, disc=None)

    async def _execute(self, job, scheduler, report, job_name=None):
        lanes[job.source] = report.name
        return job

    monkeypatch.setattr(cli, "Pipeline",
                        _fake_pipeline(prepare=_prepare, execute=_execute))
    asyncio.run(cli._cmd_queue(_ns(sources=["disc:0", "disc:1"])))
    capsys.readouterr()

    assert lanes["disc:0"] == "THE_LORD_OF_THE_RINGS_FEL"[:24]
    assert lanes["disc:1"] == "disc:1"


def test_a_queue_lane_forwards_job_events_to_the_console(
        tmp_path, monkeypatch, capsys):
    """With several jobs interleaved, an unattributed log line is unusable. Each
    line has to carry the lane it came from."""
    _use(monkeypatch, _queue_settings(tmp_path))

    async def _prepare(self, source):
        return Job(source=source, disc=DiscInfo(source=source, disc_name="TRON"))

    async def _execute(self, job, scheduler, report, job_name=None):
        report.on_event("subtitle OCR finished")
        return job

    monkeypatch.setattr(cli, "Pipeline",
                        _fake_pipeline(prepare=_prepare, execute=_execute))
    asyncio.run(cli._cmd_queue(_ns(sources=["disc:0"])))

    out = _flat(capsys.readouterr())
    assert "[TRON] subtitle OCR finished" in out


def test_each_queue_lane_tracks_its_own_job(tmp_path, capsys):
    """Two discs run at once, and each bar has to belong to one of them. Sharing
    a task would make both bars jump to whichever job reported last.

    The None-fraction case is the one that bites: 'waiting for an encode slot'
    has no percentage, and resetting the bar to zero there makes a job that is
    90%% done look like it restarted."""
    display = cli._QueueDisplay()
    display.start()
    try:
        tron = display.lane("TRON")
        alien = display.lane("ALIEN")
        tron.on_progress("encoding", 0.4)
        alien.on_progress("ripping title 1", 0.1)
        tron.on_progress("waiting for an encode slot", None)
        tasks = {t.fields["lane"]: t for t in display.progress.tasks}
    finally:
        display.stop()
    capsys.readouterr()

    assert tasks["TRON"].completed == 400        # 0.4 of the 1000-unit bar
    assert tasks["TRON"].description == "waiting for an encode slot"
    assert tasks["ALIEN"].completed == 100       # unmoved by TRON's updates
    assert tasks["ALIEN"].description == "ripping title 1"


# --------------------------------------------------------------------------
# coverart
# --------------------------------------------------------------------------


def _disc_root(tmp_path, *, portrait=None, landscape=None, title=None):
    meta = tmp_path / "DISCROOT" / "BDMV" / "META" / "DL"
    meta.mkdir(parents=True)
    if portrait:
        _png(meta / "poster_src.png", *portrait)
    if landscape:
        _png(meta / "thumb_src.png", *landscape)
    if title:
        (meta / "bdmt_eng.xml").write_text(
            f'<?xml version="1.0" encoding="UTF-8"?><di:disclib xmlns:di="urn:x">'
            f'<di:name>{title}</di:name></di:disclib>', encoding="utf-8")
    return meta.parent.parent.parent


def test_coverart_strips_the_file_selector_before_reading_the_disc(
        tmp_path, monkeypatch, capsys):
    """`file:` is makemkvcon's prefix, and it is what the rest of the app passes
    around as a source. Handing it to the filesystem verbatim resolves nothing,
    so every `coverart file:...` would report an unreadable disc."""
    root = _disc_root(tmp_path, portrait=(600, 900), title="Blade Runner")
    _use(monkeypatch, _settings(tmp_path))
    out_dir = tmp_path / "art"

    rc = asyncio.run(cli._cmd_coverart(
        _ns(source=f"file:{root}", out=str(out_dir))))

    out = _flat(capsys.readouterr())
    assert rc == 0
    assert (out_dir / "poster.png").exists()
    assert "Blade Runner" in out


def test_coverart_saves_a_landscape_only_disc_as_a_backdrop(
        tmp_path, monkeypatch, capsys):
    """Most Blu-ray disc thumbnails are landscape. Writing one as poster.jpg
    gives Plex a squashed, wrong-shaped poster it will then keep; saving it as
    fanart lets Plex fetch a real poster from its agent. The note is what stops
    the user filing it as 'no poster extracted'."""
    root = _disc_root(tmp_path, landscape=(1920, 1080))
    _use(monkeypatch, _settings(tmp_path))
    out_dir = tmp_path / "art"

    rc = asyncio.run(cli._cmd_coverart(_ns(source=str(root), out=str(out_dir))))

    out = _flat(capsys.readouterr())
    assert rc == 0
    assert (out_dir / "fanart.png").exists()
    assert not (out_dir / "poster.png").exists()
    assert "fetch the poster" in out


def test_a_disc_without_cover_art_is_not_reported_as_an_error(
        tmp_path, monkeypatch, capsys):
    """No art on the disc is a fact about the disc, not a failure to resolve it.
    The two need different exit codes or a script can't tell 'nothing to copy'
    from 'you gave me a path that isn't a disc'."""
    root = _disc_root(tmp_path)     # BDMV/META/DL exists but is empty
    _use(monkeypatch, _settings(tmp_path))

    rc = asyncio.run(cli._cmd_coverart(_ns(source=str(root), out=str(tmp_path / "art"))))

    out = _flat(capsys.readouterr())
    assert rc == 1
    assert "No cover art found" in out


def test_coverart_rejects_a_path_that_is_not_there(tmp_path, monkeypatch, capsys):
    """A mistyped path (or a drive that isn't mapped in this session) has to
    fail loudly — silently writing nothing and exiting 0 is how a whole batch
    ends up with no artwork."""
    _use(monkeypatch, _settings(tmp_path))

    rc = asyncio.run(cli._cmd_coverart(
        _ns(source=str(tmp_path / "no-such-disc"), out=str(tmp_path / "art"))))

    assert rc == 2
    assert "Could not resolve" in _flat(capsys.readouterr())


def test_a_malformed_disc_selector_never_reaches_the_drive(
        tmp_path, monkeypatch, capsys):
    """`disc:` takes an index. Enumerating drives to answer a selector that
    cannot name one takes up to a minute of makemkvcon probing (and fights a
    rip in progress) to reach the same answer."""
    _use(monkeypatch, _settings(tmp_path))
    probed = []

    class _FakeMakeMKV:
        def __init__(self, settings):
            pass

        async def list_drives(self):
            probed.append(True)
            return [Drive(index=0, name="BD", device="/dev/sr0", disc_name="X")]

    monkeypatch.setattr("blurayripper.tools.makemkv.MakeMKV", _FakeMakeMKV)

    rc_bad = asyncio.run(cli._cmd_coverart(_ns(source="disc:one", out=str(tmp_path))))
    bad_out = _flat(capsys.readouterr())
    rc_ok = asyncio.run(cli._cmd_coverart(_ns(source="disc:0", out=str(tmp_path))))
    capsys.readouterr()

    assert rc_bad == 2 and "Bad disc selector" in bad_out
    assert probed == [True], "a well-formed selector should enumerate drives"
    assert rc_ok == 2       # /dev/sr0 is not a readable disc root; different message
    assert "Bad disc selector" not in _flat(capsys.readouterr())


def test_coverart_reports_a_disc_index_no_drive_answers_to(
        tmp_path, monkeypatch, capsys):
    """`disc:3` on a two-drive box is a real mistake people make when a drive
    index shifts after a reboot."""
    _use(monkeypatch, _settings(tmp_path))

    class _FakeMakeMKV:
        def __init__(self, settings):
            pass

        async def list_drives(self):
            return [Drive(index=0, name="BD", device="F:", disc_name="X")]

    monkeypatch.setattr("blurayripper.tools.makemkv.MakeMKV", _FakeMakeMKV)

    rc = asyncio.run(cli._cmd_coverart(_ns(source="disc:3", out=str(tmp_path))))

    assert rc == 2
    assert "Could not resolve" in _flat(capsys.readouterr())


# --------------------------------------------------------------------------
# resub
# --------------------------------------------------------------------------


def _resub_pipeline(monkeypatch, **kw):
    monkeypatch.setattr("blurayripper.pipeline.Pipeline", _fake_pipeline(**kw))


def test_resub_checks_the_source_mkv_before_doing_any_work(
        tmp_path, monkeypatch, capsys):
    """The source .mkv is the multi-GB rip. Pointing at one that was already
    cleaned out of staging must fail immediately, not after the pipeline has
    spun up mkvextract and SubtitleEdit against nothing."""
    _use(monkeypatch, _settings(tmp_path))
    ran = []

    async def _resubtitle(self, video, report, source_mkv):
        ran.append(source_mkv)
        return []

    _resub_pipeline(monkeypatch, resubtitle=_resubtitle)
    video = tmp_path / "Movie.mkv"
    video.write_text("v")

    rc = asyncio.run(cli._cmd_resub(
        _ns(video=str(video), source=str(tmp_path / "gone.mkv"))))

    out = _flat(capsys.readouterr())
    assert rc == 2
    assert "Source MKV not found" in out
    assert ran == [], "the pipeline should not have been started"


def test_resub_writes_sidecars_even_when_the_library_video_is_absent(
        tmp_path, monkeypatch, capsys):
    """The .srt sits NEXT TO the video, so the sidecars are still correct and
    useful when the video is on a NAS that isn't mapped right now (or is about
    to be copied in). Refusing here would block the common repair."""
    _use(monkeypatch, _settings(tmp_path))
    placed = [tmp_path / "Movie.eng.srt"]

    async def _resubtitle(self, video, report, source_mkv):
        return placed

    _resub_pipeline(monkeypatch, resubtitle=_resubtitle)

    rc = asyncio.run(cli._cmd_resub(
        _ns(video=str(tmp_path / "Movie.mkv"), source=None)))

    out = _flat(capsys.readouterr())
    assert rc == 0
    assert "not found" in out            # warned, but did not refuse
    assert "Movie.eng.srt" in out


def test_resub_producing_nothing_is_a_failure(tmp_path, monkeypatch, capsys):
    """'Re-OCR produced no subtitles' is the exact condition the user ran this
    to fix. Exiting 0 would report the repair as done."""
    _use(monkeypatch, _settings(tmp_path))

    async def _resubtitle(self, video, report, source_mkv):
        return []

    _resub_pipeline(monkeypatch, resubtitle=_resubtitle)
    video = tmp_path / "Movie.mkv"
    video.write_text("v")

    rc = asyncio.run(cli._cmd_resub(_ns(video=str(video), source=None)))

    assert rc == 1
    assert "No subtitle sidecars" in _flat(capsys.readouterr())


@pytest.mark.parametrize("error, fragment", [
    (FileNotFoundError("no cached subtitles for this job"),
     "no cached subtitles for this job"),
    (ToolNotFoundError("subtitleedit"), "Required tool 'subtitleedit'"),
])
def test_resub_separates_a_broken_setup_from_an_empty_result(
        tmp_path, monkeypatch, capsys, error, fragment):
    """Missing cached subs and a missing OCR tool are both 'go fix something',
    which is a different action from 'this disc had no subtitles' — and 2 vs 1
    is how a wrapper script tells them apart. The cause has to survive into the
    message, or 2 only says 'something in your setup'."""
    _use(monkeypatch, _settings(tmp_path))

    async def _resubtitle(self, video, report, source_mkv):
        raise error

    _resub_pipeline(monkeypatch, resubtitle=_resubtitle)
    video = tmp_path / "Movie.mkv"
    video.write_text("v")

    rc = asyncio.run(cli._cmd_resub(_ns(video=str(video), source=None)))

    assert rc == 2
    assert fragment in _flat(capsys.readouterr())


def test_resub_says_whether_it_is_using_the_rip_or_the_cache(
        tmp_path, monkeypatch, capsys):
    """These are different repairs with different outcomes: the cached .sup subs
    may be the very thing that is missing, in which case re-running against the
    source .mkv is the fix. The header is what tells you which one just ran."""
    _use(monkeypatch, _settings(tmp_path))
    got = []

    async def _resubtitle(self, video, report, source_mkv):
        got.append(source_mkv)
        return [tmp_path / "Movie.eng.srt"]

    _resub_pipeline(monkeypatch, resubtitle=_resubtitle)
    video = tmp_path / "Movie.mkv"
    video.write_text("v")
    source = tmp_path / "title_t00.mkv"
    source.write_text("m")

    asyncio.run(cli._cmd_resub(_ns(video=str(video), source=str(source))))
    with_source = _flat(capsys.readouterr())
    asyncio.run(cli._cmd_resub(_ns(video=str(video), source=None)))
    cached = _flat(capsys.readouterr())

    assert "from title_t00.mkv" in with_source
    assert "from cached subs" in cached
    assert got[0] is not None and got[0].name == "title_t00.mkv"
    assert got[1] is None


def test_resub_echoes_the_pipelines_progress_lines(tmp_path, monkeypatch, capsys):
    """OCR of a feature's subtitle track takes minutes with no other output. A
    silent console is indistinguishable from a hang, and gets killed."""
    _use(monkeypatch, _settings(tmp_path))

    async def _resubtitle(self, video, report, source_mkv):
        report.on_event("Extracting PGS track 3 (eng)")
        report.on_progress("ocr", 0.5)
        report.checkpoint()
        return [tmp_path / "Movie.eng.srt"]

    _resub_pipeline(monkeypatch, resubtitle=_resubtitle)
    video = tmp_path / "Movie.mkv"
    video.write_text("v")

    rc = asyncio.run(cli._cmd_resub(_ns(video=str(video), source=None)))

    assert rc == 0
    assert "Extracting PGS track 3 (eng)" in _flat(capsys.readouterr())


# --------------------------------------------------------------------------
# eject / close
# --------------------------------------------------------------------------


def test_eject_exit_code_distinguishes_a_moved_tray_from_a_stuck_one(
        tmp_path, monkeypatch, capsys):
    """This command exists to diagnose auto-eject. Its answer is the exit code:
    a batch that swaps discs cannot see the console text."""
    calls = []

    def _ok(device, method):
        calls.append((device, method))

    def _boom(device, method):
        raise OSError("ioctl failed: 5; mci failed: 266")

    monkeypatch.setattr("blurayripper.tools.eject.eject", _ok)
    good = asyncio.run(cli._cmd_eject(_ns(drive="F:", method="auto")))
    good_out = _flat(capsys.readouterr())

    monkeypatch.setattr("blurayripper.tools.eject.eject", _boom)
    bad = asyncio.run(cli._cmd_eject(_ns(drive="F:", method="auto")))
    bad_out = _flat(capsys.readouterr())

    assert good == 0 and "succeeded" in good_out
    assert bad == 1 and "Eject failed" in bad_out
    assert "ioctl failed: 5" in bad_out       # each method's own error survives
    assert "Tip:" in bad_out and "Tip:" not in good_out


def test_eject_forces_the_method_the_user_asked_for(tmp_path, monkeypatch, capsys):
    """--method is the entire diagnostic: you run it four times to find which of
    scsi/ioctl/shell/mci opens your drive. Falling back to 'auto' would make all
    four runs identical and the flag a lie."""
    calls = []
    monkeypatch.setattr("blurayripper.tools.eject.eject",
                        lambda device, method: calls.append((device, method)))

    asyncio.run(cli._cmd_eject(_ns(drive="F:", method="scsi")))
    out = _flat(capsys.readouterr())

    assert calls == [("F:", "scsi")]
    assert "method: scsi" in out


def test_eject_without_a_drive_uses_the_default_and_says_so(
        tmp_path, monkeypatch, capsys):
    """On Linux/macOS there is usually one optical drive and no letter to give.
    The message has to name what it acted on, or a multi-drive box can't tell
    whether the right tray was addressed."""
    calls = []
    monkeypatch.setattr("blurayripper.tools.eject.eject",
                        lambda device, method: calls.append((device, method)))

    rc = asyncio.run(cli._cmd_eject(_ns(drive=None, method="auto")))
    out = _flat(capsys.readouterr())

    assert rc == 0
    assert calls == [(None, "auto")]
    assert "Ejecting default drive" in out
    assert "succeeded for default drive" in out
    assert "None" not in out


def test_close_tray_reports_failure_with_its_own_exit_code(
        tmp_path, monkeypatch, capsys):
    """Closing the tray is what an unattended rip station does between discs. A
    close that silently failed leaves the next rip scanning an empty drive."""
    monkeypatch.setattr("blurayripper.tools.eject.close_tray",
                        lambda device, method: None)
    good = asyncio.run(cli._cmd_close(_ns(drive="F:", method="auto")))
    good_out = _flat(capsys.readouterr())

    def _boom(device, method):
        raise OSError("no such drive")

    monkeypatch.setattr("blurayripper.tools.eject.close_tray", _boom)
    bad = asyncio.run(cli._cmd_close(_ns(drive="F:", method="auto")))
    bad_out = _flat(capsys.readouterr())

    assert good == 0 and "succeeded" in good_out
    assert bad == 1 and "Close tray failed" in bad_out
    assert "no such drive" in bad_out


def test_close_forces_the_method_the_user_asked_for(tmp_path, monkeypatch, capsys):
    """Same diagnostic as eject: scsi vs ioctl vs mci is exactly what is being
    narrowed down."""
    calls = []
    monkeypatch.setattr("blurayripper.tools.eject.close_tray",
                        lambda device, method: calls.append((device, method)))

    asyncio.run(cli._cmd_close(_ns(drive=None, method="mci")))

    assert calls == [(None, "mci")]
    assert "method: mci" in _flat(capsys.readouterr())


# --------------------------------------------------------------------------
# verify-subs
# --------------------------------------------------------------------------


def _done_record(job_id, video, *, sub_langs=("eng",), name="The Abyss"):
    disc = DiscInfo(source=f"disc:{job_id}", disc_name=name,
                    titles=[_title(1, subs=sub_langs)])
    return JobRecord(id=job_id, source=f"disc:{job_id}", state="done",
                     disc=disc_to_dict(disc), selected_title_ids=[1],
                     outputs=[str(video)])


def _store_with(monkeypatch, records, store=None):
    store = store or MemoryStore()
    for rec in records:
        store.save(rec)
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)
    return store


def test_verify_subs_flags_a_movie_whose_subtitles_never_landed(
        tmp_path, monkeypatch, capsys):
    """This is the silent failure the command was written for: the job went
    green, the video is in the library, and the subtitle track the disc carried
    is simply absent. Nothing else in the system notices."""
    _use(monkeypatch, _settings(tmp_path))
    video = tmp_path / "The Abyss (1989).mkv"
    video.write_text("v")
    _store_with(monkeypatch, [_done_record(1, video)])

    rc = asyncio.run(cli._cmd_verify_subs(_ns(all=False)))

    out = _flat(capsys.readouterr())
    assert rc == 1
    assert "missing" in out
    assert "The Abyss" in out


def test_verify_subs_is_quiet_when_every_sidecar_is_present(
        tmp_path, monkeypatch, capsys):
    """A clean audit must exit 0 and list nothing, or it can't be run from cron.
    Only the sidecar differs from the failing case above."""
    _use(monkeypatch, _settings(tmp_path))
    video = tmp_path / "The Abyss (1989).mkv"
    video.write_text("v")
    (tmp_path / "The Abyss (1989).eng.srt").write_text("1\n")
    _store_with(monkeypatch, [_done_record(1, video)])

    rc = asyncio.run(cli._cmd_verify_subs(_ns(all=False)))

    out = _flat(capsys.readouterr())
    assert rc == 0
    assert "1 OK" in out
    assert "Subtitle audit" not in out       # no table when there is nothing wrong


def test_an_unreachable_library_is_not_reported_as_missing_subtitles(
        tmp_path, monkeypatch, capsys):
    """Running the audit on a box where the NAS isn't mapped can see nothing.
    Calling that 'missing' would send someone re-ripping discs that are fine —
    so it is counted separately and does NOT fail the run."""
    _use(monkeypatch, _settings(tmp_path))
    unreachable = tmp_path / "never-mapped" / "The Abyss (1989).mkv"
    _store_with(monkeypatch, [_done_record(1, unreachable)])

    rc = asyncio.run(cli._cmd_verify_subs(_ns(all=False)))

    out = _flat(capsys.readouterr())
    assert rc == 0
    assert "1 unverifiable" in out
    assert "run this on the box" in out
    assert "0 missing subtitles" in out


def test_verify_subs_all_lists_the_jobs_that_are_fine_too(
        tmp_path, monkeypatch, capsys):
    """Without --all the healthy jobs are invisible, so you cannot tell an audit
    that found nothing wrong from one that examined nothing at all."""
    _use(monkeypatch, _settings(tmp_path))
    good = tmp_path / "Contact (1997).mkv"
    good.write_text("v")
    (tmp_path / "Contact (1997).eng.srt").write_text("1\n")
    _store_with(monkeypatch, [_done_record(1, good, name="Contact")])

    asyncio.run(cli._cmd_verify_subs(_ns(all=False)))
    default_out = _flat(capsys.readouterr())
    asyncio.run(cli._cmd_verify_subs(_ns(all=True)))
    all_out = _flat(capsys.readouterr())

    assert "Contact" not in default_out
    assert "Contact" in all_out


def test_a_disc_that_carried_no_kept_subtitles_is_not_flagged(
        tmp_path, monkeypatch, capsys):
    """Plenty of discs have no English subtitle track. Demanding a sidecar there
    would make the audit permanently red, which makes it useless."""
    _use(monkeypatch, _settings(tmp_path))
    video = tmp_path / "Foreign Film.mkv"
    video.write_text("v")
    _store_with(monkeypatch, [_done_record(1, video, sub_langs=("jpn",))])

    rc = asyncio.run(cli._cmd_verify_subs(_ns(all=False)))

    out = _flat(capsys.readouterr())
    assert rc == 0
    assert "0 missing subtitles" in out


def test_verify_subs_exits_2_when_the_job_store_cannot_be_read(
        tmp_path, monkeypatch, capsys):
    """Postgres being down means the audit examined nothing. Reporting '0
    missing' there would be a false all-clear, and 2 keeps it distinct from the
    'found problems' exit of 1."""
    _use(monkeypatch, _settings(tmp_path))

    class _BrokenStore(MemoryStore):
        def init(self):
            raise RuntimeError("could not connect to server")

    _store_with(monkeypatch, [], store=_BrokenStore())

    rc = asyncio.run(cli._cmd_verify_subs(_ns(all=False)))

    out = _flat(capsys.readouterr())
    assert rc == 2
    assert "Can't read the job store" in out
    assert "could not connect to server" in out


# --------------------------------------------------------------------------
# main(): argv -> exit code
# --------------------------------------------------------------------------


def test_a_missing_tool_exits_2_rather_than_traceback(tmp_path, monkeypatch, capsys):
    """makemkvcon not being installed/configured is a setup problem, and the one
    thing the user must be told plainly. A traceback out of asyncio.run buries
    it, and the shell would report 1 — the same code a real disc failure uses."""
    _use(monkeypatch, _settings(tmp_path))
    fail = {"raise": True}

    class _FakeMakeMKV:
        def __init__(self, settings):
            pass

        async def scan(self, source, *, on_message=None):
            if fail["raise"]:
                raise ToolNotFoundError("makemkvcon")
            return DiscInfo(source=source, disc_name="D", titles=[_title(1)])

    monkeypatch.setattr(cli, "MakeMKV", _FakeMakeMKV)

    rc_missing = cli.main(["scan", "disc:0"])
    out = _flat(capsys.readouterr())
    fail["raise"] = False
    rc_present = cli.main(["scan", "disc:0"])
    capsys.readouterr()

    assert rc_missing == 2
    assert "Required tool 'makemkvcon' was not found" in out
    assert "config/settings.toml" in out
    assert rc_present == 0            # the tool was the only difference


def test_ctrl_c_exits_130_like_any_other_interrupted_command(
        tmp_path, monkeypatch, capsys):
    """130 is what a shell expects from SIGINT, and it is what a rip-loop script
    checks to stop the loop instead of moving on to the next disc."""
    _use(monkeypatch, _settings(tmp_path))

    class _FakeMakeMKV:
        def __init__(self, settings):
            pass

        async def scan(self, source, *, on_message=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "MakeMKV", _FakeMakeMKV)

    rc = cli.main(["scan", "disc:0"])

    assert rc == 130
    assert "Interrupted" in _flat(capsys.readouterr())


def test_the_config_flag_reaches_the_settings_loader_as_a_path(
        tmp_path, monkeypatch, capsys):
    """--config is how a second worker on the same box gets its own settings.
    load_settings joins filenames onto it, so a bare string would raise deep in
    config loading rather than being usable at all."""
    from pathlib import Path

    seen = []

    def _load(config_dir=None, *a, **k):
        seen.append(config_dir)
        return _settings(tmp_path)

    monkeypatch.setattr(cli, "load_settings", _load)

    class _FakeMakeMKV:
        def __init__(self, settings):
            pass

        async def scan(self, source, *, on_message=None):
            return DiscInfo(source=source, disc_name="D", titles=[_title(1)])

    monkeypatch.setattr(cli, "MakeMKV", _FakeMakeMKV)

    cli.main(["--config", str(tmp_path / "alt"), "scan", "disc:0"])
    cli.main(["scan", "disc:0"])
    capsys.readouterr()

    assert seen[0] == Path(tmp_path / "alt")
    assert isinstance(seen[0], Path)
    assert seen[1] is None
