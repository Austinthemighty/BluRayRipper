import os

import pytest

from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Stream, StreamType, Title
from blurayripper.persistence import (
    JobRecord,
    MemoryStore,
    PostgresStore,
    assignment_from_dict,
    assignment_to_dict,
    disc_from_dict,
    disc_to_dict,
    make_store,
)


def _disc():
    return DiscInfo(
        source="disc:0", disc_name="WarGames", volume_label="WARGAMES", drive_device="F:",
        titles=[Title(
            title_id=0, duration_seconds=6762, chapter_count=12, size_bytes=83_600_000_000,
            source_filename="00002.mpls", output_filename="title_t00.mkv",
            streams=[
                Stream(0, StreamType.VIDEO, codec="V_MPEG4/ISO/AVC"),
                Stream(1, StreamType.AUDIO, codec="DTS-HD", language="eng"),
                Stream(2, StreamType.SUBTITLE, codec="S_HDMV/PGS", language="eng"),
            ],
        )],
    )


def test_disc_roundtrip():
    d2 = disc_from_dict(disc_to_dict(_disc()))
    assert d2.disc_name == "WarGames"
    assert d2.drive_device == "F:"
    t = d2.title(0)
    assert t.duration_seconds == 6762 and t.source_filename == "00002.mpls"
    assert t.audio_languages == ["eng"] and t.subtitle_languages == ["eng"]


def test_the_disc_type_survives_a_restart_and_old_records_load_without_it():
    disc = _disc()
    disc.disc_type = "Blu-ray disc"
    assert disc_from_dict(disc_to_dict(disc)).disc_type == "Blu-ray disc"

    saved_before = disc_to_dict(_disc())
    saved_before.pop("disc_type")
    assert disc_from_dict(saved_before).disc_type is None


def test_a_two_episode_title_survives_a_restart_and_old_records_load_without_it():
    a = EpisodeAssignment(2, MediaKind.TV, "HIMYM", 2005, season=8, episode=11, episode_end=12)
    assert assignment_from_dict(assignment_to_dict(a)).episodes == [11, 12]
    old = assignment_to_dict(a)
    old.pop("episode_end")
    assert assignment_from_dict(old).episode_end is None


def test_assignment_roundtrip():
    a = EpisodeAssignment(3, MediaKind.TV, "The Show", 2010, 1, 9, "Pilot")
    a2 = assignment_from_dict(assignment_to_dict(a))
    assert a2 == a


def test_job_record_json_roundtrip():
    rec = JobRecord(
        id=1, source="disc:0", state="ready", fingerprint="fp",
        disc=disc_to_dict(_disc()),
        selected_title_ids=[0],
        assignments=[assignment_to_dict(EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983))],
        grain_mode="preview", tune_grain=None,
        checkpoints={"0": {"ripped_path": "/x.mkv", "placed": False}},
        outputs=[], error=None,
    )
    rec2 = JobRecord.from_json(rec.to_json())
    assert rec2 == rec


def test_memory_store_crud():
    store = MemoryStore()
    assert store.load_all() == []
    assert store.max_id() == 0
    store.save(JobRecord(id=2, source="disc:0", state="queued"))
    store.save(JobRecord(id=5, source="disc:1", state="done"))
    assert [r.id for r in store.load_all()] == [2, 5]
    assert store.max_id() == 5
    # upsert
    store.save(JobRecord(id=2, source="disc:0", state="ripping"))
    assert {r.id: r.state for r in store.load_all()}[2] == "ripping"
    store.delete(2)
    assert [r.id for r in store.load_all()] == [5]


def test_make_store_selects_backend():
    assert isinstance(make_store(None), MemoryStore)
    assert isinstance(make_store("postgresql://x/y"), PostgresStore)
    assert make_store(None).durable is False


