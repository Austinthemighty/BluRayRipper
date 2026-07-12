"""identify_phase rips every title BEFORE any OCR, so the optical drive never
idles mid-job (some drives spin down between reads and fail to resume)."""

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, Title
from blurayripper.pipeline import Job, Pipeline
from blurayripper.tools.mkvtools import SubtitleTrack


class _R:
    def on_event(self, *a): pass
    def on_progress(self, *a): pass
    def checkpoint(self): pass


async def test_identify_rips_all_before_any_ocr(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path)
    pipe = Pipeline(settings)
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0",
                        titles=[Title(title_id=i, duration_seconds=1500) for i in (0, 1, 2)])
    job.selected_title_ids = [0, 1, 2]

    order = []

    async def fake_ripped(job, tid, sr, report):
        order.append(("rip", tid))
        p = tmp_path / f"t{tid}.mkv"; p.write_bytes(b"x"); return p

    async def fake_extract(ripped, sub_dir, report):
        sub_dir.mkdir(parents=True, exist_ok=True)
        sup = sub_dir / "s.sup"; sup.write_bytes(b"x")
        return [(sup, SubtitleTrack(track_id=0, codec="PGS", language="eng", is_pgs=True))]

    async def fake_ocr(sup, lang, out_dir):
        order.append(("ocr",))
        srt = out_dir / "s.srt"; srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
        return srt

    monkeypatch.setattr(pipe, "_ensure_ripped", fake_ripped)
    monkeypatch.setattr(pipe, "_extract_subs", fake_extract)
    monkeypatch.setattr(pipe.subtitleedit, "ocr_to_srt", fake_ocr)

    rips = await pipe.identify_phase(job, tmp_path / "job_1", _R())

    assert set(rips) == {0, 1, 2}
    rip_positions = [i for i, e in enumerate(order) if e[0] == "rip"]
    ocr_positions = [i for i, e in enumerate(order) if e[0] == "ocr"]
    # every rip happens before every OCR (drive stays busy, then OCR)
    assert max(rip_positions) < min(ocr_positions)
    # rips run in title order, back to back
    assert [e[1] for e in order if e[0] == "rip"] == [0, 1, 2]
