"""User story: "I put a Blu-ray in the drive and a named, encoded, subtitled
movie appears in my Plex library without me touching anything."

Only the external TOOLS are stubbed — makemkvcon, HandBrake, mkvmerge/mkvextract,
SubtitleEdit's OCR, TMDB's HTTP, the eject IOCTL and the ntfy push. The
JobManager, the Pipeline, MemoryStore and the naming/library/nfo services are the
real objects, so what these tests pin is the SEQUENCE those real objects run in:
scan -> auto-confirm -> rip -> eject -> subtitles -> encode -> place -> notify.

The eject position is the point of the design, not an incidental detail: the disc
must leave the drive as soon as ripping is done, so the next one can go in while
the (hours-long) encode runs.
"""

import asyncio
import time
from pathlib import Path

import pytest

import blurayripper.pipeline as pipeline_mod
import blurayripper.web.manager as manager_mod
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, Stream, StreamType, Title
from blurayripper.persistence import MemoryStore
from blurayripper.pipeline import JobState
from blurayripper.services import library, metadata
from blurayripper.tools.makemkv import Drive
from blurayripper.tools.mkvtools import ProbedFile, SubtitleTrack
from blurayripper.web.manager import JobManager

FEATURE_SECONDS = 6762  # 1:52:42 — long enough that discinfo calls the disc a movie
DISC_LABEL = "WARGAMES"  # what the volume label says: no year, no capitalisation
TMDB_TITLE = "WarGames"
TMDB_YEAR = 1983
# Deliberately short + plain: looks_like_sdh must not reclassify it, or the
# sidecar would be named ".en.sdh.srt" instead of ".en.default.srt".
SRT_TEXT = "1\n00:00:12,000 --> 00:00:15,000\nShall we play a game?\n\n"


def _pgs_track() -> SubtitleTrack:
    return SubtitleTrack(track_id=3, codec="S_HDMV/PGS", language="eng", is_pgs=True)


def _wargames_disc() -> DiscInfo:
    """A single-feature movie disc: one 112-minute title plus a trailer."""
    return DiscInfo(
        source="disc:0",
        disc_name=DISC_LABEL,
        volume_label=DISC_LABEL,
        drive_device="F:",
        titles=[
            Title(
                title_id=0, duration_seconds=FEATURE_SECONDS, chapter_count=16,
                source_filename="00002.mpls", output_filename="title_t00.mkv",
                streams=[
                    Stream(1, StreamType.VIDEO, codec="V_MPEG4/ISO/AVC"),
                    Stream(2, StreamType.AUDIO, codec="A_DTS", language="eng"),
                    Stream(3, StreamType.SUBTITLE, codec="S_HDMV/PGS", language="eng"),
                ],
            ),
            Title(title_id=1, duration_seconds=124, chapter_count=1,
                  source_filename="00801.mpls", output_filename="title_t01.mkv"),
        ],
    )