def test_claim_next_picks_ready_and_skips_claimed():
    store = MemoryStore()
    store.save(JobRecord(id=1, source="disc:0", state="done"))      # not claimable
    store.save(JobRecord(id=2, source="disc:1", state="ready"))
    store.save(JobRecord(id=3, source="disc:2", state="ready"))

    a = store.claim_next("w1", ["ready"])
    b = store.claim_next("w2", ["ready"])
    c = store.claim_next("w3", ["ready"])
    assert a.id == 2 and b.id == 3      # each claims a distinct ready job
    assert c is None                    # nothing left to claim

    store.release(2)                    # freed -> claimable again
    assert store.claim_next("w4", ["ready"]).id == 2


@pytest.mark.skipif(
    not os.environ.get("BLURAYRIPPER_TEST_DATABASE_URL"),
    reason="set BLURAYRIPPER_TEST_DATABASE_URL to run Postgres integration test",
)
def test_postgres_roundtrip():
    store = PostgresStore(os.environ["BLURAYRIPPER_TEST_DATABASE_URL"])
    store.init()
    rec = JobRecord(id=999001, source="disc:0", state="ready", checkpoints={"0": {"placed": True}})
    store.save(rec)
    try:
        loaded = {r.id: r for r in store.load_all()}
        assert loaded[999001].checkpoints == {"0": {"placed": True}}
    finally:
        store.delete(999001)


def test_release_allows_immediate_reclaim():
    """A worker releasing its claim lets the job be re-claimed at once, instead of
    waiting for the frozen heartbeat to age past stale_after_seconds."""
    store = MemoryStore()
    store.save(JobRecord(id=1, source="x", state="ready"))
    assert store.claim_next("W1", ["ready"]).id == 1
    # Still 'ready' but already claimed -> no other worker can take it.
    assert store.claim_next("W2", ["ready"]) is None
    store.release(1)
    # After release it's claimable again (mirrors a retry the manager re-queues).
    assert store.claim_next("W2", ["ready"]).id == 1


def test_postgres_connect_retries_transient_blip(monkeypatch):
    """A transient connect failure is retried (absorbed) instead of tripping the
    15s circuit breaker; only a sustained failure opens it."""
    import sys
    import types

    from blurayripper import persistence as P

    state = {"calls": 0, "fail_up_to": 1}

    class _FakeConn:
        pass

    def fake_connect(url, autocommit=None, connect_timeout=None):
        state["calls"] += 1
        if state["calls"] <= state["fail_up_to"]:
            raise OSError("transient network blip")
        return _FakeConn()

    fake = types.ModuleType("psycopg")
    fake.connect = fake_connect
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setattr(P.time, "sleep", lambda *_a: None)  # no real backoff delay

    store = P.PostgresStore("postgresql://x")

    # One blip then success -> retry absorbs it; breaker stays closed.
    assert isinstance(store._connect(), _FakeConn)
    assert store._down_until == 0.0

    # Every attempt fails -> breaker opens and the error propagates.
    state["calls"] = 0
    state["fail_up_to"] = 99
    with pytest.raises(OSError):
        store._connect()
    assert store._down_until > 0.0

    # While the breaker is open, calls fail instantly without touching psycopg.
    before = state["calls"]
    with pytest.raises(ConnectionError):
        store._connect()
    assert state["calls"] == before


def test_claim_next_prefers_higher_priority():
    """A bumped (higher-priority) job is claimed before older, normal-priority ones."""
    store = MemoryStore()
    store.save(JobRecord(id=1, source="a", state="ready"))
    store.save(JobRecord(id=2, source="b", state="ready"))
    store.save(JobRecord(id=3, source="c", state="ready", priority=5))  # bumped
    # Highest priority first, then oldest id.
    assert store.claim_next("w", ["ready"]).id == 3
    assert store.claim_next("w", ["ready"]).id == 1
    assert store.claim_next("w", ["ready"]).id == 2


def test_priority_round_trips_through_record():
    store = MemoryStore()
    store.save(JobRecord(id=9, source="x", state="ready", priority=7))
    assert {r.id: r.priority for r in store.load_all()}[9] == 7
