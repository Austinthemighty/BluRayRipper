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


class _Log:
    def __init__(self): self.events = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a): pass
    def checkpoint(self): pass


CAPTIONS = "1\n00:00:01,869 --> 00:00:03,152\n     JUSTIN:\n     That party was a dud.\n"
DVD_TRACKS = [
    SubtitleTrack(track_id=2, codec="VobSub", language="eng", is_pgs=False),
    SubtitleTrack(track_id=5, codec="SubRip/SRT", language="spa", is_pgs=False, is_text=True),
    SubtitleTrack(track_id=6, codec="SubRip/SRT", language="eng", is_pgs=False, is_text=True),
]


def _caption_rig(tmp_path, monkeypatch, tracks=DVD_TRACKS, fail=False):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path)
    pipe = Pipeline(settings)
    extracted = []

    async def identify(mkv):
        return list(tracks)

    async def extract(mkv, specs, on_progress=None):
        if fail:
            raise RuntimeError("mkvextract: read error\nlots of\ndetail")
        for tid, out in specs:
            extracted.append(tid)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(CAPTIONS, encoding="utf-8")

    monkeypatch.setattr(pipe.mkvtools, "identify_subtitles", identify)
    monkeypatch.setattr(pipe.mkvtools, "extract_tracks", extract)
    return pipe, extracted


async def test_the_captions_are_read_from_the_default_language_text_track(
        tmp_path, monkeypatch):
    pipe, extracted = _caption_rig(tmp_path, monkeypatch)
    mkv = tmp_path / "C1_t00.mkv"; mkv.write_bytes(b"x")

    text = await pipe._caption_text(mkv, tmp_path / "sub", _Log())

    assert "That party was a dud." in text
    assert extracted == [6], "the English captions, not the Spanish or the VobSub"


async def test_captions_already_pulled_out_are_not_extracted_again(tmp_path, monkeypatch):
    """Each extraction reads the whole rip back off the NAS."""
    pipe, extracted = _caption_rig(tmp_path, monkeypatch)
    mkv = tmp_path / "C1_t00.mkv"; mkv.write_bytes(b"x")
    await pipe._caption_text(mkv, tmp_path / "sub", _Log())

    again = await pipe._caption_text(mkv, tmp_path / "sub", _Log())

    assert "That party was a dud." in again
    assert extracted == [6]


async def test_no_text_track_or_a_failed_extraction_means_no_captions(tmp_path, monkeypatch):
    """Never raises — it only feeds identification and a check — and a failure
    is said in one line."""
    mkv = tmp_path / "C1_t00.mkv"; mkv.write_bytes(b"x")
    pipe, _ = _caption_rig(tmp_path, monkeypatch, tracks=DVD_TRACKS[:1])
    assert await pipe._caption_text(mkv, tmp_path / "a", _Log()) is None

    pipe, _ = _caption_rig(tmp_path, monkeypatch, fail=True)
    log = _Log()
    assert await pipe._caption_text(mkv, tmp_path / "b", log) is None
    assert log.events == ["Could not extract the captions (RuntimeError: mkvextract: read error)."]


async def test_a_dvd_title_is_identified_from_its_captions(tmp_path, monkeypatch):
    """Job 397: no Blu-ray subtitles to OCR on a DVD, so every title said "no
    subtitles to identify with". Its captions were sitting in the rip."""
    pipe, _ = _caption_rig(tmp_path, monkeypatch)
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", titles=[Title(title_id=0, duration_seconds=1294)])
    job.selected_title_ids = [0]

    async def ripped(job, tid, sr, report):
        p = tmp_path / f"C1_t{tid:02d}.mkv"; p.write_bytes(b"x"); return p

    async def no_pgs(ripped, sub_dir, report):
        return []

    async def no_ocr(*a):
        raise AssertionError("captions are text — nothing to OCR")

    monkeypatch.setattr(pipe, "_ensure_ripped", ripped)
    monkeypatch.setattr(pipe, "_extract_subs", no_pgs)
    monkeypatch.setattr(pipe.subtitleedit, "ocr_to_srt", no_ocr)
    log = _Log()

    rips = await pipe.identify_phase(job, tmp_path / "job_1", log)

    assert "That party was a dud." in rips[0]
    assert any("closed captions" in e for e in log.events)
