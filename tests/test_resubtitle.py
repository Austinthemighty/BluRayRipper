"""Re-OCR subtitles onto an existing item (no encode): from a source MKV, from
cached subs (no re-rip), or rejected when neither is available."""

import pytest

from blurayripper.config import load_settings
from blurayripper.pipeline import Pipeline
from blurayripper.tools.mkvtools import SubtitleTrack


class _Report:
    def on_event(self, *a): pass
    def on_progress(self, *a): pass
    def checkpoint(self): pass


async def _fake_ocr(sup_path, language, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    srt = out_dir / (sup_path.stem + ".srt")
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")
    return srt


async def test_resubtitle_from_source_places_and_caches(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "out")
    pipe = Pipeline(settings)

    source = tmp_path / "Chef_t00.mkv"
    source.write_bytes(b"fake")
    video_dest = tmp_path / "lib" / "Chef (2014)" / "Chef (2014).mkv"
    video_dest.parent.mkdir(parents=True)
    video_dest.write_bytes(b"video")

    async def fake_extract(ripped, sub_dir, report):
        sub_dir.mkdir(parents=True, exist_ok=True)
        out = []
        for i, lang in ((0, "eng"), (1, "fre")):
            sup = sub_dir / f"{i}.sup"
            sup.write_bytes(b"pgs")
            out.append((sup, SubtitleTrack(track_id=i, codec="PGS", language=lang, is_pgs=True)))
        return out

    monkeypatch.setattr(pipe, "_extract_subs", fake_extract)
    monkeypatch.setattr(pipe.subtitleedit, "ocr_to_srt", _fake_ocr)

    placed = await pipe.resubtitle(video_dest, _Report(), source_mkv=source)

    assert len(placed) == 2
    names = sorted(p.name for p in placed)
    assert any(".en.default.srt" in n for n in names)
    assert any(n.endswith(".fr.srt") for n in names)
    for p in placed:
        assert p.exists() and p.parent == video_dest.parent
    # The extracted subs were cached for a future re-OCR with no source.
    assert pipe._cached_subs(video_dest) is not None


async def test_resubtitle_uses_cache_without_source(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "out")
    pipe = Pipeline(settings)
    video_dest = tmp_path / "lib" / "Chef (2014)" / "Chef (2014).mkv"
    video_dest.parent.mkdir(parents=True)
    video_dest.write_bytes(b"video")

    sup = tmp_path / "0.sup"
    sup.write_bytes(b"pgs")
    pipe._cache_subs(video_dest, [(sup, SubtitleTrack(0, "PGS", "eng", is_pgs=True))])

    extract_calls = []

    async def fake_extract(ripped, sub_dir, report):
        extract_calls.append(1)
        return []

    monkeypatch.setattr(pipe, "_extract_subs", fake_extract)
    monkeypatch.setattr(pipe.subtitleedit, "ocr_to_srt", _fake_ocr)

    placed = await pipe.resubtitle(video_dest, _Report())  # no source -> cache

    assert not extract_calls   # never re-extracted (no re-rip)
    assert len(placed) == 1


async def test_redo_subtitles_uses_staged_subs_no_rerip(tmp_path, monkeypatch):
    from blurayripper.models import EpisodeAssignment, MediaKind, Title, DiscInfo
    from blurayripper.pipeline import Job

    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "out")
    pipe = Pipeline(settings)

    # A job whose rip + .sup are still staged (encode hasn't happened).
    staging_root = settings.storage.staging_dir / "job_1"
    sup = staging_root / "sub" / "t000" / "0.sup"
    sup.parent.mkdir(parents=True)
    sup.write_bytes(b"pgs")

    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="Chef",
                        titles=[Title(title_id=0, duration_seconds=6000)])
    job.selected_title_ids = [0]
    job.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "Chef", 2014)}
    # Checkpoint points at the staged .sup (as a normal run records).
    rel = str(sup.relative_to(settings.storage.staging_dir)).replace("\\", "/")
    job.checkpoints = {0: {"subs": [{"sup": rel, "track_id": 0, "codec": "PGS",
                                     "language": "eng", "forced": False, "sdh": False,
                                     "name": None}]}}

    extract_calls = []

    async def fake_extract(ripped, sub_dir, report):
        extract_calls.append(1)
        return []

    monkeypatch.setattr(pipe, "_extract_subs", fake_extract)
    monkeypatch.setattr(pipe.subtitleedit, "ocr_to_srt", _fake_ocr)

    placed = await pipe.redo_subtitles(job, staging_root, _Report())

    assert not extract_calls   # reused the staged .sup, no re-rip/extract
    assert len(placed) == 1


async def test_resubtitle_missing_source_raises(tmp_path):
    pipe = Pipeline(load_settings())
    with pytest.raises(FileNotFoundError):
        await pipe.resubtitle(tmp_path / "v.mkv", _Report(), source_mkv=tmp_path / "nope.mkv")


async def test_resubtitle_no_cache_no_source_raises(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "out")
    pipe = Pipeline(settings)
    with pytest.raises(FileNotFoundError):
        await pipe.resubtitle(tmp_path / "v.mkv", _Report())
