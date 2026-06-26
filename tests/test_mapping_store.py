from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.services.mapping_store import MappingStore


def _assignments():
    return [
        EpisodeAssignment(0, MediaKind.TV, "The Show", 2010, 1, 1, "Pilot"),
        EpisodeAssignment(1, MediaKind.TV, "The Show", 2010, 1, 2, "Second"),
    ]


def test_save_and_recall_roundtrip(tmp_path):
    store = MappingStore(tmp_path / "mappings.db")
    store.save("fp123", "The Show S1 D1", _assignments())

    recalled = store.get("fp123")
    assert recalled is not None
    assert [a.episode for a in recalled] == [1, 2]
    assert recalled[0].kind is MediaKind.TV
    assert recalled[1].episode_title == "Second"


def test_unknown_fingerprint_returns_none(tmp_path):
    store = MappingStore(tmp_path / "mappings.db")
    assert store.get("nope") is None


def test_save_is_idempotent_upsert(tmp_path):
    store = MappingStore(tmp_path / "mappings.db")
    store.save("fp", "name", _assignments())
    updated = [EpisodeAssignment(0, MediaKind.TV, "The Show", 2010, 1, 5, "Changed")]
    store.save("fp", "name", updated)
    recalled = store.get("fp")
    assert len(recalled) == 1
    assert recalled[0].episode == 5


def test_forget(tmp_path):
    store = MappingStore(tmp_path / "mappings.db")
    store.save("fp", "name", _assignments())
    store.forget("fp")
    assert store.get("fp") is None
