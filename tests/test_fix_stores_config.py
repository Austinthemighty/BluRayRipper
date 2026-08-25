"""Regressions for the store, the settings overlay, the box-set plan and the
encode worker.

The shared theme is a wrong answer that looks like a right one: a settings save
that reports success for a number nothing will ever use, a plan row whose typo
rips a whole disc into filenames Plex cannot match, a worker that finishes green
having placed an empty file. Each test here failed before its fix and states the
consequence it is guarding, not just the exception.
"""

import asyncio
import json
from pathlib import Path

import pytest

from blurayripper import boxset_plan as bp, settings_store as ss
from blurayripper.config import load_settings
from blurayripper.models import (
    DiscInfo, EpisodeAssignment, MediaKind, Stream, StreamType, Title,
)
from blurayripper.persistence import (
    JobRecord, MemoryStore, assignment_to_dict, disc_from_dict, disc_to_dict,
)
from blurayripper.pipeline import Job, JobState
from blurayripper.tools.handbrake import HandBrake
from blurayripper.tools.mkvtools import MkvTools
from blurayripper.worker import CLAIMABLE_STATES, WorkerReporter, WorkerRunner


def _toml(cfg_dir: Path, text: str = "") -> Path:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.toml").write_text(text, encoding="utf-8")
    return cfg_dir


# --- persistence: the shared jobs table ------------------------------------

def test_a_job_row_from_a_newer_build_still_loads_on_an_older_box():
    """Every box reads the same jobs table and they are updated one at a time,
    so a worker that hasn't been pulled yet reads rows a newer serve wrote. An
    unknown field used to raise TypeError out of load_all/claim_next — not for
    that one job, for the whole queue, until the last box was updated."""
    store = MemoryStore()
    store.save(JobRecord(id=5, source="disc:0", state="ready",
                         selected_title_ids=[3], priority=2))
    # Exactly what a newer build writing one extra column looks like from here.
    store._rows[5]["hdr_passthrough"] = True

    loaded = store.load_all()
    assert [r.id for r in loaded] == [5]
    assert loaded[0].selected_title_ids == [3]   # the fields we DO know survive
    assert loaded[0].priority == 2
    assert not hasattr(loaded[0], "hdr_passthrough")

    claimed = store.claim_next("encoder-A", ["ready"])
    assert claimed is not None and claimed.id == 5


def test_a_disc_keeps_makemkvs_own_titles_and_track_names_across_a_restart():
    """The names come off the disc once, at scan time. Dropping them from the
    stored form made the round-trip lossy: a job showed 'Director's Commentary'
    until serve restarted and then silently showed nothing."""
    disc = DiscInfo(source="disc:0", disc_name="HEAT", titles=[
        Title(title_id=0, duration_seconds=6432, name="Main Feature", streams=[
            Stream(stream_id=1, stream_type=StreamType.AUDIO, language="eng",
                   name="Director's Commentary")])])

    restored = disc_from_dict(json.loads(json.dumps(disc_to_dict(disc))))

    assert restored.titles[0].name == "Main Feature"
    assert restored.titles[0].streams[0].name == "Director's Commentary"


def test_a_worker_that_lost_its_claim_cannot_stamp_over_the_new_owner(tmp_path):
    """Box A loses the database for longer than the stale window; its job is
    legitimately reclaimed by box B while A's encode carries on. A's next write
    used to rewrite the whole row — state, worker, outputs — over the job B is
    actively encoding, and serve mirrored that to the UI."""
    store = MemoryStore()
    store.save(JobRecord(id=41, source="disc:0", state="ready"))
    store.claim_next("encoder-A", ["ready"])
    store.release(41)                      # serve requeues it: the claim is dropped
    reclaimed = store.claim_next("encoder-B", ["ready"])
    assert reclaimed is not None

    job = Job(source="disc:0")
    zombie = WorkerReporter(store, "encoder-A", 41, job)
    zombie.finish(JobState.FAILED, [], error="ConnectionError: database unreachable")

    row = {r.id: r for r in store.load_all()}[41]
    assert row.state == "ready"            # still B's to finish
    assert row.error is None
    assert row.worker != "encoder-A"

    # The box that actually holds the claim still reports normally.
    WorkerReporter(store, "encoder-B", 41, job).finish(
        JobState.DONE, ["/library/Movies/Heat (1995)/Heat (1995).mkv"])
    row = {r.id: r for r in store.load_all()}[41]
    assert row.state == "done" and row.worker == "encoder-B"


