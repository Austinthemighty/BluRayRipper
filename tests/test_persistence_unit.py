"""Unit coverage for the persistence layer — the part crash recovery leans on.

Three groups:
  * the JSON snapshot format (JobRecord / disc / assignment) that a restart
    reads back;
  * the ``Store`` base class contract every backend inherits, plus MemoryStore
    as the reference implementation;
  * ``PostgresStore``'s parameter and row handling, driven through a fake
    psycopg driver — the store's own logic (which column carries the state, how
    a row becomes a record, which statements a call issues) without a database.

What genuinely needs Postgres — the atomicity of ``FOR UPDATE SKIP LOCKED``,
whether ``ON CONFLICT`` really upserts, whether ``started_at`` resets only on a
new run token — is NOT asserted here; that is what the opt-in
BLURAYRIPPER_TEST_DATABASE_URL test in tests/test_persistence.py is for.
"""

import json
import sys
import types
from decimal import Decimal

from blurayripper.models import (
    DiscInfo,
    EpisodeAssignment,
    MediaKind,
    Stream,
    StreamType,
    Title,
)
from blurayripper.persistence import (
    JobRecord,
    MemoryStore,
    PostgresStore,
    Store,
    assignment_from_dict,
    assignment_to_dict,
    disc_from_dict,
    disc_to_dict,
    make_store,
    signal_key,
)
from blurayripper.pipeline import JobState

# --- fixtures / fakes ---------------------------------------------------


def _disc() -> DiscInfo:
    return DiscInfo(
        source="disc:0", disc_name="Bäbylon 5", volume_label="BABYLON5", drive_device="F:",
        titles=[Title(
            title_id=3, duration_seconds=2640, chapter_count=6, size_bytes=21_000_000_000,
            source_filename="00071.mpls", output_filename="title_t03.mkv",
            streams=[Stream(0, StreamType.VIDEO, codec="V_MPEG4/ISO/AVC"),
                     Stream(1, StreamType.AUDIO, codec="DTS-HD", language="eng")],
        )],
    )


def _full_record() -> JobRecord:
    """A record with every field populated — what a mid-OCR job looks like."""
    return JobRecord(
        id=42, source="disc:0", state=JobState.OCR_SUBS.value, fingerprint="fp-abc",
        disc=disc_to_dict(_disc()),
        selected_title_ids=[0, 3],
        assignments=[assignment_to_dict(
            EpisodeAssignment(3, MediaKind.TV, "Bäbylon 5", 1994, 2, 7, "Geheimnis"))],
        grain_mode="film", tune_grain=True,
        checkpoints={"0": {"ripped_path": "Z:/rips/t00.mkv", "placed": True},
                     "3": {"ripped_path": "Z:/rips/t03.mkv", "placed": False}},
        outputs=["Z:/TV/Bäbylon 5 (1994)/Season 02/s02e07.mkv"],
        error=None, priority=9, stage="ocr_subs", progress=0.42,
        log=["[2026-08-21 10:00:00] Ripping title 3 …", "字幕 OCR 完了", "naïve café"],
        worker="ripbox-2", auto_identify=True, show_tmdb_id=1234, season=2,
        candidate_episodes=[5, 6, 7], show_title="Bäbylon 5", show_year=1994,
        aacs_dump="Z:/dumps/aacs-babylon5.bin",
    )


class _Jsonb:
    """Stand-in for psycopg.types.json.Jsonb (adapts a dict to a JSONB param)."""

    def __init__(self, obj):
        self.obj = obj


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        self._rows = self._conn.results.pop(0) if self._conn.results else []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, results):
        self.executed: list[tuple] = []
        self.results = [list(r) for r in results]
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


def _pg(monkeypatch, *results):
    """A PostgresStore wired to a fake psycopg driver.

    ``results`` are the result sets handed to successive ``execute()`` calls
    (default: no rows). Returns (store, connection) so a test can inspect the
    statements and parameters the store produced."""
    conn = _FakeConn(results)
    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda *a, **k: conn
    types_mod = types.ModuleType("psycopg.types")
    json_mod = types.ModuleType("psycopg.types.json")
    json_mod.Jsonb = _Jsonb
    types_mod.json = json_mod
    psycopg.types = types_mod
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.types", types_mod)
    monkeypatch.setitem(sys.modules, "psycopg.types.json", json_mod)
    return PostgresStore("postgresql://fake/brr"), conn


