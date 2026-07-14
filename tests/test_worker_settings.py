"""Central per-worker settings overrides (stored centrally, applied by workers)."""
from pathlib import Path

from blurayripper.config import load_settings
from blurayripper.persistence import MemoryStore
from blurayripper import settings_store as ss


class DurableMem(MemoryStore):
    durable = True   # subclass attr shadows the base read-only property


def _cfg(tmp_path):
    (tmp_path / "settings.toml").write_text(
        '[encode]\nffmpeg_preset="medium"\nffmpeg_crf=18\n', encoding="utf-8")
    return tmp_path


def test_worker_overlay_is_sparse_and_ignores_server_only_fields(tmp_path):
    d = _cfg(tmp_path)
    store = DurableMem()
    overlay = ss.save_settings(
        d,
        {"encode": {"ffmpeg_preset": "slower", "ffmpeg_crf": "18"},  # crf == base -> dropped
         "web": {"port": "9999"}},                                    # not worker-applicable
        scope="w1", store=store)
    assert overlay == {"encode": {"ffmpeg_preset": "slower"}}
    assert store.get_worker_settings("w1") == {"encode": {"ffmpeg_preset": "slower"}}


def test_worker_payload_shows_only_worker_fields_and_marks_overrides(tmp_path):
    d = _cfg(tmp_path)
    store = DurableMem()
    store.set_worker_settings("w1", {"encode": {"ffmpeg_preset": "slower"}})
    payload = ss.settings_ui_payload(d, "w1", store)
    assert payload["scope"] == "worker"
    titles = [g["title"] for g in payload["groups"]]
    assert "Encode" in titles and "Web" not in titles and "Storage" not in titles
    enc = {f["key"]: f for g in payload["groups"] if g["title"] == "Encode" for f in g["fields"]}
    assert enc["ffmpeg_preset"]["value"] == "slower" and enc["ffmpeg_preset"]["overridden"] is True
    assert enc["ffmpeg_crf"]["value"] == 18 and enc["ffmpeg_crf"]["overridden"] is False
    assert enc["ffmpeg_crf"]["base"] == 18


def test_worker_load_settings_applies_overlay(tmp_path):
    d = _cfg(tmp_path)
    store = DurableMem()
    store.set_worker_settings("w1", {"encode": {"ffmpeg_preset": "veryslow"}})
    s = load_settings(d, extra_overlay=store.get_worker_settings("w1"))
    assert s.encode.ffmpeg_preset == "veryslow"   # overlay wins
    assert s.encode.ffmpeg_crf == 18              # base preserved


def test_resetting_to_base_clears_the_override(tmp_path):
    d = _cfg(tmp_path)
    store = DurableMem()
    store.set_worker_settings("w1", {"encode": {"ffmpeg_preset": "slower"}})
    ss.save_settings(d, {"encode": {"ffmpeg_preset": "medium"}}, scope="w1", store=store)
    assert store.get_worker_settings("w1") == {}


def test_global_payload_lists_workers_and_durability(tmp_path):
    d = _cfg(tmp_path)
    store = DurableMem()
    store.set_worker_settings("w1", {"encode": {"ffmpeg_preset": "slow"}})
    payload = ss.settings_ui_payload(d, "global", store)
    assert payload["scope"] == "global"
    assert "w1" in payload["workers"] and payload["durable"] is True


def test_worker_save_requires_durable_store(tmp_path):
    d = _cfg(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        ss.save_settings(d, {"encode": {"ffmpeg_preset": "slow"}}, scope="w1", store=MemoryStore())


def test_clear_worker_override_removes_the_row(tmp_path):
    from blurayripper.web.app import create_app
    from blurayripper.config import load_settings
    from fastapi.testclient import TestClient

    (tmp_path / "settings.toml").write_text('[encode]\nffmpeg_preset="medium"\n', encoding="utf-8")
    app = create_app(load_settings(tmp_path))
    store = DurableMem()
    app.state.manager.store = store
    store.set_worker_settings("SteamMachine", {"encode": {"ffmpeg_preset": "slower"}})
    c = TestClient(app)

    assert c.post("/api/settings/clear", json={"scope": "SteamMachine"}).status_code == 200
    assert store.get_worker_settings("SteamMachine") == {}
    assert "SteamMachine" not in store.all_worker_settings()
    # global / non-durable are rejected
    assert c.post("/api/settings/clear", json={"scope": "global"}).status_code == 400
    from blurayripper.persistence import MemoryStore
    app.state.manager.store = MemoryStore()
    assert c.post("/api/settings/clear", json={"scope": "w1"}).status_code == 400