# --- config: a corrupt overlay must never stop a box booting ----------------

@pytest.mark.parametrize("body", ['"veryslow"', "123", '[{"encode": {}}]'])
def test_an_overlay_that_is_not_an_object_still_lets_serve_boot(tmp_path, body):
    """The read is wrapped precisely so a corrupt overlay can't stop startup,
    but valid JSON that isn't an object has no .items() and raised from the
    merge instead. The settings page rendered fine throughout, so the symptom
    was serve alone refusing to start."""
    _toml(tmp_path, '[encode]\npreset_name = "Plex H265 1080p"\n')
    (tmp_path / "settings.local.json").write_text(body, encoding="utf-8")

    settings = load_settings(tmp_path)

    assert settings.encode.preset_name == "Plex H265 1080p"   # the TOML still applies
    assert settings.queue.encode_capacity >= 1


def test_an_overlay_that_is_not_utf8_still_lets_serve_boot_and_render(tmp_path):
    """A box losing power mid-save leaves bytes that aren't UTF-8. That is
    neither an OSError nor a JSONDecodeError, so the one corruption the guard
    existed for was the one that got through — on serve AND on the page that
    would let you fix it."""
    _toml(tmp_path, '[encode]\npreset_name = "Plex H265 1080p"\n')
    (tmp_path / "settings.local.json").write_bytes(b'{"encode": {"preset_name": "Caf\xe9"}}')

    assert load_settings(tmp_path).encode.preset_name == "Plex H265 1080p"
    assert ss.read_overlay(tmp_path) == {}
    payload = ss.settings_ui_payload(tmp_path)
    assert any(g["title"] == "Encode" for g in payload["groups"])


# --- settings overlay: values that must not be stored -----------------------

def test_a_blank_container_is_refused_rather_than_saved_as_the_container(tmp_path):
    """A blank was persisted, and load_settings reads the overlay with
    .get(key, default) — so the key being PRESENT means the default never
    applies. container='' placed '…/Heat (1995).' with no extension, from a job
    that reported success and that Plex cannot index."""
    _toml(tmp_path)

    with pytest.raises(ValueError, match="mkv, mp4"):
        ss.save_overlay(tmp_path, {"encode": {"container": ""}})
    with pytest.raises(ValueError, match="copy, aac"):
        ss.save_overlay(tmp_path, {"encode": {"ffmpeg_audio": "  "}})

    assert ss.read_overlay(tmp_path) == {}
    assert load_settings(tmp_path).encode.container in ("mkv", "mp4")


def test_a_json_null_language_list_does_not_become_the_word_none(tmp_path):
    """The save endpoint takes arbitrary JSON, and null became str(None).split(',')
    == ['None'] — which matches no track, so every audio and subtitle stream was
    dropped from an encode that still reported success."""
    _toml(tmp_path)

    ss.save_overlay(tmp_path, {"languages": {"keep": None}})

    languages = load_settings(tmp_path).languages
    assert languages.keep == ["eng"]           # nothing set -> the default applies
    assert languages.is_kept("eng") is True


def test_a_folder_given_as_a_list_is_refused_not_turned_into_a_literal_path(tmp_path):
    """storage.output_dir = ['a','b'] was stored as the text "['a', 'b']" and
    then created as a real directory of that name under the repo — a library
    nobody would ever look in."""
    _toml(tmp_path)

    with pytest.raises(ValueError, match="not a list or object"):
        ss.save_overlay(tmp_path, {"storage": {"output_dir": ["a", "b"]}})
    assert ss.read_overlay(tmp_path) == {}