class _MinimalStore(Store):
    """A backend implementing only the abstract surface — everything else has to
    come from the base class."""

    def __init__(self) -> None:
        self.rows: dict[int, JobRecord] = {}

    def init(self) -> None:
        pass

    def save(self, record):
        self.rows[record.id] = record

    def load_all(self):
        return [self.rows[k] for k in sorted(self.rows)]

    def delete(self, job_id):
        self.rows.pop(job_id, None)

    def claim_next(self, worker_id, states, stale_after_seconds=180):
        return None

    def heartbeat(self, job_id, worker_id):
        pass

    def release(self, job_id):
        pass


# --- the JSON snapshot format -------------------------------------------


def test_job_record_round_trips_every_field_through_real_json():
    """The record is written as JSONB and read back with ``cls(**data)``. A field
    that doesn't survive (or isn't JSON-native at all) silently loses that part of
    the job on restart — e.g. the checkpoints that stop a finished rip re-ripping."""
    rec = _full_record()
    assert JobRecord.from_json(rec.to_json()) == rec

    revived = JobRecord.from_json(json.loads(json.dumps(rec.to_json())))
    assert revived == rec
    assert revived.log[1] == "字幕 OCR 完了"       # non-ASCII log lines survive
    assert revived.checkpoints["0"]["placed"] is True
    assert revived.checkpoints["3"]["ripped_path"] == "Z:/rips/t03.mkv"
    assert revived.aacs_dump == "Z:/dumps/aacs-babylon5.bin"
    assert revived.candidate_episodes == [5, 6, 7]
    assert disc_from_dict(revived.disc).title(3).source_filename == "00071.mpls"
    assert assignment_from_dict(revived.assignments[0]).episode_title == "Geheimnis"


def test_saving_a_record_snapshots_it_so_later_edits_dont_rewrite_history():
    """A store keeps whatever ``to_json()`` returned. If that shared the live
    lists/dicts, a job appending to its log or flipping a checkpoint after the
    save would retroactively edit the snapshot — and a crash would 'recover' a
    state that was never persisted."""
    store = MemoryStore()
    rec = JobRecord(id=1, source="disc:0", state="ripping", log=["scanning"],
                    checkpoints={"0": {"placed": False}}, outputs=[])
    store.save(rec)

    rec.log.append("encoding")
    rec.checkpoints["0"]["placed"] = True
    rec.outputs.append("/late.mkv")

    (saved,) = store.load_all()
    assert saved.log == ["scanning"]
    assert saved.checkpoints == {"0": {"placed": False}}
    assert saved.outputs == []


def test_every_job_state_survives_the_store_and_is_claimable_by_name():
    """Recovery turns the stored string back into ``JobState(rec.state)``; a state
    that didn't round-trip exactly would crash the restart for that job, and a
    worker filters on the same strings when it claims."""
    store = MemoryStore()
    for i, st in enumerate(JobState, start=1):
        store.save(JobRecord(id=i, source=f"disc:{i}", state=st.value))

    assert [JobState(r.state) for r in store.load_all()] == list(JobState)

    picked = store.claim_next("w1", [JobState.ENCODING.value])
    assert JobState(picked.state) is JobState.ENCODING
    # Only the one encoding job matches — claiming is filtered, not "next row".
    assert store.claim_next("w2", [JobState.ENCODING.value]) is None


def test_disc_serialization_handles_a_missing_disc_and_sparse_titles():
    """A job persisted before its scan finished has no disc at all, and a title
    dict written by an older build lacks the optional keys. Recovery must load
    both rather than KeyError its way out of the whole restart."""
    assert disc_to_dict(None) is None
    assert disc_from_dict(None) is None
    assert disc_from_dict({}) is None

    d = disc_from_dict({"source": "disc:0",
                        "titles": [{"title_id": 1, "duration_seconds": 60,
                                    "chapter_count": 0, "size_bytes": 0}]})
    assert d.source == "disc:0" and d.disc_name is None and d.drive_device is None
    t = d.title(1)
    assert t.streams == []
    assert t.source_filename is None and t.output_filename is None


