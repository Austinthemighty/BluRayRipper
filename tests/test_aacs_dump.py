"""Keeping the AACS dump MakeMKV writes for a disc whose key it doesn't know."""

import time

from blurayripper.services import aacs_dump


def test_reads_the_app_datadir_override(tmp_path):
    """MakeMKV's data dir is configurable, so 'it's in the data directory' isn't
    enough to find the dump — the override has to be honoured."""
    conf = tmp_path / "settings.conf"
    conf.write_text('app_Key = "x"\napp_DataDir = "/mnt/TrueNas/mkv-data"\n')
    assert aacs_dump.data_dir(conf) == aacs_dump.Path("/mnt/TrueNas/mkv-data")


def test_falls_back_to_the_default_when_unset_or_unreadable(tmp_path):
    conf = tmp_path / "settings.conf"
    conf.write_text('app_Key = "x"\n')
    assert aacs_dump.data_dir(conf) == aacs_dump.default_data_dir()
    assert aacs_dump.data_dir(tmp_path / "nope.conf") == aacs_dump.default_data_dir()


def test_finds_only_a_dump_from_this_scan(tmp_path):
    """A dump left by a PREVIOUS disc must not be attributed to this one."""
    old = tmp_path / "old.tgz"
    old.write_bytes(b"old")
    import os
    os.utime(old, (time.time() - 3600, time.time() - 3600))

    started = time.time()
    assert aacs_dump.find_new_dump(tmp_path, started) is None   # only the old one

    new = tmp_path / "new.tgz"
    new.write_bytes(b"new")
    assert aacs_dump.find_new_dump(tmp_path, started) == new


def test_no_dumps_at_all_is_not_an_error(tmp_path):
    assert aacs_dump.find_new_dump(tmp_path, time.time()) is None
    assert aacs_dump.find_new_dump(tmp_path / "missing", time.time()) is None


def test_collect_copies_and_names_after_the_disc(tmp_path):
    """Copied, not moved: a failed copy must not destroy the only record of an
    unreadable disc, and MakeMKV may still want its own."""
    src = tmp_path / "dump.tgz"
    src.write_bytes(b"payload")
    dest_dir = tmp_path / "keep"

    got = aacs_dump.collect(src, dest_dir, "SOME UHD: Movie/2024")

    assert got.exists() and got.read_bytes() == b"payload"
    assert src.exists()                                   # original untouched
    assert "/" not in got.name and ":" not in got.name    # path-unsafe chars stripped
    assert got.name.endswith("dump.tgz")


def test_collect_without_a_disc_name(tmp_path):
    src = tmp_path / "abc123.tgz"
    src.write_bytes(b"x")
    got = aacs_dump.collect(src, tmp_path / "keep")
    assert got.exists() and "abc123" in got.name


def test_advice_names_the_file_and_says_nothing_was_sent(tmp_path):
    msg = aacs_dump.advice(tmp_path / "keep" / "x.tgz")
    assert "x.tgz" in msg
    assert aacs_dump.SUBMIT_TO in msg
    assert "Nothing has been sent" in msg     # the app never emails on your behalf


async def test_a_failed_scan_keeps_the_dump_and_says_where(tmp_path, monkeypatch):
    """End-to-end: an unknown-key disc fails to open, and the .tgz MakeMKV wrote
    into its own data dir is copied somewhere findable with submission advice."""
    import time as _time

    from blurayripper.config import load_settings
    from blurayripper.pipeline import JobState
    from blurayripper.services import aacs_dump as ad
    from blurayripper.web import manager as mgr
    from blurayripper.web.manager import JobManager

    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "staging")
    m = JobManager(settings)

    mkv_data = tmp_path / "mkvdata"
    mkv_data.mkdir()
    monkeypatch.setattr(mgr.aacs_dump, "data_dir", lambda *a, **k: mkv_data)

    async def failing_scan(source, report):
        # What MakeMKV does: writes a dump, then fails to open the disc.
        (mkv_data / "abc123.tgz").write_bytes(b"dump payload")
        report.on_event("Failed to open disc")
        raise RuntimeError("Failed to open disc")
    monkeypatch.setattr(m.pipeline, "scan_disc", failing_scan)

    wj = m.add_disc("disc:0")
    for _ in range(20):
        await __import__("asyncio").sleep(0)

    assert wj.state is JobState.FAILED
    log = "\n".join(wj.log)
    assert "AACS dump saved" in log
    assert ad.SUBMIT_TO in log
    assert "Nothing has been sent" in log

    kept = list((tmp_path / "staging" / "aacs-dumps").glob("*.tgz"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"dump payload"
    assert (mkv_data / "abc123.tgz").exists()      # MakeMKV's own copy is left alone


async def test_a_normal_failure_says_nothing_about_dumps(tmp_path, monkeypatch):
    """No dump written -> no noise. Most failures are dirty discs or bad drives."""
    from blurayripper.config import load_settings
    from blurayripper.web import manager as mgr
    from blurayripper.web.manager import JobManager

    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    m = JobManager(settings)
    empty = tmp_path / "mkvdata"; empty.mkdir()
    monkeypatch.setattr(mgr.aacs_dump, "data_dir", lambda *a, **k: empty)

    async def failing_scan(source, report):
        raise RuntimeError("SCSI error - MEDIUM ERROR")
    monkeypatch.setattr(m.pipeline, "scan_disc", failing_scan)

    wj = m.add_disc("disc:0")
    for _ in range(20):
        await __import__("asyncio").sleep(0)
    assert "AACS" not in "\n".join(wj.log)
