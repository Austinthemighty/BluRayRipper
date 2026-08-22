"""Two short stories about being asked, and being told.

(a) GRAIN — "show me what film grain does before I commit hours to an encode."
    A job set to "preview after rip" has to stop AFTER the rip and BEFORE the
    encode, hand you two clips cut from your own disc, and then encode with the
    one you picked. The pieces are the pipeline's _resolve_grain, the web
    reporter's resolve_grain gate and the manager's set_grain — and the failures
    that matter live between them: a gate that re-opens per episode (which read
    as an endless "pick grain" loop), a gate that re-opens after every resume,
    and a pick that never reaches HandBrake.

(b) ALERTS — "tell me when something needs me." Every push goes out through the
    real services.notify (only httpx is faked), so these pin what actually
    leaves the box: how many requests, in what order, with which ntfy priority.
    The thing being defended is trust in the alerts — one duplicate per poll and
    you start ignoring them; one swallowed alert and you stop believing them.

Only the external tools are stubbed (makemkvcon, HandBrake, mkvmerge, the
network). The JobManager, the Pipeline, the store and the naming/library
services are real.
"""

import asyncio

import pytest

import blurayripper.services.notify as notify_mod
import blurayripper.web.manager as manager_mod
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import MemoryStore, WorkerInfo
from blurayripper.pipeline import Job, JobState
from blurayripper.tools.makemkv import Drive
from blurayripper.tools.mkvtools import ProbedFile
from blurayripper.web.manager import JobManager

SHOW = "Firefly"
SHOW_YEAR = 2002
DISC_LABEL = "FIREFLY_D1"
EPISODE_SECONDS = 44 * 60
# How an alert names a two-episode job: an alert that only said "FIREFLY_D1"
# couldn't tell you which disc of the set to go and find.
JOB_NAME = f"{SHOW} S01E01–E02"


class ClusteredStore(MemoryStore):
    """Stands in for the shared Postgres the serve box and its workers share."""

    def __init__(self):
        super().__init__()
        self.workers: list[WorkerInfo] = []

    @property
    def durable(self) -> bool:
        return True

    def list_workers(self) -> list[WorkerInfo]:
        return list(self.workers)


def _disc(episodes: int) -> DiscInfo:
    return DiscInfo(
        source="disc:0", disc_name=DISC_LABEL, volume_label=DISC_LABEL,
        drive_device="F:",
        titles=[Title(title_id=i, duration_seconds=EPISODE_SECONDS, chapter_count=5,
                      source_filename=f"{800 + i:05d}.mpls")
                for i in range(episodes)],
    )


def _assignments(episodes: int) -> list[EpisodeAssignment]:
    return [EpisodeAssignment(title_id=i, kind=MediaKind.TV, show_or_movie_title=SHOW,
                              year=SHOW_YEAR, season=1, episode=i + 1)
            for i in range(episodes)]


async def _until(predicate, what: str, limit: int = 20000) -> None:
    """Yield to the loop until ``predicate()`` holds. No wall-clock waiting: every
    tool is stubbed, so anything that is going to happen happens on the next few
    ticks."""
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