@pytest.mark.parametrize("section,key,bad", [
    ("queue", "encode_capacity", 0),
    ("queue", "encode_capacity", 10 ** 30),
    ("subtitles", "ocr_concurrency", -4),
    ("web", "port", 70000),
])
def test_a_number_outside_its_range_is_refused_instead_of_saved_and_clamped(
        tmp_path, section, key, bad):
    """The consumers clamp (EncodeScheduler floors capacity at 1, the OCR
    semaphore likewise), so the page reported a successful save of a value that
    could never be in effect — and a port outside 1-65535 stops serve binding
    at all."""
    _toml(tmp_path)

    with pytest.raises(ValueError, match=f"{section}.{key} must be at "):
        ss.save_overlay(tmp_path, {section: {key: bad}})

    assert ss.read_overlay(tmp_path) == {}
    settings = load_settings(tmp_path)
    assert settings.queue.encode_capacity >= 1
    assert 1 <= settings.web_port <= 65535


def test_a_typo_in_the_restart_age_is_caught_at_save_not_at_the_next_boot(tmp_path):
    """'float' had no branch in the coercion, so whatever was typed was stored
    as text — and the first thing that looked at it was config.load_settings'
    float() at startup. One typo in a Settings box and serve would not boot."""
    _toml(tmp_path)

    with pytest.raises(ValueError, match="must be a number"):
        ss.save_overlay(tmp_path, {"cluster": {"worker_restart_after_days": "banana"}})
    assert ss.read_overlay(tmp_path) == {}
    assert load_settings(tmp_path).cluster.worker_restart_after_days == 0

    saved = ss.save_overlay(tmp_path, {"cluster": {"worker_restart_after_days": "1.5"}})
    assert saved["cluster"]["worker_restart_after_days"] == 1.5
    assert load_settings(tmp_path).cluster.worker_restart_after_days == 1.5


# --- box-set plan -----------------------------------------------------------

def test_one_unclosed_quote_reports_the_line_instead_of_breaking_every_scan():
    """csv swallows everything after an unbalanced quote into a single field,
    which trips its 128 KB limit. parse_csv is documented never to raise, and
    it is read on EVERY disc insert — so one stray quote in the plan took the
    box-set editor and the auto-naming path down together."""
    text = ('disc_label,show,season,first_episode\n'
            'GOOD_S1_D1,Good Show,1,1\n'
            'BAD_S1_D1,"The Office\n'
            + "FILLER_S1_D1,Filler,1,1\n" * 8000)

    rows, errors = bp.parse_csv(text)          # must not raise

    assert [r.disc_label for r in rows] == ["GOOD_S1_D1"]   # the good line survives
    assert any("unreadable" in e and "quote" in e for e in errors), errors


def test_a_plan_exported_from_excel_as_csv_utf8_still_finds_its_columns():
    """Excel's 'CSV UTF-8' writes a byte-order mark, which became part of the
    first header name — so the operator was told disc_label was missing while
    looking straight at it."""
    rows, errors = bp.parse_csv(
        "﻿disc_label,show,season,first_episode\nTHE_OFFICE_S3_D2,The Office,3,9\n")

    assert errors == []
    assert [r.disc_label for r in rows] == ["THE_OFFICE_S3_D2"]
    assert rows[0].season == 3 and rows[0].first_episode == 9


def test_a_plan_saved_from_notepad_as_windows_1252_still_loads(tmp_path):
    """A box-set CSV with an accented show title saved on Windows is cp1252,
    not UTF-8 — the normal case, not an exotic one. UnicodeDecodeError is not
    an OSError, so it came out of load_plan and out of every caller, including
    the disc-scan path that reads the plan on every insert."""
    settings = load_settings(_toml(tmp_path / "config"))
    bp.plan_path(settings).write_bytes(
        b'disc_label,show,season,first_episode\nCAFE_S1_D1,Caf\xe9 Show,1,1\n')

    rows = bp.load_rows(settings)             # must not raise

    assert [r.disc_label for r in rows] == ["CAFE_S1_D1"]
    assert rows[0].show.startswith("Caf")