def test_stream_table_survives_the_disc_round_trip():
    """Encode/subtitle decisions after a restart are taken from the RECOVERED
    disc, so each stream's type and language has to come back intact — a lost
    subtitle language means the PGS track is never OCR'd."""
    disc = DiscInfo(source="disc:0", titles=[Title(title_id=0, streams=[
        Stream(0, StreamType.VIDEO, "V_MPEG4/ISO/AVC"),
        Stream(1, StreamType.AUDIO, "DTS-HD", "eng"),
        Stream(2, StreamType.SUBTITLE, "S_HDMV/PGS", "fra"),
        Stream(3, StreamType.UNKNOWN),
    ])])
    back = disc_from_dict(disc_to_dict(disc)).title(0)

    assert [s.stream_type for s in back.streams] == [
        StreamType.VIDEO, StreamType.AUDIO, StreamType.SUBTITLE, StreamType.UNKNOWN]
    assert [s.stream_id for s in back.streams] == [0, 1, 2, 3]
    assert back.audio_languages == ["eng"] and back.subtitle_languages == ["fra"]
    assert back.streams[0].codec == "V_MPEG4/ISO/AVC"
    assert back.streams[3].codec is None and back.streams[3].language is None


def test_assignment_round_trip_keeps_the_naming_fields_that_place_the_file():
    """Plex naming after a restart runs off the recovered assignments: a dropped
    edition tag or season number puts the file under a different name than the
    one already half-written."""
    tv = EpisodeAssignment(3, MediaKind.TV, "The Show", 2010, 1, 9, "Pilot")
    movie = EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983, edition="Extended Cut")
    bare = EpisodeAssignment(1, MediaKind.MOVIE, "Untitled")
    for a in (tv, movie, bare):
        assert assignment_from_dict(assignment_to_dict(a)) == a

    assert assignment_from_dict(assignment_to_dict(movie)).edition == "Extended Cut"
    back = assignment_from_dict(assignment_to_dict(bare))
    assert back.year is None and back.season is None and back.edition is None
    # kind comes back as the enum the naming code branches on, not a raw string
    assert assignment_from_dict(assignment_to_dict(tv)).kind is MediaKind.TV


# --- the Store base-class contract --------------------------------------


def test_store_defaults_report_no_cluster_state_instead_of_raising():
    """The UI calls the cluster accessors on whatever store is configured
    (workers panel, per-worker overrides, shared config). A backend that doesn't
    implement them must degrade to 'nothing configured' — and the base setters
    must stay no-ops, so a non-shared store never claims to have stored a
    setting that no worker will ever read."""
    store = _MinimalStore()
    assert store.get_worker_settings("ripbox-2") == {}
    assert store.all_worker_settings() == {}
    assert store.cluster_signals() == {}
    assert store.get_shared_config("handbrake_preset") is None
    assert store.list_workers() == []

    store.set_worker_settings("ripbox-2", {"encoder": "nvenc"})
    store.set_shared_config("handbrake_preset", {"preset": "HQ 1080p"})
    store.record_worker("ripbox-2", "1.4.0", 8, "encoding", "run-abc")
    store.request_cluster_signal("drain", "ripbox-2")

    assert store.get_worker_settings("ripbox-2") == {}
    assert store.all_worker_settings() == {}
    assert store.get_shared_config("handbrake_preset") is None
    assert store.cluster_signals() == {}
    assert store.list_workers() == []


def test_an_unrecognised_backend_is_assumed_durable_but_memory_is_not():
    """``store.durable`` gates every cluster feature (workers panel, remote
    claiming, per-worker overrides). Postgres and any other shared backend must
    read as durable; the in-memory default must not, or the UI would offer
    clustering that can never reach a worker."""
    assert _MinimalStore().durable is True
    assert PostgresStore("postgresql://x/y").durable is True
    assert MemoryStore().durable is False