class Rig:
    """One serve box with stubbed tools, plus the recorders the assertions read.

    ``calls`` is the ordered tool log: ("rip", title_id), ("clip", tune_grain)
    for each grain preview segment HandBrake renders, and ("encode", tune_grain)
    for each real encode.
    """

    def __init__(self, settings, manager, store, calls, notes, spawned, disc):
        self.settings = settings
        self.manager = manager
        self.store = store
        self.calls = calls
        self.notes = notes          # (title, message, priority) pushes
        self.spawned = spawned      # every background task the manager started
        self._disc = disc           # what the next scan finds in the drive
        self.fail_encode: str | None = None

    def set_disc(self, episodes: int) -> None:
        """Swap in a disc with a different number of episodes on it."""
        self._disc["disc"] = _disc(episodes)

    # --- driving ---------------------------------------------------------

    async def queue_disc(self, episodes: int, *, grain_mode: str = "auto",
                         tune_grain: bool | None = None):
        """The operator's clicks: insert the disc, pick the titles, name them and
        choose how grain is decided."""
        wj = self.manager.add_disc("disc:0")
        await _until(
            lambda: wj.state in (JobState.AWAITING_TITLES, JobState.AWAITING_MAPPING),
            "the job to park for the operator")
        if wj.state is JobState.AWAITING_TITLES:   # a one-title disc skips this gate
            self.manager.set_titles(wj.id, list(range(episodes)))
        self.manager.set_mapping(wj.id, _assignments(episodes),
                                 grain_mode=grain_mode, tune_grain=tune_grain)
        return wj

    async def wait_for_gate(self, wj) -> None:
        await _until(lambda: wj.state is JobState.AWAITING_GRAIN, "the grain gate")
        # The job's own task is parked on the gate; anything else still in flight
        # is an outbound push, and the tests count those.
        await _until(
            lambda: all(t.done() for t in list(self.manager._tasks) if t is not wj._task),
            "the pushes sent on the way to the gate")

    async def wait_until_settled(self, wj) -> None:
        await _until(lambda: wj.state in (JobState.DONE, JobState.FAILED),
                     f"job #{wj.id} to settle (state was {wj.state})")
        await _until(lambda: all(t.done() for t in list(self.manager._tasks)),
                     "the manager's background tasks")

    # --- reading ---------------------------------------------------------

    def kinds(self) -> list[str]:
        return [kind for kind, _detail in self.calls]

    def clips(self) -> list[bool]:
        return [detail for kind, detail in self.calls if kind == "clip"]

    def encodes(self) -> list[bool]:
        return [detail for kind, detail in self.calls if kind == "encode"]

    def states_while_encoding(self) -> list[str]:
        """What the UI said the job was doing at each moment HandBrake was
        actually running — the card is the only place a stale state shows."""
        return [detail for kind, detail in self.calls if kind == "ui-state"]

    def episode_file(self, episode: int):
        folder = (self.settings.storage.tv_root / f"{SHOW} ({SHOW_YEAR})" / "Season 01")
        return folder / f"{SHOW} - S01E{episode:02d}.mkv"

    def card(self, wj) -> dict:
        return self.manager._job_dict(wj)

    def crashed_tasks(self) -> list[BaseException]:
        """Exceptions that escaped a background task the manager started. Pushes
        are fire-and-forget tasks, so this is the only place a raising notifier
        would show up at all."""
        return [t.exception() for t in self.spawned
                if t.done() and not t.cancelled() and t.exception() is not None]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "staging")
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "library")
    settings.config_dir = tmp_path / "config"     # no box-set plan to match
    settings.tmdb_api_key = None                  # no network
    settings.auto_open = False                    # the test drives the disc
    settings.auto_rip = False
    settings.auto_eject = False                   # no eject IOCTL on a test box
    settings.extract_cover_art = False
    settings.auto_retry_max = 0                   # a failure stays failed, no backoff
    settings.notify_url = "https://ntfy.example.com/blurayripper"
    settings.notify_priority = "low"              # deliberately not the ntfy default
    settings.encode.tune_grain = False            # the configured default
    settings.encode.container = "mkv"
    settings.tools.ffmpeg = ""                    # posters are optional; clips are not
    # Clustered: this box rips, and encodes 1080p itself rather than shipping it.
    settings.cluster.use_workers = True
    settings.cluster.worker_does_subs = False
    settings.cluster.local_encode_max_height = 1080
    settings.cluster.offline_alert_seconds = 300

    calls: list[tuple[str, object]] = []
    notes: list[tuple[str, str, str | None]] = []

    store = ClusteredStore()
    manager = JobManager(settings, store=store)
    disc_holder: dict[str, DiscInfo] = {"disc": _disc(2)}
    spawned: list[asyncio.Task] = []
    real_spawn = manager._spawn

    def spy_spawn(coro):
        task = real_spawn(coro)
        spawned.append(task)      # the manager drops finished tasks; keep them all
        return task

    monkeypatch.setattr(manager, "_spawn", spy_spawn)
    rig = Rig(settings, manager, store, calls, notes, spawned, disc_holder)

    # --- makemkvcon -------------------------------------------------------
    async def fake_list_drives(*, timeout=60.0):
        return [Drive(index=0, name="ASUS BW-16D1HT", device="F:",
                      disc_name=DISC_LABEL)]

    async def fake_scan_disc(source, report):
        job = Job(source=source)
        job.state = JobState.SCANNING
        job.disc = disc_holder["disc"]
        job.fingerprint = f"fp-{DISC_LABEL}"
        return job

    async def fake_rip(source, title_id, dest_dir, *, on_progress=None, on_message=None):
        calls.append(("rip", title_id))
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"title_t{title_id:02d}.mkv"
        out.write_bytes(b"RIPPED-" + str(title_id).encode())
        return [out]

    monkeypatch.setattr(manager.pipeline.makemkv, "list_drives", fake_list_drives)
    monkeypatch.setattr(manager.pipeline.makemkv, "rip_title", fake_rip)
    monkeypatch.setattr(manager.pipeline, "scan_disc", fake_scan_disc)

    # --- mkvmerge / mkvextract -------------------------------------------
    async def fake_probe(path):
        return ProbedFile(duration_seconds=EPISODE_SECONDS, video_height=1080,
                          audio_languages=["eng"], subtitles=[])

    async def fake_identify(path):
        return []           # no PGS tracks on this disc: no OCR in this story

    async def fake_height(path):
        return 1080

    monkeypatch.setattr(manager.pipeline.mkvtools, "probe", fake_probe)
    monkeypatch.setattr(manager.pipeline.mkvtools, "identify_subtitles", fake_identify)
    monkeypatch.setattr(manager.pipeline.mkvtools, "video_height", fake_height)

    # --- HandBrake --------------------------------------------------------
    async def fake_encode_clip(src, dst, *, start, duration, tune_grain):
        calls.append(("clip", tune_grain))
        assert src.exists(), "the grain preview was cut from a rip that isn't there"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"GRAINY-CLIP" if tune_grain else b"SMOOTH-CLIP")
        return dst

    async def fake_encode(src, dst, *, on_progress=None, tune_grain=None,
                          deinterlace=False):
        calls.append(("encode", tune_grain))
        for job in manager.jobs.values():           # what the UI shows right now
            calls.append(("ui-state", manager._job_dict(job)["state"]))
        if rig.fail_encode:
            raise RuntimeError(rig.fail_encode)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"ENCODED")
        return dst

    monkeypatch.setattr(manager.pipeline.handbrake, "encode_clip", fake_encode_clip)
    monkeypatch.setattr(manager.pipeline.handbrake, "encode", fake_encode)

    # --- the push itself --------------------------------------------------
    async def fake_notify(url, title, message, priority=None):
        notes.append((title, message, priority))
        return True

    monkeypatch.setattr(manager_mod, "notify", fake_notify)
    return rig