class Rig:
    """The stubbed world plus the recorders the assertions read."""

    def __init__(self, manager, settings, steps, notes, tmdb, drive, disc):
        self.manager = manager
        self.settings = settings
        self.steps = steps          # ordered (kind, detail) tuples of tool calls
        self.notes = notes          # (title, message) pushes that reached ntfy
        self.tmdb = tmdb            # mutable list of MovieCandidate returned by search
        self.drive = drive          # {"readable": bool} — what makemkvcon can see
        self.disc = disc            # the DiscInfo the scan returns

    @property
    def movie_dir(self) -> Path:
        return self.settings.storage.movies_root / f"{TMDB_TITLE} ({TMDB_YEAR})"

    @property
    def video(self) -> Path:
        return self.movie_dir / f"{TMDB_TITLE} ({TMDB_YEAR}).mkv"

    def kinds(self) -> list[str]:
        return [kind for kind, _detail in self.steps]

    def job(self):
        return next(iter(self.manager.jobs.values()))

    async def poll_drives(self) -> None:
        """One tick of the background drive watcher."""
        self.manager._drives_cache = await self.manager.list_drives()
        self.manager._auto_open(self.manager._drives_cache)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "staging")
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "library")
    settings.config_dir = tmp_path / "config"  # no box-set plan CSV to match
    settings.auto_open = True
    settings.auto_rip = True
    settings.auto_eject = True
    settings.cluster.use_workers = False
    settings.encode.container = "mkv"
    settings.auto_retry_max = 0  # a failed job stays failed instead of sleeping on a backoff
    settings.tmdb_api_key = "test-key"
    settings.notify_url = "http://ntfy.invalid/blurayripper"

    steps: list[tuple[str, object]] = []
    notes: list[tuple[str, str]] = []
    tmdb = [metadata.MovieCandidate(tmdb_id=860, title=TMDB_TITLE, year=TMDB_YEAR,
                                    overview="A teenager hacks WOPR.")]

    async def fake_search_movie(self, query, year=None):
        steps.append(("tmdb-search", query))
        return list(tmdb)

    async def fake_movie_artwork(self, title, year):
        return None, None

    monkeypatch.setattr(metadata.TMDBClient, "search_movie", fake_search_movie)
    monkeypatch.setattr(metadata.TMDBClient, "movie_artwork", fake_movie_artwork)

    async def fake_notify(url, title, message, priority=None):
        notes.append((title, message))
        return True

    monkeypatch.setattr(manager_mod, "notify", fake_notify)

    def fake_eject(device):
        steps.append(("eject", device))

    monkeypatch.setattr(pipeline_mod, "eject", fake_eject)

    real_place = library.place

    def spy_place(src, dest, **kw):
        steps.append(("place", Path(dest).name))
        return real_place(src, dest, **kw)

    monkeypatch.setattr(library, "place", spy_place)

    manager = JobManager(settings, store=MemoryStore())
    disc = _wargames_disc()
    # makemkvcon can't probe a drive it is already holding open, so a poll during
    # a scan or rip reports the drive as empty even though the disc is right there.
    drive = {"readable": True}

    # --- makemkvcon -----------------------------------------------------
    async def fake_list_drives(*, timeout=60.0):
        return [Drive(index=0, name="ASUS BW-16D1HT", device="F:",
                      disc_name=disc.disc_name if drive["readable"] else None)]

    async def fake_scan(source, *, on_message=None):
        steps.append(("scan", source))
        if on_message is not None:
            on_message("Opening Blu-ray disc")
        return disc

    async def fake_rip(source, title_id, dest_dir, *, on_progress=None, on_message=None):
        steps.append(("rip", title_id))
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"title_t{title_id:02d}.mkv"
        out.write_bytes(b"RIPPED-VIDEO")
        if on_progress is not None:
            on_progress("Saving to MKV file", 1.0)
        return [out]

    monkeypatch.setattr(manager.pipeline.makemkv, "list_drives", fake_list_drives)
    monkeypatch.setattr(manager.pipeline.makemkv, "scan", fake_scan)
    monkeypatch.setattr(manager.pipeline.makemkv, "rip_title", fake_rip)

    # --- mkvmerge / mkvextract ------------------------------------------
    async def fake_probe(path):
        return ProbedFile(duration_seconds=FEATURE_SECONDS, video_height=1080,
                          audio_languages=["eng"], subtitles=[_pgs_track()])

    async def fake_identify(path):
        steps.append(("identify-subs", Path(path).name))
        return [_pgs_track()]

    async def fake_extract(path, specs, *, on_progress=None):
        steps.append(("extract-subs", tuple(tid for tid, _out in specs)))
        for _tid, out in specs:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"PGS-BITMAPS")
        if on_progress is not None:
            on_progress(1.0)

    async def fake_height(path):
        return 1080

    monkeypatch.setattr(manager.pipeline.mkvtools, "probe", fake_probe)
    monkeypatch.setattr(manager.pipeline.mkvtools, "identify_subtitles", fake_identify)
    monkeypatch.setattr(manager.pipeline.mkvtools, "extract_tracks", fake_extract)
    monkeypatch.setattr(manager.pipeline.mkvtools, "video_height", fake_height)

    # --- SubtitleEdit OCR ------------------------------------------------
    async def fake_ocr(sup_path, language, out_dir):
        steps.append(("ocr", language))
        assert Path(sup_path).exists(), "OCR was handed a .sup that was never extracted"
        out_dir.mkdir(parents=True, exist_ok=True)
        srt = Path(out_dir) / (Path(sup_path).stem + ".srt")
        srt.write_text(SRT_TEXT, encoding="utf-8")
        return srt

    monkeypatch.setattr(manager.pipeline.subtitleedit, "ocr_to_srt", fake_ocr)

    # --- HandBrake -------------------------------------------------------
    async def fake_encode(src, dst, *, on_progress=None, tune_grain=None, deinterlace=False):
        steps.append(("encode", Path(dst).name))
        assert Path(src).exists(), "HandBrake was pointed at a rip that isn't there"
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"ENCODED-VIDEO")
        return dst

    monkeypatch.setattr(manager.pipeline.handbrake, "encode", fake_encode)

    return Rig(manager, settings, steps, notes, tmdb, drive, disc)