def test_max_id_is_the_highest_id_present_not_the_row_count():
    """New jobs are numbered ``max_id() + 1``. Counting rows instead would hand a
    new job an id that already exists after any delete, and the save would
    overwrite that older job's record."""
    store = _MinimalStore()
    assert store.max_id() == 0                      # empty store starts numbering at 1
    for jid in (3, 41, 7):
        store.save(JobRecord(id=jid, source="disc:0", state="done"))
    assert store.max_id() == 41
    store.delete(41)
    assert store.max_id() == 7


def test_worker_update_request_rides_the_broadcast_signal_key():
    """'Update all workers' is the ``update:*`` row; a worker acts when the token
    rises above the value it snapshotted at startup, so the token must increase
    per request and a signal aimed at one box must not move the broadcast."""
    store = MemoryStore()
    assert store.worker_update_token() == 0         # never requested

    store.request_worker_update()
    assert store.cluster_signals()[signal_key("update", "*")] == 1
    assert store.worker_update_token() == 1

    store.request_worker_update()
    assert store.worker_update_token() == 2

    store.request_cluster_signal("drain", "ripbox-2")
    assert store.worker_update_token() == 2
    assert store.cluster_signals()["drain:ripbox-2"] == 1


# --- MemoryStore as the reference implementation ------------------------


def test_memory_store_heartbeat_leaves_the_claim_alone():
    """MemoryStore is single-process, so a heartbeat is a no-op — but it has to
    STAY a no-op: if it dropped the claim, the in-process runner heartbeating its
    own job would make that job claimable again and it would be started twice."""
    store = MemoryStore()
    store.save(JobRecord(id=1, source="disc:0", state="ready"))
    assert store.claim_next("w1", ["ready"]).id == 1

    store.heartbeat(1, "w1")
    store.heartbeat(1, "someone-else")
    assert store.claim_next("w2", ["ready"]) is None     # still claimed

    store.release(1)
    assert store.claim_next("w2", ["ready"]).id == 1


def test_memory_store_copies_worker_overlays_in_and_out():
    """The overlay is copied on the way in and on the way out: the settings page
    keeps editing its own dict after submitting, and a reader that mutates what
    it got back must not rewrite the stored override for everyone else."""
    store = MemoryStore()
    overlay = {"tools": {"handbrakecli": "C:/hb.exe"}}
    store.set_worker_settings("ripbox-2", overlay)

    overlay["tools"]["handbrakecli"] = "C:/EDITED-BUT-NOT-SUBMITTED.exe"
    assert store.get_worker_settings("ripbox-2") == {"tools": {"handbrakecli": "C:/hb.exe"}}

    got = store.get_worker_settings("ripbox-2")
    got["tools"]["handbrakecli"] = "C:/MUTATED-BY-READER.exe"
    assert store.get_worker_settings("ripbox-2")["tools"]["handbrakecli"] == "C:/hb.exe"
    assert store.all_worker_settings()["ripbox-2"]["tools"]["handbrakecli"] == "C:/hb.exe"

    store.set_worker_settings("ripbox-2", {})           # empty overlay clears it
    assert store.all_worker_settings() == {}
    assert store.get_worker_settings("ripbox-2") == {}


def test_memory_store_copies_shared_config_in_and_out():
    """Same aliasing hazard for the cluster-wide config: the settings object the
    caller keeps holding must not be able to change what the store hands to the
    next reader."""
    store = MemoryStore()
    value = {"preset": "HQ 1080p", "extra": {"quality": 20}}
    store.set_shared_config("handbrake", value)

    value["extra"]["quality"] = 99
    assert store.get_shared_config("handbrake")["extra"]["quality"] == 20

    got = store.get_shared_config("handbrake")
    got["extra"]["quality"] = 1
    assert store.get_shared_config("handbrake")["extra"]["quality"] == 20
    assert store.get_shared_config("never-set") is None