# --- (a) the grain preview gate -------------------------------------------

async def test_a_preview_disc_stops_after_the_rip_with_two_clips_to_compare(rig):
    """The whole point of the preview: it costs one short sample, not one encode.

    The gate has to sit AFTER the rip (the clips are cut from your own disc) and
    BEFORE the encode (deciding afterwards is worthless), and it has to hand the
    UI both clips — a gate that opens with nothing to look at is just a stall."""
    wj = await rig.queue_disc(2, grain_mode="preview")
    await rig.wait_for_gate(wj)

    # Ripped, sampled both ways, and NOT encoded — that ordering is the feature.
    assert rig.kinds() == ["rip", "rip", "clip", "clip"]
    assert sorted(rig.clips()) == [False, True]
    assert rig.encodes() == []

    # The two clips exist and differ, so there is really something to compare.
    off_clip, on_clip = wj.preview_off_clip, wj.preview_on_clip
    assert off_clip.exists() and on_clip.exists()
    assert off_clip.read_bytes() != on_clip.read_bytes()

    # ...and the UI is told where to play them from.
    card = rig.card(wj)
    assert card["state"] == JobState.AWAITING_GRAIN.value
    assert card["preview"]["off_clip"] == f"/api/jobs/{wj.id}/preview-clip/off"
    assert card["preview"]["on_clip"] == f"/api/jobs/{wj.id}/preview-clip/on"

    # The phone says which job is waiting, so a gate reached overnight is findable.
    grain_pushes = [n for n in rig.notes if "Grain choice" in n[0]]
    assert len(grain_pushes) == 1
    assert f"Job #{wj.id}" in grain_pushes[0][1]


@pytest.mark.parametrize("picked", [True, False])
async def test_the_grain_you_pick_is_the_grain_every_title_is_encoded_with(rig, picked):
    """The pick has to travel gate -> checkpoint -> HandBrake. Both answers are
    driven because the encoder's configured default is False: only asserting the
    "on" case would pass on a build that ignored the click and asserted nothing
    about "off"."""
    wj = await rig.queue_disc(2, grain_mode="preview")
    await rig.wait_for_gate(wj)

    rig.manager.set_grain(wj.id, picked)
    await rig.wait_until_settled(wj)

    assert wj.state is JobState.DONE, wj.error
    assert rig.encodes() == [picked, picked]      # every episode, the one choice
    assert rig.episode_file(1).exists() and rig.episode_file(2).exists()
    assert [str(rig.episode_file(1)), str(rig.episode_file(2))] == wj.outputs
    # The log says what it did, so "which grain did this file get?" is answerable.
    assert sum(1 for line in wj.log
               if f"grain={'on' if picked else 'off'}" in line) == 2