def test_a_first_episode_below_one_parks_the_disc_instead_of_ripping_it_to_e00():
    """A plan row is consent to rip with NO naming gate, so a typo'd cell has no
    backstop: first_episode=0 numbered the files S03E00, S03E01… and -5 gave
    S03E-5, which Plex will not match and nobody would think to look for."""
    disc = DiscInfo(source="disc:0", volume_label="THE_OFFICE_S3_D2", titles=[
        Title(title_id=i, duration_seconds=1500, size_bytes=1) for i in range(3)])

    rows, errors = bp.parse_csv(
        "disc_label,show,season,first_episode\nTHE_OFFICE_S3_D2,The Office,3,0\n")
    assert rows[0].first_episode is None
    assert any("first_episode must be 1 or more" in e for e in errors), errors

    # And at the point of consent, for a row built any other way.
    for bad in (0, -5):
        row = bp.PlanRow(disc_label="THE_OFFICE_S3_D2", show="The Office",
                         season=3, first_episode=bad)
        result = bp.assignments_for(row, disc)
        assert not result.ok and result.assignments == []
        assert str(bad) in result.reason and "episode" in result.reason

    # A sane row still auto-names, so the guard did not close the whole path.
    good = bp.PlanRow(disc_label="THE_OFFICE_S3_D2", show="The Office",
                      season=3, first_episode=9)
    assert [a.episode for a in bp.assignments_for(good, disc).assignments] == [9, 10, 11]


# --- worker -----------------------------------------------------------------

def _worker_config(root: Path) -> Path:
    return _toml(root / "config", "\n".join([
        "[storage]", f'staging_dir = "{root / "staging"}"',
        f'output_dir = "{root / "library"}"',
        "[metadata]", "write_nfo = false", "fetch_artwork = false",
        "[serve]", "cleanup_stale_tools = false",
        "[cluster]", "use_workers = true", "worker_poll_seconds = 0",
        "heartbeat_seconds = 3600", "offline_alert_seconds = 0",
        "[disc]", "extract_cover_art = false",
    ]) + "\n")


def _queued(store: MemoryStore, settings, job_id: int = 1) -> Path:
    ripped = settings.storage.staging_dir / f"job_{job_id}" / "rip" / "t000" / "t.mkv"
    ripped.parent.mkdir(parents=True, exist_ok=True)
    ripped.write_text("a whole Blu-ray, honest")
    store.save(JobRecord(
        id=job_id, source="disc:0", state="ready", selected_title_ids=[0],
        assignments=[assignment_to_dict(
            EpisodeAssignment(0, MediaKind.MOVIE, "Heat", 1995))],
        checkpoints={"0": {"ripped_path": f"job_{job_id}/rip/t000/t.mkv"}}))
    return ripped


def _stub_handbrake(monkeypatch, payload: bytes):
    """HandBrakeCLI, writing exactly what we tell it and exiting 0 either way."""
    async def encode(self, src, dst, *, on_progress=None, tune_grain=None,
                     deinterlace=False):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(payload)
        return dst

    async def height(self, path):
        return 1080

    monkeypatch.setattr(HandBrake, "encode", encode)
    monkeypatch.setattr(MkvTools, "video_height", height)


