"""Every operator-facing message web/manager.py emits is a piece of ADVICE, and
the operator acts on it — walks to the shelf, power-cycles a drive, re-adds a
file, waits. So the thing worth testing is not that the sentence exists but that
it appears under exactly its own condition and NOT under a neighbouring one.

This suite has already shipped a hint that told someone to power-cycle a
perfectly healthy drive, so each condition here is paired with the nearest
condition it could be confused with, and both are asserted:

  drive dropped off the bus  vs  disc merely absent  vs  disc unreadable
                             vs  no volume key       vs  nothing diagnosable
  rip never finished (park)  vs  rip finished (leave it for a worker)
  disc back for a parked job vs  a genuinely new disc
  a worker that went quiet   vs  the serve box's own row in the same table
  a plan file that won't load vs no matching row vs a row that won't apply
  one exact TMDB match       vs  two             vs  TMDB unreachable
  a confident subtitle match vs  one that isn't

The MakeMKV log lines are verbatim from the production cluster (job 317's drive
dropping off the bus, jobs 334/335's NAS write dying mid-rip, and the run where
the drive block was enumerated twice).
"""

import asyncio
from dataclasses import replace

import pytest

import blurayripper.web.manager as mgr
from blurayripper import boxset_plan, presence
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, MediaKind, Title
from blurayripper.persistence import JobRecord, MemoryStore, WorkerInfo, disc_to_dict
from blurayripper.pipeline import Job, JobState
from blurayripper.services import metadata as metadata_mod
from blurayripper.services.metadata import MovieCandidate
from blurayripper.services.sub_match import Match
from blurayripper.tools.makemkv import Drive
from blurayripper.web.manager import JobManager, WebJob, _Reporter


# --- verbatim production tool output --------------------------------------

# job 317: the drive vanished off the bus mid-read. Note it prints "Failed to
# open disc" TOO — the same line an unknown AACS volume key prints.
DRIVE_OFF_THE_BUS = [
    "Error 'Posix error - No such device' occurred while reading "
    "'/BDMV/STREAM/00001.m2ts' at offset '0'",
    "Title #00100.mpls was skipped",
    "Failed to open disc",
]

# jobs 334/335: the NAS write died mid-rip. The drive and the disc were both
# fine — this is exactly the failure a retry can fix.
NAS_WRITE_DIED = [
    "Program reads data faster than it can write to disk, consider upgrading "
    "your hard drive if you see many of these messages.",
    "Error 'Posix error - Resource temporarily unavailable' occurred while "
    "writing data to '/mnt/TrueNas/.../job_334/rip/t000/X.mkv'",
    "Failed to save title 0 to file /mnt/TrueNas/.../job_334/rip/t000/X.mkv",
    "0 titles saved, 1 failed",
    "Copy complete. 0 titles saved, 1 failed.",
]

UNREADABLE_DISC = ["SCSI error - MEDIUM ERROR", "Failed to open disc"]
NO_VOLUME_KEY = ["Failed to open disc"]

# The drive whose label the auto-open watcher sees, from the duplicated DRV run.
XMAS = "The Nightmare Before Christmas"
DRIVE_MODEL_0 = "BD-RE ASUS BW-16D1HT 3.01 KLUL75I4959"
DRIVE_MODEL_1 = "BD-RE ASUS BW-16D1HT 3.10 KX1P71E4206"


class _DurableMemoryStore(MemoryStore):
    """MemoryStore that claims to be the shared Postgres, so the clustered code
    paths (park-for-review, worker ownership) are the ones under test."""

    @property
    def durable(self) -> bool:
        return True


def _settings(tmp_path, monkeypatch):
    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path / "library")
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "staging")
    monkeypatch.setattr(s, "config_dir", tmp_path / "config")
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    s.auto_retry_max = 2
    s.tmdb_api_key = None       # hermetic: nothing may reach TMDB unless a test says so
    s.notify_url = None
    return s


@pytest.fixture
def manager(tmp_path, monkeypatch):
    return JobManager(_settings(tmp_path, monkeypatch), store=MemoryStore())