async def test_the_gate_closes_behind_the_pick_instead_of_shadowing_the_encode(rig):
    """Resolving the future is not enough: the job has to leave AWAITING_GRAIN
    there and then. It once didn't, so a single-box install (the default) showed
    "awaiting grain" with a live preview for the entire multi-hour encode — a
    question already answered, and one that clicking again only errors on.

    Driven on a one-title disc so the whole run is gate -> encode: what the card
    reports WHILE HandBrake is running is the assertion."""
    rig.settings.cluster.use_workers = False        # one box rips and encodes
    rig.set_disc(1)
    wj = await rig.queue_disc(1, grain_mode="preview")
    await rig.wait_for_gate(wj)
    assert rig.card(wj)["preview"] is not None      # the gate is up

    rig.manager.set_grain(wj.id, True)
    await rig.wait_until_settled(wj)

    assert rig.states_while_encoding() == [JobState.ENCODING.value]
    assert rig.manager._live_state(wj) is JobState.DONE
    assert rig.card(wj)["preview"] is None          # ...and gone again afterwards
    with pytest.raises(ValueError):                 # a stray second click is refused
        rig.manager.set_grain(wj.id, False)
    assert rig.encodes() == [True]                  # ...and changed nothing


async def test_the_gate_opens_once_for_the_disc_not_once_per_episode(rig):
    """The endless-loop regression, on the rip box's prep phase: grain used to be
    resolved per title, so a four-episode disc rendered a preview, parked, and did
    it again for the next title — you could never get past it. One disc, one
    question, and the answer applies to every episode on it."""
    rig.set_disc(4)
    wj = await rig.queue_disc(4, grain_mode="preview")
    await rig.wait_for_gate(wj)

    rig.manager.set_grain(wj.id, True)
    await rig.wait_until_settled(wj)

    assert wj.state is JobState.DONE, wj.error
    assert len(rig.clips()) == 2                   # one A/B sample for the disc
    assert len([n for n in rig.notes if "Grain choice" in n[0]]) == 1
    assert rig.encodes() == [True] * 4             # asked once, applied to all four
    assert [rig.episode_file(e).exists() for e in (1, 2, 3, 4)] == [True] * 4


async def test_a_decided_disc_is_never_asked_again_when_the_job_resumes(rig):
    """The choice is checkpointed per title, so restarting the box (or hitting
    Retry) picks the encode back up instead of re-opening a gate you already
    answered — the other half of the loop that made preview mode unusable."""
    wj = await rig.queue_disc(2, grain_mode="preview")
    await rig.wait_for_gate(wj)

    rig.fail_encode = "HandBrakeCLI exited 3: encoder initialisation failed"
    rig.manager.set_grain(wj.id, True)
    await rig.wait_until_settled(wj)
    assert wj.state is JobState.FAILED

    # The decision reached the store, which is what a restart reads back.
    saved = {r.id: r for r in rig.store.load_all()}[wj.id]
    assert [cp.get("tune_grain") for cp in saved.checkpoints.values()] == [True, True]

    # A fresh serve process: recover the queue, fix the encoder, click Retry.
    rig.calls.clear()
    restarted = JobManager(rig.settings, store=rig.store)
    restarted.pipeline = rig.manager.pipeline      # same stubbed tools
    restarted.recover()
    # A real failure stays parked across the restart — Retry is the operator's.
    assert restarted.jobs[wj.id].state is JobState.FAILED
    rig.fail_encode = None
    restarted.retry(wj.id)
    wj2 = restarted.jobs[wj.id]
    await _until(
        lambda: wj2.state in (JobState.DONE, JobState.FAILED, JobState.AWAITING_GRAIN),
        "the resumed job to reach the encode, the gate, or a failure")

    assert wj2.state is JobState.DONE, f"the resume stopped at {wj2.state}: {wj2.error}"
    assert rig.clips() == []                       # no second preview rendered
    assert ("rip", 0) not in rig.calls             # nor a second rip
    assert rig.encodes() == [True, True]           # the remembered choice
    assert rig.episode_file(1).exists() and rig.episode_file(2).exists()