async def _drain(manager, timeout=15.0):
    """Run until every background task the manager spawned (scan, run, notify)
    has finished — including ones spawned by other tasks."""
    deadline = time.monotonic() + timeout
    while True:
        live = [t for t in list(manager._tasks) if not t.done()]
        if not live:
            await asyncio.sleep(0)
            if not [t for t in list(manager._tasks) if not t.done()]:
                return
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("background work did not finish in time")
        await asyncio.wait(live, timeout=remaining)


async def _run_story(rig):
    await rig.poll_drives()
    await _drain(rig.manager)


# --- the story ----------------------------------------------------------

async def test_a_disc_left_in_the_drive_becomes_a_finished_library_file(rig):
    """The whole promise in one assertion: nobody picks titles, confirms a name,
    or presses anything, and a playable, named, subtitled file exists in the Plex
    library afterwards. If any hand-off between manager and pipeline breaks, the
    job parks or fails and the library folder stays empty."""
    await _run_story(rig)

    assert rig.video.exists(), f"no video in the library; steps were {rig.steps}"
    assert rig.video.read_bytes() == b"ENCODED-VIDEO"  # the encode, not the raw rip
    assert (rig.movie_dir / f"{TMDB_TITLE} ({TMDB_YEAR}).en.default.srt").exists()
    assert rig.job().state is JobState.DONE
    assert rig.job().outputs == [str(rig.video)]
    assert rig.job().error is None


async def test_the_steps_happen_in_the_order_the_design_depends_on(rig):
    """Pins the whole sequence, not just the end state: scan, auto-confirm from
    TMDB, rip, EJECT, subtitles, encode, place. Any stage that migrates (subtitles
    after the encode, the eject sliding past the encode) changes this list."""
    await _run_story(rig)

    assert rig.steps == [
        ("scan", "disc:0"),
        ("tmdb-search", DISC_LABEL),
        ("rip", 0),
        ("eject", "F:"),
        ("identify-subs", "title_t00.mkv"),
        ("extract-subs", (3,)),
        ("ocr", "eng"),
        ("place", f"{TMDB_TITLE} ({TMDB_YEAR}).en.default.srt"),
        ("encode", f"{TMDB_TITLE} ({TMDB_YEAR}).mkv"),
        ("place", f"{TMDB_TITLE} ({TMDB_YEAR}).mkv"),
    ]


async def test_the_disc_is_ejected_before_the_encode_so_the_next_one_can_go_in(rig):
    """The reason ripping and encoding are separate stages: the drive must be free
    while the hours-long encode runs. An eject that drifts to the end of the job
    (or is dropped) silently costs a whole encode's worth of drive time."""
    await _run_story(rig)

    kinds = rig.kinds()
    assert kinds.index("rip") < kinds.index("eject") < kinds.index("encode")
    # ...and nothing but the eject happens between the rip finishing and the
    # subtitle work starting, i.e. the disc is released at the first moment it can be.
    assert kinds[kinds.index("rip") + 1] == "eject"