def test_make_store_treats_a_blank_database_url_as_no_database():
    """An empty BLURAYRIPPER_DATABASE_URL (set, but blank) is how a box is taken
    out of the cluster. It must produce the in-memory store, not a PostgresStore
    that fails to connect on every single persist."""
    assert isinstance(make_store(""), MemoryStore)
    assert isinstance(make_store(None), MemoryStore)
    assert make_store("").durable is False

    pg = make_store("postgresql://user@db/brr")
    assert isinstance(pg, PostgresStore)
    assert pg._url == "postgresql://user@db/brr"    # the URL reaches the connection
    assert pg.durable is True


# --- PostgresStore statements and row handling --------------------------


def test_init_creates_and_migrates_the_schema_idempotently(monkeypatch):
    """init() runs on every start, so every CREATE has to be guarded — and the
    ALTERs have to be there, because a serve box whose jobs table predates
    clustering would otherwise fail every claim on 'no column claimed_by'."""
    store, conn = _pg(monkeypatch)
    store.init()

    (sql, _params), = conn.executed
    for table in ("jobs", "workers", "worker_settings", "cluster_signals", "cluster_config"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "CREATE TABLE " not in sql.replace("CREATE TABLE IF NOT EXISTS", "")
    assert "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_by" in sql
    assert "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS heartbeat" in sql
    assert "ALTER TABLE workers ADD COLUMN IF NOT EXISTS run_token" in sql
    assert conn.closed


def test_save_denormalises_the_state_into_its_own_column(monkeypatch):
    """claim_next filters on ``jobs.state``, not on the JSON blob. If save wrote
    only the blob (or put the wrong value in that column), a queued job would sit
    there forever and no worker would ever see it."""
    store, conn = _pg(monkeypatch)
    rec = _full_record()
    store.save(rec)

    (sql, params), = conn.executed
    assert params[0] == rec.id
    assert params[2] == JobState.OCR_SUBS.value
    assert isinstance(params[1], _Jsonb)
    assert JobRecord.from_json(params[1].obj) == rec     # the whole record goes in
    assert "ON CONFLICT (id) DO UPDATE" in sql          # re-saving updates in place
    assert conn.closed


def test_load_all_reads_rows_whether_the_driver_returns_json_or_text(monkeypatch):
    """psycopg hands JSONB back as a dict, but a data column that ended up TEXT
    (an older deployment, or a driver without the JSON adapter) yields a string.
    Recovery has to read both, or one such row kills the entire restart."""
    a = JobRecord(id=1, source="disc:0", state="ready", outputs=["/x.mkv"]).to_json()
    b = JobRecord(id=2, source="disc:1", state="done", log=["naïve café"]).to_json()
    store, conn = _pg(monkeypatch, [(a,), (json.dumps(b, ensure_ascii=False),)])

    got = store.load_all()
    assert [r.id for r in got] == [1, 2]
    assert got[0].outputs == ["/x.mkv"]
    assert got[1].log == ["naïve café"]

    (sql, _params), = conn.executed
    assert "ORDER BY id" in sql          # recovery replays jobs oldest-first
    assert conn.closed


def test_claim_next_returns_none_when_nothing_was_claimable(monkeypatch):
    """The UPDATE matched no row: the worker must get None and poll again rather
    than blow up on an empty RETURNING (it polls every few seconds, forever)."""
    store, conn = _pg(monkeypatch, [])
    assert store.claim_next("ripbox-2", ["ready"]) is None
    assert conn.closed


def test_claim_next_takes_one_free_or_stale_row_at_the_top_priority(monkeypatch):
    """The claim is the whole cluster contract: one row at a time, highest
    priority first, skipping rows another worker has locked, and taking back a
    job whose owner stopped heartbeating."""
    rec = JobRecord(id=8, source="disc:0", state="ready", priority=5)
    store, conn = _pg(monkeypatch, [(rec.to_json(),)])

    assert store.claim_next("ripbox-2", ["ready", "queued"], stale_after_seconds=90) == rec

    sql, params = conn.executed[0]
    assert params == ("ripbox-2", ["ready", "queued"], 90)
    assert "FOR UPDATE SKIP LOCKED LIMIT 1" in sql
    assert "ORDER BY COALESCE((data->>'priority')::int, 0) DESC, id ASC" in sql
    assert "claimed_by IS NULL OR heartbeat < now() - make_interval(secs => %s)" in sql
    assert "SET claimed_by = %s, heartbeat = now()" in sql


def test_claim_next_passes_the_states_as_a_list_for_any(monkeypatch):
    """``state = ANY(%s)`` needs a list parameter; a tuple adapts to a composite
    row instead and the claim errors out, so the callers' tuple is normalised."""
    store, conn = _pg(monkeypatch)
    store.claim_next("ripbox-2", ("ready", "queued"))
    assert conn.executed[0][1][1] == ["ready", "queued"]


def test_claim_next_reads_a_text_data_column(monkeypatch):
    """Same TEXT-vs-JSONB hazard as load_all, on the path that actually starts
    work: the claimed record carries the rip checkpoints, so failing to decode it
    would re-rip a disc that was already ripped."""
    rec = JobRecord(id=3, source="disc:1", state="ready",
                    checkpoints={"0": {"ripped_path": "Z:/rips/t00.mkv", "placed": False}})
    store, _conn = _pg(monkeypatch, [(json.dumps(rec.to_json()),)])

    got = store.claim_next("ripbox-2", ["ready"])
    assert got.checkpoints == {"0": {"ripped_path": "Z:/rips/t00.mkv", "placed": False}}


def test_heartbeat_refreshes_only_the_row_this_worker_still_owns(monkeypatch):
    """Without the claimed_by guard, a worker that already lost its job (it went
    stale and another box took it) would keep resetting the new owner's
    heartbeat, and the job would flap between the two."""
    store, conn = _pg(monkeypatch)
    store.heartbeat(8, "ripbox-2")

    (sql, params), = conn.executed
    assert params == (8, "ripbox-2")
    assert "SET heartbeat = now()" in sql
    assert "WHERE id = %s AND claimed_by = %s" in sql
    assert conn.closed


def test_release_clears_the_claim_without_touching_the_job(monkeypatch):
    """Release is what a worker does when it hands a job back (retry, drain).
    The row must survive so the job can be claimed again — and its data blob must
    not be rewritten from whatever the releasing worker had in memory."""
    store, conn = _pg(monkeypatch)
    store.release(8)

    (sql, params), = conn.executed
    assert params == (8,)
    assert "SET claimed_by = NULL" in sql
    assert "DELETE" not in sql.upper()
    assert "data" not in sql
    assert conn.closed


def test_delete_removes_exactly_one_job(monkeypatch):
    """Deleting a finished job from the UI must be scoped by id; an unscoped
    DELETE would empty the whole queue on the first cleanup."""
    store, conn = _pg(monkeypatch)
    store.delete(12)

    (sql, params), = conn.executed
    assert params == (12,)
    assert "DELETE FROM jobs WHERE id = %s" in sql


def test_record_worker_sends_the_run_token_so_uptime_tracks_the_process(monkeypatch):
    """The workers panel reports uptime since the worker PROCESS started. The
    store resets started_at only when the run token changes, so the token has to
    reach the query — otherwise every heartbeat either zeroes the uptime or it
    counts from the first time that box was ever seen."""
    store, conn = _pg(monkeypatch)
    store.record_worker("ripbox-2", "1.4.0", 8, "encoding", "run-abc")

    (sql, params), = conn.executed
    assert params == ("ripbox-2", "1.4.0", 8, "encoding", "run-abc")
    assert "started_at = CASE WHEN workers.run_token IS DISTINCT FROM EXCLUDED.run_token" in sql
    assert "last_seen = now()" in sql
    assert conn.closed


def test_cluster_signals_are_keyed_by_action_and_target(monkeypatch):
    """A restart aimed at one box must not read as the broadcast, and the token
    only ever increases — that monotonic bump is how a worker tells a new request
    from the one it already consumed."""
    store, conn = _pg(monkeypatch)
    store.request_cluster_signal("restart", "ripbox-2")
    store.request_cluster_signal("drain")            # default target: every worker
    store.request_worker_update()                    # base-class convenience wrapper

    assert [params for _sql, params in conn.executed] == [
        ("restart:ripbox-2",), ("drain:*",), ("update:*",)]
    assert "token = cluster_signals.token + 1" in conn.executed[0][0]


def test_cluster_signals_come_back_as_plain_ints(monkeypatch):
    """Tokens are compared against (and stored as) the snapshot a worker took at
    startup; a NUMERIC column would arrive as Decimal, which is not JSON-native
    and not what the declared dict[str, int] contract promises."""
    store, conn = _pg(monkeypatch, [("update:*", Decimal("3")), ("drain:ripbox-2", 1)])

    got = store.cluster_signals()
    assert got == {"update:*": 3, "drain:ripbox-2": 1}
    assert all(type(v) is int for v in got.values())
    assert conn.closed


def test_worker_update_token_reads_the_broadcast_key_from_postgres(monkeypatch):
    """The base-class helper picks ``update:*`` out of the signal table — not the
    first row, and not a per-worker restart signal that happens to be there."""
    store, _conn = _pg(monkeypatch, [("restart:ripbox-2", 9), ("update:*", 4)])
    assert store.worker_update_token() == 4

    quiet, _conn2 = _pg(monkeypatch, [("restart:ripbox-2", 9)])
    assert quiet.worker_update_token() == 0          # never requested


def test_shared_config_is_none_when_the_cluster_never_set_it(monkeypatch):
    """A box that starts before anyone saved a shared preset falls back to its
    local config; a missing row must read as 'unset', not crash the startup."""
    store, conn = _pg(monkeypatch, [])
    assert store.get_shared_config("handbrake_preset") is None

    (sql, params), = conn.executed
    assert params == ("handbrake_preset",)
    assert "WHERE key = %s" in sql


def test_shared_config_decodes_both_json_and_text_values(monkeypatch):
    """The value drives encoding on every box, so a TEXT column has to parse the
    same as JSONB rather than handing the settings layer a raw string."""
    store, _conn = _pg(monkeypatch, [({"preset": "HQ 1080p", "quality": 20},)])
    assert store.get_shared_config("handbrake") == {"preset": "HQ 1080p", "quality": 20}

    store2, _conn2 = _pg(monkeypatch, [('{"preset": "HQ 1080p"}',)])
    assert store2.get_shared_config("handbrake") == {"preset": "HQ 1080p"}


def test_setting_shared_config_overwrites_the_existing_value(monkeypatch):
    """Editing the preset once on the serve UI has to reach every box — an insert
    that failed on an existing key would leave the workers on the old value."""
    store, conn = _pg(monkeypatch)
    store.set_shared_config("handbrake_preset", {"preset": "HQ 1080p"})

    (sql, params), = conn.executed
    assert params[0] == "handbrake_preset"
    assert isinstance(params[1], _Jsonb) and params[1].obj == {"preset": "HQ 1080p"}
    assert "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value" in sql
    assert conn.closed


def test_worker_settings_are_empty_for_a_box_with_no_override(monkeypatch):
    """Every worker asks for its overlay at startup. The common case — no
    override configured — must be an empty overlay, not a missing-row crash that
    stops the worker before it ever polls."""
    store, conn = _pg(monkeypatch, [])
    assert store.get_worker_settings("ripbox-2") == {}
    assert conn.executed[0][1] == ("ripbox-2",)


def test_worker_settings_decode_a_text_overlay(monkeypatch):
    """An overlay stored as TEXT still has to arrive as a dict — the worker
    layers it over its own settings and would otherwise crash on a string."""
    store, _conn = _pg(monkeypatch, [('{"tools": {"handbrakecli": "C:/hb.exe"}}',)])
    assert store.get_worker_settings("ripbox-2") == {"tools": {"handbrakecli": "C:/hb.exe"}}


def test_clearing_a_worker_overlay_deletes_the_row_and_writes_nothing_else(monkeypatch):
    """'Reset to defaults' submits an empty overlay. If the DELETE fell through
    into the INSERT, the box would be pinned to an empty override row forever
    instead of going back to its own local settings."""
    store, conn = _pg(monkeypatch)
    store.set_worker_settings("ripbox-2", {})

    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert sql.startswith("DELETE FROM worker_settings")
    assert params == ("ripbox-2",)
    assert conn.closed              # the early return still closes the connection


def test_storing_a_worker_overlay_upserts_it(monkeypatch):
    """Re-saving a box's overrides has to replace the row; a plain insert would
    fail on the second edit and the change would never reach the worker."""
    store, conn = _pg(monkeypatch)
    store.set_worker_settings("ripbox-2", {"encoder": "nvenc"})

    (sql, params), = conn.executed
    assert params[0] == "ripbox-2"
    assert isinstance(params[1], _Jsonb) and params[1].obj == {"encoder": "nvenc"}
    assert "ON CONFLICT (worker_id) DO UPDATE" in sql
    assert "DELETE" not in sql.upper()


def test_all_worker_settings_keys_every_overlay_by_worker(monkeypatch):
    """The settings page lists which boxes have an override; a TEXT overlay must
    decode like a JSONB one or that box's panel renders a string instead of its
    settings."""
    store, conn = _pg(monkeypatch, [("ripbox-1", {"encoder": "nvenc"}),
                                    ("ripbox-2", '{"encoder": "qsv"}')])
    assert store.all_worker_settings() == {"ripbox-1": {"encoder": "nvenc"},
                                           "ripbox-2": {"encoder": "qsv"}}
    assert conn.closed


def test_list_workers_prunes_long_gone_boxes_then_reports_the_recent_ones(monkeypatch):
    """The panel shows boxes seen in the last 10 minutes and sweeps rows older
    than an hour, so a decommissioned box doesn't linger forever. A row with NULL
    timestamps (written before the started_at migration) must report 0, not crash
    the panel on float(None)."""
    store, conn = _pg(monkeypatch, [], [
        ("ripbox-1", "1.4.0", 8, "encoding", 3.5, 7200.0),
        ("ripbox-2", None, None, None, None, None),
    ])
    got = store.list_workers()

    assert [w.worker_id for w in got] == ["ripbox-1", "ripbox-2"]
    first = got[0]
    assert (first.version, first.job_id, first.stage) == ("1.4.0", 8, "encoding")
    assert first.seconds_since_seen == 3.5 and first.uptime_seconds == 7200.0
    assert got[1].seconds_since_seen == 0.0 and got[1].uptime_seconds == 0.0

    prune_sql, select_sql = conn.executed[0][0], conn.executed[1][0]
    assert prune_sql.startswith("DELETE FROM workers") and "interval '1 hour'" in prune_sql
    assert "interval '10 minutes'" in select_sql and "ORDER BY worker_id" in select_sql
    assert conn.closed


def test_every_postgres_call_closes_its_connection(monkeypatch):
    """A fresh connection per call is only viable because each one is closed.
    Leaking them exhausts Postgres's connection slots and the whole cluster
    stalls on 'too many clients already'."""
    calls = {
        "init": lambda s: s.init(),
        "save": lambda s: s.save(JobRecord(id=1, source="disc:0", state="ready")),
        "load_all": lambda s: s.load_all(),
        "delete": lambda s: s.delete(1),
        "claim_next": lambda s: s.claim_next("ripbox-2", ["ready"]),
        "heartbeat": lambda s: s.heartbeat(1, "ripbox-2"),
        "release": lambda s: s.release(1),
        "record_worker": lambda s: s.record_worker("ripbox-2", "1.4.0", None, None, "run"),
        "request_cluster_signal": lambda s: s.request_cluster_signal("drain"),
        "cluster_signals": lambda s: s.cluster_signals(),
        "get_shared_config": lambda s: s.get_shared_config("k"),
        "set_shared_config": lambda s: s.set_shared_config("k", {"a": 1}),
        "get_worker_settings": lambda s: s.get_worker_settings("ripbox-2"),
        "set_worker_settings": lambda s: s.set_worker_settings("ripbox-2", {"a": 1}),
        "clear_worker_settings": lambda s: s.set_worker_settings("ripbox-2", {}),
        "all_worker_settings": lambda s: s.all_worker_settings(),
        "list_workers": lambda s: s.list_workers(),
    }
    for name, call in calls.items():
        store, conn = _pg(monkeypatch)
        call(store)
        assert conn.closed, f"{name} left its connection open"