@pytest.mark.parametrize("mode, configured_default, expected", [
    ("on", False, True),
    ("off", True, False),
])
async def test_grain_on_or_off_never_opens_the_gate(rig, mode, configured_default,
                                                    expected):
    """"On" and "off" are the answers you already know — they must not cost a
    preview render or a click, and each has to beat the opposite configured
    default, or the setting would silently decide the encode instead."""
    rig.settings.encode.tune_grain = configured_default
    wj = await rig.queue_disc(2, grain_mode=mode)
    await rig.wait_until_settled(wj)

    assert wj.state is JobState.DONE, wj.error
    assert rig.clips() == []                       # nothing was rendered to look at
    assert rig.encodes() == [expected, expected]
    assert [n for n in rig.notes if "Grain choice" in n[0]] == []
    # No preview assets were ever attached, so the card never offered a gate.
    assert (wj.preview_off_clip, wj.preview_on_clip) == (None, None)
    assert rig.card(wj)["preview"] is None


# --- (b) the alerts --------------------------------------------------------

class Ntfy:
    """The push endpoint, as the box actually reaches it: every alert here has
    travelled through the real services.notify, so what is recorded is the HTTP
    request itself — body, Title header, Priority header."""

    def __init__(self):
        self.posts: list[dict] = []
        self.attempts = 0
        self.down = False

    def titles(self) -> list[str]:
        return [p["title"].decode("utf-8") for p in self.posts]

    def priorities(self) -> list[str | None]:
        return [p["priority"] for p in self.posts]

    def bodies(self) -> list[str]:
        return [p["body"].decode("utf-8") for p in self.posts]

    def client_class(self):
        ntfy = self

        class _Resp:
            is_success = True

        class _Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, content=None, headers=None):
                ntfy.attempts += 1
                if ntfy.down:
                    raise OSError("ntfy.example.com: connection refused")
                ntfy.posts.append({"url": url, "body": content,
                                   "title": headers["Title"],
                                   "priority": headers.get("Priority")})
                return _Resp()

        return _Client


@pytest.fixture
def ntfy(rig, monkeypatch):
    """Put the real notify() back under the manager and fake only the socket."""
    endpoint = Ntfy()
    monkeypatch.setattr(manager_mod, "notify", notify_mod.notify)
    monkeypatch.setattr(notify_mod.httpx, "AsyncClient", endpoint.client_class())
    return endpoint


def _worker(worker_id: str, age: float) -> WorkerInfo:
    return WorkerInfo(worker_id=worker_id, version="v1", job_id=None, stage="idle",
                      seconds_since_seen=age)


async def _poll_store(rig, ticks: int = 3) -> None:
    """Run the serve box's real store-refresh loop for a few iterations.

    This is the loop that notices a worker finishing an encode and a worker going
    quiet, so alerts about either have to be counted from here rather than from a
    hand-called helper — a duplicate per poll is exactly the failure mode."""
    rig.settings.cluster.worker_poll_seconds = 0
    store, seen = rig.store, {"n": 0}
    real_load_all = store.load_all

    def counted():
        seen["n"] += 1
        return real_load_all()

    store.load_all = counted
    rig.manager.closing = False
    task = asyncio.create_task(rig.manager._refresh_from_store())
    try:
        await _until(lambda: seen["n"] >= ticks, "the store poll to come round")
    finally:
        rig.manager.closing = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.load_all = real_load_all
    await _until(lambda: all(t.done() for t in list(rig.manager._tasks)),
                 "the pushes the poll spawned")


async def _needs_input_then_fails(rig):
    """The unhappy half of an evening: a disc that stops for naming, then an
    encode that dies on it. Returns the job."""
    rig.fail_encode = "HandBrakeCLI exited 3: encoder initialisation failed"
    wj = await rig.queue_disc(2)
    await rig.wait_until_settled(wj)
    assert wj.state is JobState.FAILED
    return wj