@pytest.fixture
def clustered(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    s.cluster.use_workers = True
    return JobManager(s, store=_DurableMemoryStore())


def _pushes(m, monkeypatch):
    """Capture the phone alerts instead of firing them."""
    out = []
    monkeypatch.setattr(m, "_notify",
                        lambda title, message, priority=None: out.append(
                            (title, message, priority)))
    return out


def _spawns(m, monkeypatch):
    """Capture background tasks (retry timers, job runs) instead of running them."""
    out = []

    def fake(coro):
        out.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(m, "_spawn", fake)
    return out


def _tv_disc(label=XMAS, source="disc:0", device="/dev/sr0"):
    return DiscInfo(
        source=source, disc_name=label, volume_label=label, drive_device=device,
        titles=[Title(title_id=i, duration_seconds=2700, chapter_count=8,
                      source_filename=f"0000{i + 1}.mpls") for i in range(3)],
    )


def _job(m, job_id, *, source="disc:0", state=JobState.QUEUED, disc=None,
         log=(), error=None, selected=()):
    wj = WebJob(id=job_id, source=source, state=state, error=error)
    wj.log = list(log)
    wj._log_mark = 0
    wj.disc = disc
    wj.selected_title_ids = list(selected)
    job = Job(source=source)
    job.disc = disc
    job.state = state
    job.selected_title_ids = list(selected)
    wj._pipeline_job = job
    m.jobs[job_id] = wj
    return wj


def _log(wj):
    return "\n".join(wj.log)


def _worker(worker_id, job_id, age, stage="encoding"):
    return WorkerInfo(worker_id=worker_id, version="1.2.3", job_id=job_id,
                      stage=stage, seconds_since_seen=age)


# =========================================================================
# The drive is gone / the disc is gone / the disc is unreadable
# =========================================================================


def test_a_drive_that_dropped_off_the_bus_is_never_told_to_reinsert_the_disc(
        manager, monkeypatch):
    """Job 317's real log. The drive vanished, so the OS-level enumeration also
    reports the drive as empty — which is the disc-missing condition too. The
    remedies are opposite (check the drive's power vs walk over with the disc),
    so the hardware diagnosis has to win, and it must not burn a retry that can
    only fail again."""
    pushes = _pushes(manager, monkeypatch)
    spawned = _spawns(manager, monkeypatch)
    manager._drives_cache = [Drive(0, DRIVE_MODEL_0, "/dev/sr0", None)]
    manager._drives_scanned = True
    wj = _job(manager, 317, state=JobState.FAILED, log=DRIVE_OFF_THE_BUS,
              error="ProcessError: Command failed (11)")

    manager._schedule_auto_retry(wj)

    assert "dropped off the bus" in _log(wj)
    assert "power/connection" in _log(wj)
    assert "The disc isn't in the drive" not in _log(wj)
    assert manager._auto_retries == {}          # terminal: no retry spent
    assert spawned == []                        # ...and no timer armed
    assert len(pushes) == 1 and "needs attention" in pushes[0][0]
    assert "dropped off the bus" in pushes[0][1]


def test_an_empty_drive_asks_for_the_disc_back_and_says_nothing_about_hardware(
        manager, monkeypatch):
    """The drive answered fine, it just has no disc in it. Telling the operator
    to power-cycle it here is the shipped bug this file exists for."""
    pushes = _pushes(manager, monkeypatch)
    spawned = _spawns(manager, monkeypatch)
    manager._drives_cache = [Drive(1, DRIVE_MODEL_1, "/dev/sr1", None)]
    manager._drives_scanned = True
    wj = _job(manager, 40, source="disc:1", state=JobState.FAILED,
              log=["Failed to save title 0"],
              error="RuntimeError: no ripped file for title 0")

    manager._schedule_auto_retry(wj)

    assert "The disc isn't in the drive — reinsert it, then click Retry." in _log(wj)
    assert "dropped off the bus" not in _log(wj)
    assert "power/connection" not in _log(wj)
    assert manager._auto_retries == {} and spawned == []
    assert len(pushes) == 1 and "needs attention" in pushes[0][0]


def test_a_drive_that_still_holds_its_disc_spends_the_retry_instead_of_advising(
        manager, monkeypatch):
    """Same failure, disc still loaded: nothing for the operator to do, so the
    job retries itself rather than pushing an alert at 3am."""
    pushes = _pushes(manager, monkeypatch)
    spawned = _spawns(manager, monkeypatch)
    manager._drives_cache = [Drive(1, DRIVE_MODEL_1, "/dev/sr1", "HOUSE_S1_D3")]
    manager._drives_scanned = True
    wj = _job(manager, 41, source="disc:1", state=JobState.FAILED,
              log=["Failed to save title 0"],
              error="RuntimeError: no ripped file for title 0")

    assert manager._source_disc_missing(wj) is False
    manager._schedule_auto_retry(wj)

    assert "The disc isn't in the drive" not in _log(wj)
    assert "Auto-retry 1/2 in 30s…" in _log(wj)
    assert manager._auto_retries[41] == 1 and len(spawned) == 1
    assert pushes == []


def test_a_drive_the_watcher_has_never_enumerated_is_not_declared_empty(manager):
    """Right after a restart the drive cache is a guess, not an observation.
    "Not in the cache" only means "not attached" once a scan has succeeded —
    before that, claiming the disc is missing is a fabricated diagnosis."""
    wj = _job(manager, 42, source="disc:3", state=JobState.FAILED)
    manager._drives_cache = []

    manager._drives_scanned = False
    assert manager._source_disc_missing(wj) is False   # genuinely unknown
    manager._drives_scanned = True
    assert manager._source_disc_missing(wj) is True    # drive 3 isn't attached


def test_a_nas_write_that_died_mid_rip_is_retried_not_blamed_on_the_drive(
        manager, monkeypatch):
    """Jobs 334/335: the CIFS mount went away while MakeMKV was writing. Drive
    fine, disc fine, and the next attempt often works — so this must fall
    through to the retry budget rather than collecting a hardware diagnosis."""
    spawned = _spawns(manager, monkeypatch)
    manager._drives_cache = [Drive(0, DRIVE_MODEL_0, "/dev/sr0", "HOUSE_S1_D3")]
    manager._drives_scanned = True
    wj = _job(manager, 334, state=JobState.FAILED, log=NAS_WRITE_DIED,
              error="ProcessError: Command failed (1)")

    assert manager._rip_failure_hint(wj) is None
    manager._schedule_auto_retry(wj)

    assert "Auto-retry 1/2 in 30s…" in _log(wj)
    assert manager._auto_retries[334] == 1 and len(spawned) == 1
    assert "dropped off the bus" not in _log(wj)
    assert "Disc read error" not in _log(wj)


def test_the_three_makemkv_diagnoses_give_three_different_remedies(manager):
    """The drive being gone, the disc being unreadable, and the disc having no
    volume key all end the same run. They need three different sentences, and
    only the last one is worth retrying — the real log for the first case even
    contains the third case's line, so precedence matters."""
    offline = manager._rip_failure_hint(
        _job(manager, 1, state=JobState.FAILED, log=DRIVE_OFF_THE_BUS))
    unreadable = manager._rip_failure_hint(
        _job(manager, 2, state=JobState.FAILED, log=UNREADABLE_DISC))
    unopenable = manager._rip_failure_hint(
        _job(manager, 3, state=JobState.FAILED, log=NO_VOLUME_KEY))

    assert len({offline[0], unreadable[0], unopenable[0]}) == 3
    assert (offline[1], unreadable[1], unopenable[1]) == (True, True, False)
    assert "dropped" in offline[0] and "Clean the disc" not in offline[0]
    assert "Clean the disc" in unreadable[0] and "dropped off the bus" not in unreadable[0]
    assert "volume key" in unopenable[0] and "volume key" not in offline[0]


# =========================================================================
# The auto-retry budget: spent, then exhausted
# =========================================================================


def test_the_retry_budget_counts_down_backs_off_and_then_reports_itself_spent(
        manager, monkeypatch, tmp_path):
    """Each attempt says which one it is out of how many and how long the wait
    is, so "it's been quiet for four minutes" is answerable from the log. When
    the budget runs out the job stops retrying and escalates once."""
    src = tmp_path / "Movie (2019).mkv"
    src.write_bytes(b"x")                       # present: no source-file hint
    pushes = _pushes(manager, monkeypatch)
    spawned = _spawns(manager, monkeypatch)
    wj = _job(manager, 50, source=str(src), state=JobState.FAILED,
              error="RuntimeError: transient encode blip")

    manager._schedule_auto_retry(wj)
    manager._schedule_auto_retry(wj)
    manager._schedule_auto_retry(wj)

    assert wj.log[0].endswith("Auto-retry 1/2 in 30s…")
    assert wj.log[1].endswith("Auto-retry 2/2 in 60s…")   # exponential backoff
    assert wj.log[2].endswith("Auto-retry exhausted (2) — hit Retry to try again.")
    assert len(wj.log) == 3
    assert manager._auto_retries[50] == 2                 # never climbs past the max
    assert len(spawned) == 2                              # the third armed nothing
    assert [p[2] for p in pushes] == ["high"]             # one escalation, at the end
    assert pushes[0][0].startswith("❌ Failed:")
    assert "transient encode blip" in pushes[0][1]


def test_an_unopenable_disc_still_gets_its_retries_but_says_why(manager, monkeypatch):
    """"Failed to open disc" is ambiguous — a dead drive and a missing AACS key
    print it alike. Reporting it must not cancel the retry the transient case
    deserves, so this is the one diagnosis that is advice AND a retry."""
    spawned = _spawns(manager, monkeypatch)
    manager._drives_cache = [Drive(0, DRIVE_MODEL_0, "/dev/sr0", "GHOST IN THE SHELL")]
    manager._drives_scanned = True
    wj = _job(manager, 51, state=JobState.FAILED, log=NO_VOLUME_KEY,
              error="ProcessError: Command failed (12)")

    manager._schedule_auto_retry(wj)

    assert "volume key" in _log(wj) and "AACS dump" in _log(wj)
    assert "Auto-retry 1/2 in 30s…" in _log(wj)
    assert manager._auto_retries[51] == 1 and len(spawned) == 1


# =========================================================================
# A job parked for review, and the disc coming back for it
# =========================================================================


def _persisted(store, job_id, *, state, source, label, checkpoints, selected=(0,)):
    store.save(JobRecord(
        id=job_id, source=source, state=state,
        disc=disc_to_dict(_tv_disc(label=label, source=source)),
        selected_title_ids=list(selected), checkpoints=checkpoints,
    ))


def test_a_ready_job_whose_rip_never_finished_is_parked_with_instructions(
        clustered, monkeypatch):
    """Nobody owns this job: serve skips READY as worker-owned, and a worker
    refuses it because there is no ripped file to encode. Serve takes it back —
    but it must NOT spin the drive up unasked, because the disc may not even be
    in the machine. So it parks, and the message has to say both what to do and
    that nothing was destroyed."""
    resumed = []
    monkeypatch.setattr(clustered, "_resume", lambda wj: resumed.append(wj.id))
    _persisted(clustered.store, 9, state="ready", source="disc:0",
               label="HOUSE_S1_D3", checkpoints={})

    assert clustered.recover() == 0
    wj = clustered.jobs[9]

    assert wj.state is JobState.FAILED and wj._pipeline_job.state is JobState.FAILED
    assert "Rip never completed" in wj.error
    assert resumed == []                                  # nothing was re-ripped
    line = wj.log[-1]
    assert "Needs review" in line and "Rip this disc" in line
    assert "Nothing has been read or re-ripped." in line
    # The park is durable, so the next restart doesn't have to work it out again.
    assert [r.state for r in clustered.store.load_all()] == ["failed"]


def test_a_ready_job_whose_rip_did_finish_is_left_alone_for_a_worker(
        clustered, monkeypatch):
    """The same state, the same source — the only difference is that the rip
    checkpoint exists. That job is genuinely claimable, so parking it would
    stall real work and tell the operator to go and find a disc for nothing."""
    resumed = []
    monkeypatch.setattr(clustered, "_resume", lambda wj: resumed.append(wj.id))
    _persisted(clustered.store, 10, state="ready", source="disc:0",
               label="HOUSE_S1_D3",
               checkpoints={"0": {"ripped_path": "job_10/rip/t000/X.mkv"}})

    assert clustered.recover() == 0
    wj = clustered.jobs[10]

    assert wj.state is JobState.READY
    assert "Needs review" not in _log(wj)
    assert wj.error is None and resumed == []


def test_the_returning_disc_is_named_by_the_drive_the_operator_gave_it(
        manager, monkeypatch):
    """With a shelf of parked discs, "put it back" is only actionable if the
    message says which drive it landed in. The friendly name wins when one is
    set, so the log matches the label stuck on the machine."""
    added = []
    monkeypatch.setattr(manager, "add_disc", lambda s: added.append(s))
    _pushes(manager, monkeypatch)
    manager.drive_names.set("/dev/sr1", "Top drive")
    wj = _job(manager, 12, state=JobState.FAILED, disc=_tv_disc(label="HOUSE_S1_D3"))

    manager._auto_open([Drive(1, DRIVE_MODEL_1, "/dev/sr1", "HOUSE_S1_D3")])

    assert added == []                       # the parked job IS the next step
    assert wj.source == "disc:1"             # repointed at where it actually is
    assert "Disc is back in Top drive." in wj.log[-1]
    assert "No new job was created for this disc." in wj.log[-1]


def test_an_unnamed_drive_is_identified_by_its_model_then_by_its_selector(
        manager, monkeypatch):
    """Nobody names their drives on day one. The fallback still has to identify
    the drive: the model string, or failing that the selector — never a blank."""
    monkeypatch.setattr(manager, "add_disc", lambda s: None)
    _pushes(manager, monkeypatch)
    named = _job(manager, 13, state=JobState.FAILED, disc=_tv_disc(label="HOUSE_S1_D3"))
    manager._auto_open([Drive(1, DRIVE_MODEL_1, "/dev/sr1", "HOUSE_S1_D3")])
    assert f"Disc is back in {DRIVE_MODEL_1}." in named.log[-1]

    manager._last_disc.clear()
    bare = _job(manager, 14, state=JobState.FAILED, disc=_tv_disc(label="HOUSE_S1_D4"))
    manager._auto_open([Drive(2, "", None, "HOUSE_S1_D4")])
    assert "Disc is back in disc:2." in bare.log[-1]


def test_the_disc_back_alert_says_how_many_parked_jobs_that_label_matches(
        manager, monkeypatch):
    """Every disc in a box set carries the same volume label, so the disc that
    just went in may be any of several parked jobs. One match names the job;
    several say how many, because the alert can't pick for you."""
    monkeypatch.setattr(manager, "add_disc", lambda s: None)
    pushes = _pushes(manager, monkeypatch)
    first = _job(manager, 20, state=JobState.FAILED, disc=_tv_disc(label="HOUSE"))
    manager._auto_open([Drive(1, DRIVE_MODEL_1, "/dev/sr1", "HOUSE")])

    manager._last_disc.clear()   # the tray was opened and closed again
    second = _job(manager, 21, state=JobState.FAILED, disc=_tv_disc(label="HOUSE"))
    manager._auto_open([Drive(1, DRIVE_MODEL_1, "/dev/sr1", "HOUSE")])

    assert [p[0] for p in pushes] == ["💿 Disc back for review: HOUSE"] * 2
    assert pushes[0][1] == "Job #20 was waiting for this disc."
    assert pushes[1][1] == "2 parked jobs match this disc."
    # The alert can't pick, so BOTH candidates have to be pointed at the drive —
    # otherwise "2 parked jobs match" leaves you with nothing to click.
    assert "Disc is back in" in first.log[-1] and first.source == "disc:1"
    assert "Disc is back in" in second.log[-1] and second.source == "disc:1"


def test_a_parked_job_that_already_has_its_rip_lets_the_disc_start_a_new_one(
        manager, monkeypatch):
    """A job that failed AFTER ripping doesn't need the disc any more. If it
    claimed the returning disc anyway, re-inserting it to rip something else
    would silently do nothing at all."""
    added = []
    monkeypatch.setattr(manager, "add_disc", lambda s: added.append(s))
    pushes = _pushes(manager, monkeypatch)
    wj = _job(manager, 22, state=JobState.FAILED, selected=(0,),
              disc=_tv_disc(label="HOUSE_S1_D3"))
    wj._pipeline_job.checkpoints = {0: {"ripped_path": "job_22/rip/t000/X.mkv"}}

    manager._auto_open([Drive(1, DRIVE_MODEL_1, "/dev/sr1", "HOUSE_S1_D3")])

    assert added == ["disc:1"]                 # treated as a new disc, correctly
    assert wj.source == "disc:0"               # the ripped job was left alone
    assert "Disc is back in" not in _log(wj)
    assert pushes == []


# =========================================================================
# The same disc must not be queued twice
# =========================================================================


def test_a_drive_block_enumerated_twice_in_one_scan_still_queues_one_job(
        manager, monkeypatch):
    """makemkvcon has emitted its whole DRV block twice in a single invocation,
    which put duplicate drives in the panel. Two rows for one physical drive
    must not become two jobs racing each other for it."""
    added = []
    monkeypatch.setattr(manager, "add_disc", lambda s: added.append(s))
    loaded = Drive(0, DRIVE_MODEL_0, "/dev/sr0", XMAS)
    empty = Drive(1, DRIVE_MODEL_1, "/dev/sr1", "")

    manager._auto_open([loaded, empty, replace(loaded), replace(empty)])

    assert added == ["disc:0"]


def test_a_disc_left_in_the_drive_is_not_re_queued_on_every_poll(
        manager, monkeypatch):
    """The watcher re-enumerates every few seconds and the disc is still there.
    Only the first sighting is new."""
    added = []
    monkeypatch.setattr(manager, "add_disc", lambda s: added.append(s))
    drives = [Drive(0, DRIVE_MODEL_0, "/dev/sr0", XMAS)]

    for _ in range(3):
        manager._auto_open(drives)

    assert added == ["disc:0"]


def test_a_disc_still_being_scanned_is_not_queued_a_second_time(
        manager, monkeypatch):
    """Between add_disc and the scan finishing, the job has no disc info at all,
    so the name-based guard cannot see it — and makemkvcon holds the drive
    during a scan, so the watcher's probe reads the tray as changed. The
    in-flight-scan guard is the only thing covering that window."""
    added = []
    monkeypatch.setattr(manager, "add_disc", lambda s: added.append(s))
    _job(manager, 30, source="disc:0", state=JobState.SCANNING)   # no .disc yet

    assert manager._disc_has_active_job(XMAS) is False   # the gap it has to cover
    assert manager._scan_pending_for("disc:0") is True
    manager._auto_open([Drive(0, DRIVE_MODEL_0, "/dev/sr0", XMAS)])

    assert added == []


def test_a_scan_that_already_failed_does_not_block_that_drive_forever(manager):
    """The in-flight guard has to end somewhere, or one failed scan makes the
    drive permanently unusable for auto-open."""
    wj = _job(manager, 31, source="disc:0", state=JobState.SCANNING)
    assert manager._scan_pending_for("disc:0") is True

    wj.state = wj._pipeline_job.state = JobState.FAILED
    assert manager._scan_pending_for("disc:0") is False


# =========================================================================
# A stalled job
# =========================================================================


def test_a_job_counts_as_stalled_only_when_its_own_worker_stops_heartbeating(
        manager):
    """"Encoding" with nobody encoding it is the state that looks healthy and
    is not. The verdict has to key off the heartbeat of the worker holding THIS
    job — a fresh heartbeat from a worker busy with a different job proves
    nothing about this one."""
    wj = _job(manager, 60, state=JobState.ENCODING)
    stale = manager.settings.cluster.stale_after_seconds

    manager._workers_cache = [_worker("encoder-A", 60, stale)]
    assert manager._is_stalled(wj, JobState.ENCODING) is False
    manager._workers_cache = [_worker("encoder-A", 60, stale + 1)]
    assert manager._is_stalled(wj, JobState.ENCODING) is True
    manager._workers_cache = [_worker("encoder-B", 61, 1.0)]
    assert manager._is_stalled(wj, JobState.ENCODING) is True


def test_requeue_refuses_a_job_a_live_worker_is_still_encoding(manager):
    """Requeue cancels work and re-runs it. Aimed at a job that is genuinely
    progressing it would throw away a live encode, so the guard has to say no
    and leave the job exactly as it was."""
    wj = _job(manager, 62, state=JobState.ENCODING)
    manager._workers_cache = [_worker("encoder-A", 62, 5.0)]

    with pytest.raises(ValueError) as err:
        manager.requeue(62)

    assert "Job 62 isn't stalled — nothing to requeue." in str(err.value)
    assert wj.state is JobState.ENCODING
    assert _log(wj) == ""


def test_requeuing_a_stalled_encode_keeps_the_rip_and_drops_the_dead_claim(
        manager, monkeypatch):
    """The worker died holding the claim. Recovery must reuse the finished rip
    (re-ripping a UHD disc is an hour), release the stale claim so another
    worker can take it, and leave a durable READY the cluster can see."""
    spawned = _spawns(manager, monkeypatch)
    released = []
    monkeypatch.setattr(manager.store, "release", lambda jid: released.append(jid))
    wj = _job(manager, 63, state=JobState.ENCODING, selected=(0,))
    wj._pipeline_job.checkpoints = {0: {"ripped_path": "job_63/rip/t000/X.mkv"}}
    manager._workers_cache = []                 # nothing is heartbeating it

    manager.requeue(63)

    assert wj.state is JobState.READY and wj._pipeline_job.state is JobState.READY
    assert released == [63]
    assert wj._pipeline_job.checkpoints == {0: {"ripped_path": "job_63/rip/t000/X.mkv"}}
    assert wj.log[-1].endswith("Requeued (was stalled) — resuming from checkpoints.")
    assert [r.state for r in manager.store.load_all()] == ["ready"]
    assert len(spawned) == 1                    # exactly one re-run, not one per click


def test_requeue_all_only_touches_the_jobs_that_are_actually_stuck(
        manager, monkeypatch):
    """The bulk button is aimed at a shelf of frozen jobs after a worker box
    rebooted. It must skip the ones still being worked — not just leave their
    state alone, but not re-run them and not write a misleading "Requeued" line
    into the log of an encode that is in fact still going."""
    spawned = _spawns(manager, monkeypatch)
    stuck = _job(manager, 64, state=JobState.ENCODING)
    live = _job(manager, 65, state=JobState.ENCODING)
    manager._workers_cache = [_worker("encoder-A", 65, 1.0)]

    assert manager.requeue_stalled() == 1
    assert stuck.state is JobState.READY
    assert stuck.log[-1].endswith("Requeued (was stalled) — resuming from checkpoints.")
    assert live.state is JobState.ENCODING
    assert _log(live) == ""
    assert len(spawned) == 1                 # only the stuck one was re-run


# =========================================================================
# A worker going offline and coming back
# =========================================================================


def test_a_worker_going_quiet_escalates_and_quotes_the_configured_window(
        manager, monkeypatch):
    """The alert has to state the window it judged by, because "no heartbeat"
    means something different at 5 minutes than at 7. Encoding stops entirely
    while a worker is down, so this is the one that rings the phone."""
    pushes = _pushes(manager, monkeypatch)
    manager.settings.cluster.offline_alert_seconds = 420
    manager._worker_watch = presence.PresenceWatch(threshold_seconds=420)

    manager._workers_cache = [_worker("encoder-A", 70, 5.0)]
    manager._check_worker_presence()
    assert pushes == []                          # seen alive: nothing to say

    manager._workers_cache = [_worker("encoder-A", 70, 421.0)]
    manager._check_worker_presence()

    assert len(pushes) == 1
    assert pushes[0][0] == "🔌 Worker offline: encoder-A"
    assert "No heartbeat for over 7m." in pushes[0][1]
    assert "Encoding stops until it's back." in pushes[0][1]
    assert pushes[0][2] == "high"


def test_the_worker_coming_back_is_announced_once_and_not_escalated(
        manager, monkeypatch):
    """Recovery is good news: say it once, at normal priority, and then stop —
    a per-poll "it's back" is how alerting gets muted."""
    pushes = _pushes(manager, monkeypatch)
    manager.settings.cluster.offline_alert_seconds = 300
    manager._worker_watch = presence.PresenceWatch(threshold_seconds=300)

    for age in (5.0, 900.0, 4.0, 3.0):
        manager._workers_cache = [_worker("encoder-A", 70, age)]
        manager._check_worker_presence()

    assert [(p[0], p[2]) for p in pushes] == [
        ("🔌 Worker offline: encoder-A", "high"),
        ("✅ Worker back: encoder-A", None),
    ]
    assert pushes[1][1] == "It's reporting in again."


def test_the_serve_box_own_heartbeat_is_never_reported_as_a_worker(
        manager, monkeypatch):
    """The rip box registers in the SAME workers table, so the workers can spot
    IT dropping. Restarting the server would otherwise alert the server about
    itself — and about a machine nobody can go and fix from that message."""
    pushes = _pushes(manager, monkeypatch)
    manager.settings.cluster.offline_alert_seconds = 300
    manager._worker_watch = presence.PresenceWatch(threshold_seconds=300)
    serve = presence.serve_id("brr-serve")

    manager._workers_cache = [_worker(serve, None, 5.0, "serving"),
                              _worker("encoder-A", 70, 5.0)]
    manager._check_worker_presence()
    manager._workers_cache = [_worker(serve, None, 9999.0, "serving"),
                              _worker("encoder-A", 70, 5.0)]
    manager._check_worker_presence()
    assert pushes == []                          # the serve row aged out: silence

    manager._workers_cache = [_worker(serve, None, 9999.0, "serving"),
                              _worker("encoder-A", 70, 9999.0)]
    manager._check_worker_presence()
    assert [p[0] for p in pushes] == ["🔌 Worker offline: encoder-A"]


# =========================================================================
# A job removed while it is running
# =========================================================================


async def test_removing_a_running_job_says_it_is_cancelling_and_leaves_nothing(
        manager):
    """Remove on a live job kills the rip subprocess, which takes a moment —
    so the UI has to say what it is doing rather than appearing to hang. And
    "removed" has to mean removed: staging gone, store row gone."""
    staging = manager.settings.storage.staging_dir / "job_70"
    staging.mkdir(parents=True)
    (staging / "t000.mkv").write_bytes(b"x")
    wj = _job(manager, 70, state=JobState.RIPPING)
    manager._persist(wj)
    started = asyncio.Event()

    async def wedged_rip():
        started.set()
        await asyncio.sleep(3600)

    wj._task = asyncio.create_task(wedged_rip())
    await started.wait()

    await manager.remove(70)

    assert "Removing — cancelling in-flight work…" in wj.log[-1]
    assert wj._task.cancelled()                  # the rip really was killed
    assert 70 not in manager.jobs
    assert manager.store.load_all() == []
    assert not staging.exists()


async def test_removing_a_parked_job_does_not_claim_to_cancel_anything(manager):
    """Nothing is running, so announcing a cancellation would be a lie in the
    log the next person reads."""
    wj = _job(manager, 71, state=JobState.FAILED)
    manager._persist(wj)

    await manager.remove(71)

    assert "cancelling in-flight work" not in _log(wj)
    assert 71 not in manager.jobs
    assert manager.store.load_all() == []


# =========================================================================
# Source remapping across machines
# =========================================================================


def test_a_source_path_left_over_from_the_old_server_says_re_add_it_here(
        manager, monkeypatch, tmp_path):
    """A file job stores an ABSOLUTE source, so moving the server leaves it
    pointing at the old box's drive letter. Retry can never fix that — and a
    mapped drive letter from a person's own session was never visible to the
    server in the first place."""
    pushes = _pushes(manager, monkeypatch)
    spawned = _spawns(manager, monkeypatch)
    # tmp_path.name keeps the path unique per run: this box really does have a
    # Z: mapping, and a path that happened to EXIST would silently take the
    # "file is fine" branch and make the test vacuous.
    wj = _job(manager, 80, source=rf"Z:\Media\Rips\{tmp_path.name}\Movie (2019).mkv",
              state=JobState.FAILED, error="FileNotFoundError: [Errno 2]")

    manager._schedule_auto_retry(wj)

    hint = wj.log[-1]
    assert "The source file isn't reachable" in hint
    assert "retrying won't help" in hint
    assert "a path from another machine" in hint
    assert "re-add the file from its path here" in hint
    assert "Put the file back" not in hint
    assert manager._auto_retries == {} and spawned == []
    assert len(pushes) == 1 and "needs attention" in pushes[0][0]


def test_a_local_source_that_merely_vanished_asks_for_the_file_back(
        manager, monkeypatch, tmp_path):
    """Same absent file, but a path this server genuinely owns — here putting it
    back and retrying IS the fix, so telling the operator to delete the job and
    re-add it would be gratuitous work."""
    pushes = _pushes(manager, monkeypatch)
    spawned = _spawns(manager, monkeypatch)
    # tmp_path.name keeps it unique: /mnt/TrueNas IS a real mount on the rip
    # box, and a path that happened to exist would make this test vacuous.
    wj = _job(manager, 81, source=f"/mnt/TrueNas/rips/{tmp_path.name}/Movie (2019).mkv",
              state=JobState.FAILED, error="FileNotFoundError: [Errno 2]")

    manager._schedule_auto_retry(wj)

    hint = wj.log[-1]
    assert "The source file isn't reachable" in hint
    assert "Put the file back" in hint
    assert "another machine" not in hint
    assert "re-add the file" not in hint
    # Same terminal treatment as the foreign path: no retry spent, no timer.
    assert manager._auto_retries == {} and spawned == []
    assert len(pushes) == 1 and "needs attention" in pushes[0][0]


def test_a_source_that_is_present_or_a_disc_gets_no_file_hint_at_all(
        manager, tmp_path):
    """The hint must be about the file actually being gone, not about the job
    having failed — otherwise every encode failure reads as a missing file."""
    src = tmp_path / "Movie (2019).mkv"
    src.write_bytes(b"x")

    present = _job(manager, 82, source=str(src), state=JobState.FAILED)
    from_disc = _job(manager, 83, source="disc:0", state=JobState.FAILED)

    assert manager._source_file_missing(present) is None
    assert manager._source_file_missing(from_disc) is None


def test_moving_a_job_to_another_drive_repoints_what_would_be_ejected(manager):
    """Re-pointing is what you do when one drive can't read a disc another can.
    The eject target has to move with it, or finishing the rip pops the tray on
    a different disc — and the message has to say the rip isn't automatic."""
    disc = _tv_disc(label="HOUSE_S1_D3", source="disc:0", device="/dev/sr0")
    wj = _job(manager, 84, source="disc:0", state=JobState.FAILED, disc=disc)
    manager._drives_cache = [Drive(0, DRIVE_MODEL_0, "/dev/sr0", None),
                             Drive(1, DRIVE_MODEL_1, "/dev/sr1", "HOUSE_S1_D3")]

    manager.change_source(84, "disc:1")

    assert wj.source == "disc:1" and wj._pipeline_job.source == "disc:1"
    assert wj.disc.source == "disc:1"
    assert wj.disc.drive_device == "/dev/sr1"
    assert wj.log[-1].endswith(
        "Rip source changed to disc:1 — click Retry to rip from it.")


def test_the_drive_cannot_be_changed_out_from_under_a_running_job(manager):
    """makemkvcon is holding the drive. Swapping the selector mid-rip points the
    running job at a drive it never opened."""
    wj = _job(manager, 85, source="disc:0", state=JobState.RIPPING)

    with pytest.raises(ValueError) as err:
        manager.change_source(85, "disc:1")

    assert "Can't change the drive while the job is running." in str(err.value)
    assert wj.source == "disc:0"


def test_a_bare_path_is_rejected_as_a_drive_selector(manager):
    """"Change drive" takes disc:N, not the path someone pasted from Explorer;
    makemkvcon can't parse the latter and fails much further downstream."""
    wj = _job(manager, 86, source="disc:0", state=JobState.FAILED)

    with pytest.raises(ValueError) as err:
        manager.change_source(86, r"Z:\Media\Rips")

    assert "Source must be a selector like 'disc:1'." in str(err.value)
    assert wj.source == "disc:0"


# =========================================================================
# The box-set plan: matched, and the three ways it doesn't
# =========================================================================


HOUSE_PLAN = ("disc_label,show,year,season,first_episode,episodes\n"
              "HOUSE_S1_D3,House,2004,1,9,\n")


def _plan(m, text):
    boxset_plan.save_plan(m.settings, text)


def _planned_job(m, job_id=90, label="HOUSE_S1_D3"):
    wj = _job(m, job_id, state=JobState.SCANNING, disc=_tv_disc(label=label))
    return wj, wj._pipeline_job


async def test_a_plan_that_loaded_no_rows_names_the_file_it_read(manager):
    """"The CSV isn't working" and "no row covers this disc" look identical from
    the outside — a disc that just quietly parks at the naming gate. Naming the
    path is what turns it into something checkable."""
    wj, job = _planned_job(manager)

    assert await manager._try_planned_tv(wj, job, _Reporter(manager, wj)) is False

    assert str(boxset_plan.plan_path(manager.settings)) in _log(wj)
    assert "no rows loaded" in _log(wj)
    assert "naming this one manually" in _log(wj)


async def test_a_plan_that_covers_other_discs_quotes_the_label_it_did_not_match(
        manager):
    """The plan loaded fine, it just doesn't mention this disc. The operator
    needs the label the matcher actually saw, since that is what their pattern
    has to cover."""
    _plan(manager, "disc_label,show,year,season,first_episode,episodes\n"
                   "THE_OFFICE_S3_D2,The Office,2005,3,9,4\n")
    wj, job = _planned_job(manager)

    assert await manager._try_planned_tv(wj, job, _Reporter(manager, wj)) is False

    assert "1 row(s) loaded but none match the disc label “HOUSE_S1_D3”" in _log(wj)
    assert "trailing * covers a whole set" in _log(wj)
    assert "no rows loaded" not in _log(wj)


async def test_a_matched_row_that_disagrees_with_the_disc_refuses_to_guess(
        manager, monkeypatch):
    """The row says six episodes and the disc has three, so one of them is
    wrong. Applying it anyway would name three files as the wrong episodes and
    rip the whole disc unattended — so it parks, and it says which numbers
    disagreed."""
    started = []
    monkeypatch.setattr(manager, "_start_job", lambda wj: started.append(wj.id))
    _plan(manager, "disc_label,show,year,season,first_episode,episodes\n"
                   "HOUSE_S1_D3,House,2004,1,9,6\n")
    wj, job = _planned_job(manager)

    assert await manager._try_planned_tv(wj, job, _Reporter(manager, wj)) is False

    assert "Box-set plan matched “HOUSE_S1_D3” but didn't apply" in _log(wj)
    assert "plan expects 6 episode(s) but the disc has 3" in _log(wj)
    assert "Confirm this one manually." in _log(wj)
    assert wj.assignments == {} and started == []
    assert wj.state is JobState.SCANNING     # still parked, not queued to rip


async def test_a_matching_row_names_every_episode_and_starts_ripping_unattended(
        manager, monkeypatch):
    """The CSV is the operator's up-front statement of what these discs are, so
    a clean match is consent to skip the naming gate entirely."""
    started = []
    monkeypatch.setattr(manager, "_start_job", lambda wj: started.append(wj.id))
    _plan(manager, HOUSE_PLAN)
    wj, job = _planned_job(manager)
    pushes = _pushes(manager, monkeypatch)

    assert await manager._try_planned_tv(wj, job, _Reporter(manager, wj)) is True

    assert [(a.show_or_movie_title, a.season, a.episode, a.kind)
            for a in wj.assignments.values()] == [
        ("House", 1, 9, MediaKind.TV),
        ("House", 1, 10, MediaKind.TV),
        ("House", 1, 11, MediaKind.TV),
    ]
    assert wj.selected_title_ids == [0, 1, 2]
    assert wj.state is JobState.READY and job.state is JobState.READY
    assert started == [90]
    assert "Box-set plan: House S01E09–E11 from 3 title(s) — ripping automatically." \
        in _log(wj)
    assert "Use Rename if the show is wrong" in _log(wj)
    assert len(pushes) == 1                  # one alert for the disc, not one per title
    assert pushes[0][0] == "⏩ Auto-ripping: House S01E09–E11"


async def test_a_plan_row_without_a_year_warns_that_plex_may_pick_a_remake(
        manager, monkeypatch):
    """A yearless row still rips — but the library folder gets no year either,
    which is how a 1968 box set ended up matched to the 2010 remake. Say it at
    rip time, when the CSV can still be fixed cheaply."""
    monkeypatch.setattr(manager, "_start_job", lambda wj: None)
    _pushes(manager, monkeypatch)
    _plan(manager, "disc_label,show,year,season,first_episode,episodes\n"
                   "HAWAII_FIVE_0_S1_*,Hawaii Five-O,,1,1,\n")
    yearless, job = _planned_job(manager, 91, label="HAWAII_FIVE_0_S1_D1")

    assert await manager._try_planned_tv(yearless, job, _Reporter(manager, yearless)) is True
    assert "has no year" in _log(yearless)
    assert "Plex may match a remake instead of the original" in _log(yearless)

    _plan(manager, "disc_label,show,year,season,first_episode,episodes\n"
                   "HAWAII_FIVE_0_S1_*,Hawaii Five-O,1968,1,1,\n")
    dated, job2 = _planned_job(manager, 92, label="HAWAII_FIVE_0_S1_D1")

    assert await manager._try_planned_tv(dated, job2, _Reporter(manager, dated)) is True
    assert "Plex may match a remake" not in _log(dated)


# =========================================================================
# Auto-identify: giving up beats guessing
# =========================================================================


def _fake_tmdb(monkeypatch, *, results=None, error=None):
    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def search_movie(self, query, year=None):
            if error is not None:
                raise error
            return list(results or [])

    monkeypatch.setattr(metadata_mod, "TMDBClient", FakeClient)


def _movie_job(m, job_id=100, label="Dumbo"):
    disc = DiscInfo(source="disc:0", disc_name=label, volume_label=label,
                    titles=[Title(title_id=0, duration_seconds=6762,
                                  chapter_count=12, source_filename="00002.mpls")])
    wj = _job(m, job_id, state=JobState.AWAITING_MAPPING, disc=disc)
    return wj, wj._pipeline_job


async def test_two_films_of_the_same_name_park_the_disc_instead_of_picking_one(
        manager, monkeypatch):
    """Dumbo is 1941 and 2019. Auto-rip picking either would place a whole
    unattended rip under the wrong title, so an ambiguous name has to stop and
    say how many candidates it found."""
    started = []
    monkeypatch.setattr(manager, "_start_job", lambda wj: started.append(wj.id))
    manager.settings.auto_rip = True
    manager.settings.tmdb_api_key = "test-key"
    _fake_tmdb(monkeypatch, results=[MovieCandidate(1, "Dumbo", 1941, ""),
                                     MovieCandidate(2, "Dumbo", 2019, "")])
    wj, job = _movie_job(manager)

    ok = await manager._try_auto_rip(wj, job, 0, _Reporter(manager, wj))

    assert ok is False
    assert "Auto-rip: 2 exact matches for 'Dumbo'" in _log(wj)
    assert "ambiguous, leaving for you to confirm" in _log(wj)
    assert wj.assignments == {} and started == []
    assert wj.state is JobState.AWAITING_MAPPING


async def test_one_exact_match_among_near_misses_rips_without_asking(
        manager, monkeypatch):
    """The guard is about EXACT title matches, not about how many results TMDB
    returned — otherwise every film with a sequel in the catalogue needs a
    human."""
    started = []
    monkeypatch.setattr(manager, "_start_job", lambda wj: started.append(wj.id))
    _pushes(manager, monkeypatch)
    manager.settings.auto_rip = True
    manager.settings.tmdb_api_key = "test-key"
    _fake_tmdb(monkeypatch, results=[MovieCandidate(1, "Dumbo", 1941, ""),
                                     MovieCandidate(2, "Dumbo Drop", 1995, "")])
    wj, job = _movie_job(manager)

    ok = await manager._try_auto_rip(wj, job, 0, _Reporter(manager, wj))

    assert ok is True
    assert [(a.show_or_movie_title, a.year, a.kind) for a in wj.assignments.values()] \
        == [("Dumbo", 1941, MediaKind.MOVIE)]
    assert wj.state is JobState.READY and started == [100]
    assert "Auto-rip: matched “Dumbo (1941)” — ripping automatically." in _log(wj)
    assert "Use Rename if it's wrong" in _log(wj)


async def test_a_tmdb_outage_reads_as_an_outage_not_as_an_ambiguous_name(
        manager, monkeypatch):
    """Both leave the disc at the naming gate, but only one is worth
    investigating — and "0 exact matches" would be a fabricated result."""
    monkeypatch.setattr(manager, "_start_job", lambda wj: None)
    manager.settings.auto_rip = True
    manager.settings.tmdb_api_key = "test-key"
    _fake_tmdb(monkeypatch, error=ConnectionError("proxy returned 502"))
    wj, job = _movie_job(manager)

    ok = await manager._try_auto_rip(wj, job, 0, _Reporter(manager, wj))

    assert ok is False
    assert "Auto-rip: TMDB lookup failed (ConnectionError)" in _log(wj)
    assert "leaving for manual naming" in _log(wj)
    assert "exact matches" not in _log(wj)
    assert wj.assignments == {}


def _identify_rig(m, monkeypatch, matches):
    """Stub the rip+OCR phase and the reference fetch, leaving the manager's own
    confidence decision as the only live logic."""
    async def fake_identify_phase(job, staging_root, reporter):
        return {0: "ocr text for title 0", 1: "ocr text for title 1"}

    async def fake_refs(*a, **kw):
        return {9: "reference 9", 10: "reference 10"}

    monkeypatch.setattr(m.pipeline, "identify_phase", fake_identify_phase)
    monkeypatch.setattr(mgr, "gather_references", fake_refs)
    monkeypatch.setattr(mgr, "identify", lambda rips, refs, **kw: matches)


async def test_an_unsure_subtitle_match_parks_for_confirmation_rather_than_naming(
        manager, monkeypatch):
    """A scrambled TV disc's titles are only distinguishable by their subtitles.
    When one title's best and second-best episodes are neck and neck, naming it
    anyway swaps two episodes in the library silently — so the whole job stops
    at a gate and shows its working."""
    pushes = _pushes(manager, monkeypatch)
    _identify_rig(manager, monkeypatch, [
        Match(title_id=0, episode=9, score=0.51, runner_up=10,
              runner_up_score=0.49, confident=False),
        Match(title_id=1, episode=10, score=0.80, runner_up=9,
              runner_up_score=0.10, confident=True),
    ])
    wj = _job(manager, 110, state=JobState.READY, disc=_tv_disc(label="HOUSE_S1_D3"))
    wj.auto_identify = True
    wj.show_title, wj.season, wj.candidate_episodes = "House", 1, [9, 10]

    ok = await manager._auto_identify(wj, _Reporter(manager, wj))

    assert ok is False
    assert wj.state is JobState.AWAITING_MATCH
    assert wj.assignments == {}                      # nothing was guessed
    assert [c["confident"] for c in wj.match_candidates] == [False, True]
    assert "title 0 -> E9 (score 0.51, runner-up E10 0.49) — needs confirmation" \
        in _log(wj)
    assert "title 1 -> E10 (score 0.80, runner-up E9 0.10)" in _log(wj)
    assert "Subtitle match needs confirmation — review in the UI." in _log(wj)
    assert pushes[0][0].startswith("📺 Confirm episodes")


async def test_a_confident_subtitle_match_names_every_title_and_carries_on(
        manager, monkeypatch):
    """The gate is for doubt only — a clean match must not add a click to every
    disc in the box set."""
    pushes = _pushes(manager, monkeypatch)
    _identify_rig(manager, monkeypatch, [
        Match(title_id=0, episode=9, score=0.91, runner_up=10,
              runner_up_score=0.10, confident=True),
        Match(title_id=1, episode=10, score=0.88, runner_up=9,
              runner_up_score=0.09, confident=True),
    ])
    wj = _job(manager, 111, state=JobState.READY, disc=_tv_disc(label="HOUSE_S1_D3"))
    wj.auto_identify = True
    wj.show_title, wj.show_year = "House", 2004
    wj.season, wj.candidate_episodes = 1, [9, 10]

    ok = await manager._auto_identify(wj, _Reporter(manager, wj))

    assert ok is True
    assert wj.state is JobState.READY        # no gate inserted: the caller carries on
    assert [(a.title_id, a.episode, a.season, a.show_or_movie_title, a.year)
            for a in wj.assignments.values()] == [
        (0, 9, 1, "House", 2004), (1, 10, 1, "House", 2004)]
    assert wj.selected_title_ids == [0, 1]
    assert "needs confirmation" not in _log(wj)
    assert pushes == []
