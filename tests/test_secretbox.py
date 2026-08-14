"""Credentials shared through Postgres, encrypted with a per-box passphrase.

The threat model is narrow and worth stating: Postgres runs on a different
machine, so the database (and its dumps) must never hold readable credentials.
It does NOT defend against someone reading secrets.env on a ripper — that box
has to decrypt unattended at boot, so the passphrase is necessarily there.
"""

import pytest

from blurayripper.persistence import MemoryStore
from blurayripper.services import secretbox


class Durable(MemoryStore):
    @property
    def durable(self): return True


def test_a_round_trip_returns_exactly_what_went_in():
    blob = secretbox.encrypt("correct horse", {"TMDB_API_KEY": "abc123"})
    assert secretbox.decrypt("correct horse", blob) == {"TMDB_API_KEY": "abc123"}


def test_the_stored_blob_never_contains_the_plaintext():
    """The whole point: a leaked database dump has to be inert."""
    import json

    blob = secretbox.encrypt("pw", {"TMDB_API_KEY": "SUPERSECRET"})
    assert "SUPERSECRET" not in json.dumps(blob)


def test_a_wrong_passphrase_fails_loudly_rather_than_returning_nothing():
    """Returning {} would be indistinguishable from a working cluster with no
    credentials stored — the mistake would surface weeks later as artwork
    quietly not downloading."""
    blob = secretbox.encrypt("right", {"TMDB_API_KEY": "abc"})
    with pytest.raises(secretbox.SecretsUnavailable, match="differing from the box"):
        secretbox.decrypt("wrong", blob)


def test_tampering_with_the_ciphertext_is_detected():
    """AES-GCM is authenticated, so a database someone edited fails to open
    rather than decrypting to something attacker-chosen."""
    blob = secretbox.encrypt("pw", {"TMDB_API_KEY": "abc"})
    raw = bytearray(secretbox._unb64(blob["ct"]))
    raw[0] ^= 0xFF
    blob["ct"] = secretbox._b64(bytes(raw))
    with pytest.raises(secretbox.SecretsUnavailable):
        secretbox.decrypt("pw", blob)


def test_only_the_allowlisted_credentials_are_ever_shared():
    """Per-box values must not travel. The Windows worker's tool paths are not
    the Linux box's, so sharing them would break the cluster; and the database
    URL cannot move at all, since it is needed to reach the store."""
    moved = secretbox.movable({
        "TMDB_API_KEY": "abc",
        "OPENSUBTITLES_API_KEY": "def",
        "BLURAYRIPPER_DATABASE_URL": "postgresql://user:pw@host/db",
        "BLURAYRIPPER_HANDBRAKECLI": r"C:\tools\HandBrakeCLI.exe",
        "BLURAYRIPPER_STAGING_DIR": "Z:/staging",
    })
    assert moved == {"TMDB_API_KEY": "abc", "OPENSUBTITLES_API_KEY": "def"}


def test_blank_credentials_are_not_shared():
    assert secretbox.movable({"TMDB_API_KEY": "   "}) == {}


def test_publish_then_fetch_across_boxes():
    store = Durable()
    secretbox.publish(store, "shared-pw", {"TMDB_API_KEY": "abc"})
    assert secretbox.fetch(store, "shared-pw") == {"TMDB_API_KEY": "abc"}


def test_an_unreachable_database_falls_back_to_local_credentials():
    """An outage must not stop a box working — it just uses its own secrets.env."""
    class Broken(Durable):
        def get_shared_config(self, key):
            raise ConnectionError("no route to host")

    assert secretbox.fetch(Broken(), "pw") == {}


def test_a_box_with_no_passphrase_is_told_why_it_cannot_read_them():
    store = Durable()
    secretbox.publish(store, "pw", {"TMDB_API_KEY": "abc"})
    with pytest.raises(secretbox.SecretsUnavailable, match="is not set"):
        secretbox.fetch(store, "")


def test_cluster_credentials_only_fill_gaps(tmp_path, monkeypatch):
    """A value set on this box wins. Pointing a box at the cluster must never
    silently swap out a credential it already had."""
    from blurayripper.config import _CLUSTER_SECRETS, _load_secrets

    store = Durable()
    secretbox.publish(store, "pw", {"TMDB_API_KEY": "from-cluster",
                                    "OPENSUBTITLES_API_KEY": "os-cluster"})
    _CLUSTER_SECRETS.clear()
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)
    (tmp_path / "secrets.env").write_text(
        "BLURAYRIPPER_DATABASE_URL=postgresql://x/y\n"
        f"{secretbox.PASSPHRASE_KEY}=pw\n"
        "TMDB_API_KEY=local-wins\n")
    for var in ("TMDB_API_KEY", "OPENSUBTITLES_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    out = _load_secrets(tmp_path)
    assert out["TMDB_API_KEY"] == "local-wins"        # local override respected
    assert out["OPENSUBTITLES_API_KEY"] == "os-cluster"  # gap filled
    _CLUSTER_SECRETS.clear()


def test_without_a_passphrase_nothing_touches_the_database(tmp_path, monkeypatch):
    """Every existing install must stay on exactly the path it was on."""
    from blurayripper.config import _load_secrets

    def boom(url):
        raise AssertionError("must not build a store without a passphrase")

    monkeypatch.setattr("blurayripper.persistence.make_store", boom)
    (tmp_path / "secrets.env").write_text(
        "BLURAYRIPPER_DATABASE_URL=postgresql://x/y\nTMDB_API_KEY=abc\n")
    assert _load_secrets(tmp_path)["TMDB_API_KEY"] == "abc"
