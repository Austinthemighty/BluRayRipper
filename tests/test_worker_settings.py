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


def test_job_log_endpoint_returns_full_retained_log(tmp_path):
    """The snapshot only carries the log tail; /log returns the whole retained log
    so a failure (e.g. a box-set disc that errored) can be read in full."""
    from blurayripper.web.app import create_app
    from blurayripper.config import load_settings
    from blurayripper.web.manager import WebJob
    from blurayripper.pipeline import JobState
    from fastapi.testclient import TestClient

    (tmp_path / "settings.toml").write_text("", encoding="utf-8")
    app = create_app(load_settings(tmp_path))
    mgr = app.state.manager
    wj = WebJob(id=7, source="disc:0", state=JobState.FAILED)
    wj.log = [f"line {i}" for i in range(120)]   # more than the snapshot tail
    mgr.jobs[7] = wj
    c = TestClient(app)

    r = c.get("/api/jobs/7/log")
    assert r.status_code == 200
    assert r.json()["log"] == wj.log            # full 120 lines, not just the tail
    assert c.get("/api/jobs/999/log").status_code == 404


def _boxset_app(tmp_path):
    from blurayripper.config import load_settings
    from blurayripper.web.app import create_app

    (tmp_path / "settings.toml").write_text("", encoding="utf-8")
    settings = load_settings(tmp_path)
    settings.config_dir = tmp_path        # keep the plan file out of the real config dir
    return create_app(settings)


def test_boxset_plan_saves_and_reloads(tmp_path):
    from fastapi.testclient import TestClient

    c = TestClient(_boxset_app(tmp_path))
    assert c.get("/api/boxsets").json()["csv"] == ""      # nothing saved yet

    csv_text = "disc_label,show,year\nHAWAII_FIVE_0_S1_*,Hawaii Five-0,2010\n"
    r = c.post("/api/boxsets", json={"csv": csv_text}).json()
    assert r["saved"] is True and r["errors"] == []
    assert r["rows"][0]["show"] == "Hawaii Five-0"
    assert (tmp_path / "boxsets.local.csv").read_text(encoding="utf-8") == csv_text

    back = c.get("/api/boxsets").json()
    assert back["csv"] == csv_text and back["rows"][0]["disc_label"] == "HAWAII_FIVE_0_S1_*"


def test_boxset_plan_dry_run_does_not_write(tmp_path):
    """Preview must show what would happen without changing what discs do next."""
    from fastapi.testclient import TestClient

    c = TestClient(_boxset_app(tmp_path))
    r = c.post("/api/boxsets", json={"csv": "disc_label,show\nA_S1_D1,Show\n",
                                     "dry_run": True}).json()
    assert r["saved"] is False and len(r["rows"]) == 1
    assert not (tmp_path / "boxsets.local.csv").exists()


def test_boxset_plan_reports_bad_csv(tmp_path):
    from fastapi.testclient import TestClient

    c = TestClient(_boxset_app(tmp_path))
    r = c.post("/api/boxsets", json={"csv": "nope,nothing\nx,y\n"}).json()
    assert r["errors"] and any("disc_label" in e for e in r["errors"])
    assert r["rows"] == []
    assert c.post("/api/boxsets", json={"csv": 5}).status_code == 400


def test_job_endpoint_errors_map_to_http(tmp_path):
    """Every job endpoint funnels its errors through one helper: an unknown job id
    is 404, a rejected request is 400 (with the manager's reason). Covers both a
    sync endpoint and an awaiting one, since the helper wraps both."""
    from blurayripper.config import load_settings
    from blurayripper.pipeline import Job, JobState
    from blurayripper.web.app import create_app
    from blurayripper.web.manager import WebJob
    from fastapi.testclient import TestClient

    (tmp_path / "settings.toml").write_text("", encoding="utf-8")
    app = create_app(load_settings(tmp_path))
    c = TestClient(app)

    # Unknown job -> 404, sync and async paths alike.
    r = c.post("/api/jobs/999/retry")
    assert r.status_code == 404 and r.json()["detail"] == "Job not found."
    assert c.post("/api/jobs/999/preview-grain").status_code == 404

    # Known job in the wrong state -> 400, carrying the manager's explanation.
    wj = WebJob(id=5, source="disc:0", state=JobState.READY)
    wj._pipeline_job = Job(source="disc:0")
    wj._pipeline_job.state = JobState.READY
    app.state.manager.jobs[5] = wj
    r = c.post("/api/jobs/5/retry")          # only a FAILED job can be retried
    assert r.status_code == 400 and "not failed" in r.json()["detail"]


def test_search_endpoint_honours_quoted_exact_queries(tmp_path, monkeypatch):
    """The search box is where quoting matters — guard that the endpoint keeps
    routing through the cleaner (and so keeps honouring quotes)."""
    import blurayripper.services.metadata as md
    from blurayripper.config import load_settings
    from fastapi.testclient import TestClient

    seen = []

    async def fake_search(self, query, year=None):
        seen.append(query)
        return []
    monkeypatch.setattr(md.TMDBClient, "search_movie", fake_search)

    from blurayripper.web.app import create_app
    (tmp_path / "settings.toml").write_text("", encoding="utf-8")
    settings = load_settings(tmp_path)
    settings.tmdb_api_key = "x"
    c = TestClient(create_app(settings))

    c.get("/api/tmdb/search", params={"kind": "movie", "query": "My Pal"})
    assert seen[-1] == "My"                    # cleaned: trailing PAL looks like noise

    c.get("/api/tmdb/search", params={"kind": "movie", "query": '"My Pal"'})
    assert seen[-1] == "My Pal"                # quoted: sent exactly as typed


def test_worker_action_endpoint_targets_one_worker(tmp_path):
    """Per-worker control over HTTP: name a worker, or "*" for all."""
    from blurayripper.config import load_settings
    from blurayripper.persistence import WorkerInfo, signal_key
    from blurayripper.web.app import create_app
    from fastapi.testclient import TestClient

    (tmp_path / "settings.toml").write_text("", encoding="utf-8")
    app = create_app(load_settings(tmp_path))
    store = DurableMem()
    app.state.manager.store = store
    app.state.manager._workers_cache = [
        WorkerInfo("SteamMachine", "v1", None, "idle", 1.0),
        WorkerInfo("desktop-01", "v1", None, "idle", 1.0),
    ]
    c = TestClient(app)

    r = c.post("/api/workers/SteamMachine/action", json={"action": "drain"})
    assert r.status_code == 200 and r.json()["workers"] == 1
    assert store.cluster_signals()[signal_key("drain", "SteamMachine")] == 1

    r = c.post("/api/workers/*/action", json={"action": "update"})
    assert r.status_code == 200 and r.json()["workers"] == 2

    # Bad action and unknown worker are rejected, not silently queued.
    assert c.post("/api/workers/*/action", json={"action": "explode"}).status_code == 400
    assert c.post("/api/workers/TYPO/action", json={"action": "drain"}).status_code == 400