async def test_an_encode_that_wrote_nothing_is_not_reported_done(tmp_path, monkeypatch):
    """HandBrake.encode is the one encoder wrapper with no 'did it write
    anything' check, and the clustered path never verified its outputs either —
    so a 0-byte file was moved into the Plex library, checkpointed as placed
    (which makes every later retry skip the title) and reported green."""
    cfg = _worker_config(tmp_path)
    settings = load_settings(cfg)
    store = MemoryStore()
    _queued(store, settings)
    _stub_handbrake(monkeypatch, b"")

    runner = WorkerRunner(settings, worker_id="encoder-A")
    runner.store = store
    await runner._process(store.claim_next("encoder-A", CLAIMABLE_STATES))

    row = {r.id: r for r in store.load_all()}[1]
    assert row.state == "failed"
    assert row.outputs == []
    # The pipeline now catches this one layer earlier (it weighs the file before
    # the move, so nothing empty reaches the library at all). Assert the outcome
    # and that the message identifies the title, not which layer caught it.
    assert "Heat (1995)" in row.error
    assert "empty file" in row.error or "0 bytes" in row.error, row.error
    # Recoverable: the rip is still in staging and the placed flag is gone, so a
    # Retry re-encodes instead of skipping the title it already "placed".
    assert (settings.storage.staging_dir / "job_1" / "rip" / "t000" / "t.mkv").exists()
    assert "placed" not in row.checkpoints["0"]

    # Prove the recovery: same job, a HandBrake that works.
    _stub_handbrake(monkeypatch, b"h265 bytes")
    row.state = "ready"
    store.save(row)
    store.release(1)
    retry = WorkerRunner(load_settings(cfg), worker_id="encoder-B")
    retry.store = store
    await retry._process(store.claim_next("encoder-B", CLAIMABLE_STATES))

    done = {r.id: r for r in store.load_all()}[1]
    assert done.state == "done"
    assert Path(done.outputs[0]).read_bytes() == b"h265 bytes"


async def test_a_job_nobody_named_fails_instead_of_finishing_with_no_files(
        tmp_path, monkeypatch):
    """encode_phase logs 'Skipping title 0: no naming assignment' and returns
    nothing, and the worker reported DONE with zero outputs — then swept the
    staging tree, destroying the rip for a disc that only ever needed naming."""
    cfg = _worker_config(tmp_path)
    settings = load_settings(cfg)
    store = MemoryStore()
    ripped = _queued(store, settings)
    unnamed = {r.id: r for r in store.load_all()}[1]
    unnamed.assignments = []
    store.save(unnamed)
    _stub_handbrake(monkeypatch, b"h265 bytes")

    runner = WorkerRunner(settings, worker_id="encoder-A")
    runner.store = store
    await runner._process(store.claim_next("encoder-A", CLAIMABLE_STATES))

    row = {r.id: r for r in store.load_all()}[1]
    assert row.state == "failed"
    assert row.outputs == []
    assert "no naming assignment" in row.error and "Retry" in row.error
    assert ripped.exists()                       # the rip was NOT thrown away
    assert not list((settings.storage.output_dir / "Movies").rglob("*.mkv"))


async def test_a_worker_booted_while_the_database_is_down_waits_for_it(
        tmp_path, monkeypatch):
    """Both boxes come back from the same power cut and Postgres is on another
    host, so the worker regularly starts first. init() outside any try made it
    exit on the spot — the machine was up, idle, and encoding nothing until
    someone noticed. serve is explicitly built to boot without the database."""
    import blurayripper.version as version_mod
    monkeypatch.setattr(version_mod, "get_version", lambda: "0.1.0+1.abcdefg")

    class DownThenUp(MemoryStore):
        durable = True

        def __init__(self):
            super().__init__()
            self.init_calls = 0

        def init(self):
            self.init_calls += 1
            if self.init_calls < 3:
                raise ConnectionError("database unreachable")

    cfg = _worker_config(tmp_path)
    settings = load_settings(cfg)
    settings.database_url = "postgresql://cluster/blurayripper"
    monkeypatch.setattr("blurayripper.worker.load_settings", lambda *a, **k: settings)
    store = DownThenUp()
    _queued(store, settings)

    runner = WorkerRunner(settings, worker_id="encoder-A")
    runner.store = store

    processed = []

    async def fake_process(rec):
        processed.append(rec.id)
        runner.request_stop()

    monkeypatch.setattr(runner, "_process", fake_process)
    await asyncio.wait_for(runner.run(), timeout=5)

    assert store.init_calls == 3        # it waited out the outage rather than exiting
    assert processed == [1]             # and then did the work that was waiting