async def test_the_ejected_drive_is_the_one_the_disc_is_actually_in(rig):
    """The device recorded at scan time can go stale (drives renumber, the disc is
    moved), so the eject re-resolves it from the live drive list. Trusting the
    stored one on a multi-drive box pops the wrong tray — ejecting another job's
    disc mid-rip. Only the stored device is varied here; the live list still says
    disc:0 is drive F:."""
    rig.disc.drive_device = "E:"   # what the scan recorded, before the renumber

    await _run_story(rig)

    assert ("eject", "F:") in rig.steps
    assert ("eject", "E:") not in rig.steps
    # The drive's cached disc name is dropped on eject, so the panel stops showing
    # a disc that is now in the user's hand and re-inserting it reads as new.
    assert all(d.disc_name is None for d in rig.manager._drives_cache)
    assert rig.manager._last_disc == {}


async def test_the_file_is_named_from_tmdb_not_from_the_disc_label(rig):
    """The disc only says "WARGAMES". Plex needs "WarGames (1983)" — the year comes
    from the TMDB match alone, so a fallback to the volume label would leave an
    unmatchable file (and the wrong folder) in the library."""
    await _run_story(rig)

    movies_root = rig.settings.storage.movies_root
    assert rig.video.exists(), f"nothing was placed at the TMDB name; steps were {rig.steps}"

    # Read the names back OFF THE DISK rather than comparing paths this test built
    # (rig.video is constructed from TMDB_TITLE/TMDB_YEAR, so comparing against it
    # would pass no matter what the code named things — and on a case-insensitive
    # filesystem even the "not under the raw label" check would be satisfied by a
    # folder literally called "WARGAMES").
    assert [p.name for p in sorted(movies_root.iterdir())] == [
        f"{TMDB_TITLE} ({TMDB_YEAR})"]
    assert sorted(p.name for p in rig.movie_dir.glob("*.mkv")) == [
        f"{TMDB_TITLE} ({TMDB_YEAR}).mkv"]
    # Both halves come from TMDB: the casing the disc label doesn't have, and the
    # year it doesn't carry at all.
    assert DISC_LABEL not in rig.video.name and str(TMDB_YEAR) in rig.video.name


async def test_two_equally_good_matches_park_the_job_instead_of_guessing(rig):
    """Same disc, one thing varied: TMDB returns two exact-title matches. A wrong
    auto-pick names a whole disc as the wrong film, so ambiguity must stop at the
    naming gate. The disc is ripped meanwhile — that needs no name, and it gets
    the disc out of the drive — but nothing is named, encoded or placed."""
    rig.tmdb.append(metadata.MovieCandidate(
        tmdb_id=99999, title=TMDB_TITLE, year=2018, overview="A remake."))

    await _run_story(rig)

    assert rig.job().state is JobState.AWAITING_MAPPING
    assert rig.job().assignments == {}, "an ambiguous match was named anyway"
    assert "encode" not in rig.kinds()
    assert not rig.settings.storage.movies_root.exists()


async def test_the_subtitles_land_beside_the_video_under_the_video_s_name(rig):
    """Plex only finds a sidecar named after its video. OCR names its output after
    the .sup file, so a placement step that skipped the rename would leave
    "title_t00.t3.eng.srt" next to the movie — invisible to Plex."""
    await _run_story(rig)

    srt = rig.movie_dir / f"{TMDB_TITLE} ({TMDB_YEAR}).en.default.srt"
    assert srt.exists(), f"sidecars present: {sorted(p.name for p in rig.movie_dir.iterdir())}"
    assert srt.read_text(encoding="utf-8") == SRT_TEXT
    assert srt.parent == rig.video.parent
    assert not list(rig.movie_dir.glob("title_t00*"))


