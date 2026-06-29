"""Re-OCR subtitles onto an existing item (no encode): extracts from a source
MKV, OCRs, and places .srt sidecars next to the destination video."""

import pytest

from blurayripper.config import load_settings
from blurayripper.pipeline import Pipeline
from blurayripper.tools.mkvtools import SubtitleTrack


class _Report:
    def on_event(self, *a): pass
    def on_progress(self, *a): pass
    def checkpoint(self): pass


async def test_resubtitle_places_sidecars(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")
    pipe = Pipeline(settings)

    source = tmp_path / "Chef_t00.mkv"
    source.write_bytes(b"fake")
    video_dest = tmp_path / "lib" / "Chef (2014)" / "Chef (2014).mkv"
    video_dest.parent.mkdir(parents=True)
    video_dest.write_bytes(b"video")

    async def fake_extract(ripped, sub_dir, report):
        sub_dir.mkdir(parents=True, exist_ok=True)
        return [
            (sub_dir / "0.sup", SubtitleTrack(track_id=0, codec="PGS", language="eng", is_pgs=True)),
            (sub_dir / "1.sup", SubtitleTrack(track_id=1, codec="PGS", language="fre", is_pgs=True)),
        ]

    async def fake_ocr(sup_path, language, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        srt = out_dir / (sup_path.stem + ".srt")
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")
        return srt

    monkeypatch.setattr(pipe, "_extract_subs", fake_extract)
    monkeypatch.setattr(pipe.subtitleedit, "ocr_to_srt", fake_ocr)

    placed = await pipe.resubtitle(source, video_dest, _Report())

    assert len(placed) == 2
    names = sorted(p.name for p in placed)
    # Default-language (eng) sidecar gets the .en.default tag; fre is the other.
    assert any(".en.default.srt" in n for n in names)
    assert any(n.endswith(".fr.srt") for n in names)
    for p in placed:
        assert p.exists() and p.parent == video_dest.parent


async def test_resubtitle_missing_source_raises(tmp_path):
    pipe = Pipeline(load_settings())
    with pytest.raises(FileNotFoundError):
        await pipe.resubtitle(tmp_path / "nope.mkv", tmp_path / "v.mkv", _Report())