async def test_each_thing_that_needs_you_pushes_exactly_one_alert(rig, ntfy):
    """Three different call sites, three moments a person has to act on: a job
    parked for naming, a job that died, and a box that stopped answering. Each
    has to arrive once — an alert per poll trains you to ignore the lot, and a
    missing one leaves an encode farm idle overnight.

    Asserted as the whole ordered list rather than as per-title counts, so a
    duplicate can't hide behind a substring match."""
    wj = await _needs_input_then_fails(rig)

    # A worker stops reporting in, then comes back. The store is polled several
    # times in each state — the alert is about the TRANSITION, not the state.
    rig.store.workers = [_worker("SteamMachine", 5)]
    await _poll_store(rig, ticks=2)
    rig.store.workers = [_worker("SteamMachine", 900)]
    await _poll_store(rig, ticks=4)
    rig.store.workers = [_worker("SteamMachine", 3)]
    await _poll_store(rig, ticks=4)

    assert ntfy.titles() == [
        f"🟡 Needs your input: {DISC_LABEL}",
        f"💿 Finished ripping: {SHOW}",
        f"❌ Failed: {JOB_NAME}",
        "🔌 Worker offline: SteamMachine",
        "✅ Worker back: SteamMachine",
    ]
    # Each one says which job/box, so it is actionable from a phone.
    bodies = ntfy.bodies()
    assert f"Job #{wj.id}" in bodies[0] and "pick titles" in bodies[0]
    assert f"Job #{wj.id} failed" in bodies[2]
    assert "Encoding stops until it's back" in bodies[3]
    # ...and every one of them went to the configured endpoint.
    assert {p["url"] for p in ntfy.posts} == {rig.settings.notify_url}


async def test_the_configured_priority_rides_along_unless_the_alert_escalates(rig, ntfy):
    """Priority is why a phone rings at 2am instead of collecting alerts silently,
    so the configured value has to reach the wire on ordinary alerts — and the two
    that mean "something is broken" have to outrank it even on a box set to a
    quiet default."""
    await _needs_input_then_fails(rig)
    rig.store.workers = [_worker("SteamMachine", 5)]
    await _poll_store(rig, ticks=2)
    rig.store.workers = [_worker("SteamMachine", 900)]
    await _poll_store(rig, ticks=2)

    assert rig.settings.notify_priority == "low"        # what the box is set to
    assert dict(zip(ntfy.titles(), ntfy.priorities())) == {
        f"🟡 Needs your input: {DISC_LABEL}": "low",
        f"💿 Finished ripping: {SHOW}": "low",
        f"❌ Failed: {JOB_NAME}": "high",
        "🔌 Worker offline: SteamMachine": "high",
    }


async def test_a_worker_finishing_a_job_is_announced_once_however_often_we_poll(
        rig, ntfy):
    """The encode ran on another box, so the only way this box learns the job is
    done is the store poll — which runs every few seconds for as long as the
    server is up. That makes the "done" push the easiest alert in the system to
    send hundreds of times."""
    rig.settings.cluster.local_encode_max_height = 0     # UHD box: workers encode
    wj = await rig.queue_disc(2)
    await _until(lambda: wj.state is JobState.READY, "the job to be queued for a worker")
    await _until(lambda: all(t.done() for t in list(rig.manager._tasks)), "the rip")
    assert rig.encodes() == []                           # this box did not encode it

    # A worker claims it, encodes it, and writes the result back to the store.
    rec = {r.id: r for r in rig.store.load_all()}[wj.id]
    rec.state = "done"
    rec.outputs = [str(rig.episode_file(1)), str(rig.episode_file(2))]
    rig.store.save(rec)

    await _poll_store(rig, ticks=5)

    done = [t for t in ntfy.titles() if t.startswith("✅ Done")]
    assert done == [f"✅ Done: {JOB_NAME}"]
    assert "2 files placed" in ntfy.bodies()[-1]
    assert wj.state is JobState.DONE                     # and the UI followed along
    assert wj.outputs == rec.outputs


@pytest.mark.parametrize("notifier", ["down", "switched off"])
async def test_a_notifier_that_is_down_or_switched_off_never_touches_the_job(
        rig, ntfy, notifier):
    """Pushes are best-effort by design: a self-hosted ntfy that is switched off,
    or a URL nobody configured (the state of every fresh install), must not cost
    you an encode. An exception escaping the push would break ripping for
    everyone who never set notifications up."""
    if notifier == "down":
        ntfy.down = True
    else:
        rig.settings.notify_url = None

    wj = await rig.queue_disc(2)
    await rig.wait_until_settled(wj)

    assert wj.state is JobState.DONE, wj.error
    assert wj.error is None
    assert rig.episode_file(1).exists() and rig.episode_file(2).exists()
    assert ntfy.posts == []                       # nothing was delivered either way
    # A refused connection was really attempted (and swallowed); an unconfigured
    # URL never opens a socket at all.
    assert (ntfy.attempts > 0) is (notifier == "down")
    # Nothing blew up on the way out: a push that raised would leave a dead task
    # (and a stack trace per alert) behind on a perfectly healthy server.
    assert rig.crashed_tasks() == []