async def test_a_job_is_only_finished_once_its_files_are_really_on_disk(rig, monkeypatch):
    """DONE is claimed only after the pipeline re-checks the output folder. If the
    placed sidecar vanishes between placing and verifying, the job must fail rather
    than report a green run with a subtitle-less movie — the half-finished job that
    nobody notices until they sit down to watch it."""
    placing = library.place  # the rig's recorder, so the step order is still logged

    def eat_the_srt(src, dest, **kw):
        out = placing(src, dest, **kw)
        if out.suffix == ".srt":
            out.unlink()  # e.g. a NAS write that silently didn't stick
        return out

    monkeypatch.setattr(library, "place", eat_the_srt)

    await _run_story(rig)

    assert rig.job().state is JobState.FAILED
    assert "missing subtitle" in (rig.job().error or "")
    # The expensive work is kept so a retry re-places rather than re-rips.
    assert rig.video.exists()


async def test_finishing_pushes_exactly_one_done_notification(rig):
    """The phone should buzz once per disc. `_maybe_notify_done` is reached again
    on any re-run/recovery of the same job, so an unguarded push means a finished
    disc re-announces itself on every restart."""
    await _run_story(rig)

    done = [n for n in rig.notes if n[0].startswith("✅ Done")]
    assert len(done) == 1
    assert done[0] == (
        f"✅ Done: {TMDB_TITLE} ({TMDB_YEAR})",
        f"{TMDB_TITLE} ({TMDB_YEAR}) finished encoding — 1 file placed in your library.",
    )

    rig.manager._maybe_notify_done(rig.job())  # what a resume/re-run would do
    await _drain(rig.manager)
    assert len([n for n in rig.notes if n[0].startswith("✅ Done")]) == 1


async def test_the_pushes_tell_the_story_in_order(rig):
    """Three moments matter to someone not watching the UI: it started by itself,
    the disc can come out, it's in the library. Order is the information — a
    "swap the disc" push that arrived after "done" would be useless."""
    await _run_story(rig)

    assert [title for title, _msg in rig.notes] == [
        f"⏩ Auto-ripping: {TMDB_TITLE} ({TMDB_YEAR})",
        f"💿 Finished ripping: {TMDB_TITLE}",
        f"✅ Done: {TMDB_TITLE} ({TMDB_YEAR})",
    ]
    # The eject push has to be able to say the tray is already open.
    swap = next(msg for title, msg in rig.notes if title.startswith("💿"))
    assert "disc ejected" in swap


async def test_the_same_disc_sitting_there_is_not_queued_twice(rig):
    """The watcher re-enumerates every few seconds and keeps seeing the same disc
    — including a tick where the drive reads EMPTY because the scan is holding it
    open, then reads the label again. Without the de-dupe one disc becomes a job
    per poll, all fighting over the same drive."""
    await rig.poll_drives()               # tray closes -> one job, scan queued
    await rig.poll_drives()               # plain repeat tick
    rig.drive["readable"] = False         # scan has the drive open: probe sees nothing
    await rig.poll_drives()
    rig.drive["readable"] = True          # ...and the label comes back
    await rig.poll_drives()
    await _drain(rig.manager)

    assert len(rig.manager.jobs) == 1
    assert rig.kinds().count("scan") == 1
    assert rig.video.exists()             # the one job still ran to completion


async def test_the_finished_job_is_durable_before_it_is_announced(rig):
    """A clustered box reads the store, not this process's memory. If DONE isn't
    written there, a restart resurrects a finished job and re-rips or re-encodes
    work that is already in the library."""
    await _run_story(rig)

    records = rig.manager.store.load_all()
    assert len(records) == 1
    assert records[0].state == JobState.DONE.value
    assert records[0].outputs == [str(rig.video)]
    # The rip checkpoint survives too, so a recovery knows not to re-rip.
    assert records[0].checkpoints["0"]["placed"] is True
