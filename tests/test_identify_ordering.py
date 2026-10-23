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


CAPTIONS = "1\n00:00:01,869 --> 00:00:03,152\n     MARA:\n     The ferry left without us.\n"
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

    assert "The ferry left without us." in text
    assert extracted == [6], "the English captions, not the Spanish or the VobSub"


async def test_captions_already_pulled_out_are_not_extracted_again(tmp_path, monkeypatch):
    """Each extraction reads the whole rip back off the NAS."""
    pipe, extracted = _caption_rig(tmp_path, monkeypatch)
    mkv = tmp_path / "C1_t00.mkv"; mkv.write_bytes(b"x")
    await pipe._caption_text(mkv, tmp_path / "sub", _Log())

    again = await pipe._caption_text(mkv, tmp_path / "sub", _Log())

    assert "The ferry left without us." in again
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

    assert "The ferry left without us." in rips[0]
    assert any("closed captions" in e for e in log.events)


# --- the captions become the subtitles of a DVD episode ------------------------------------

def _sub_rig(tmp_path, monkeypatch, *, extracted=(), captions=RuntimeError, lang="eng"):
    """The subtitle step for one TV title, with the disc parts stood in for."""
    from blurayripper.models import EpisodeAssignment, MediaKind
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "out")
    pipe = Pipeline(settings)
    asked = []

    async def extract(job, tid, ripped, sub_dir, report):
        return list(extracted)

    async def caption_srt(ripped, sub_dir):
        asked.append(tid := 0)
        if captions is RuntimeError:
            raise RuntimeError("mkvextract: read error")
        if captions is None:
            return None
        sub_dir.mkdir(parents=True, exist_ok=True)
        raw = sub_dir / "C1_t00.t6.eng.captions.srt"
        raw.write_text(captions, encoding="utf-8")
        return raw, SubtitleTrack(track_id=6, codec="SubRip/SRT", language=lang,
                                  is_pgs=False, is_text=True)

    monkeypatch.setattr(pipe, "_extract_title_subs", extract)
    monkeypatch.setattr(pipe, "_caption_srt", caption_srt)
    job = Job(source="disc:0")
    a = EpisodeAssignment(0, MediaKind.TV, "How I Met Your Mother", 2005, season=9, episode=17)
    job.assignments = {0: a}
    todo = [(0, a, tmp_path / "C1_t00.mkv", tmp_path / "stage" / "job_1" / "sub" / "t000")]
    return pipe, job, todo, asked


DVD_CAPTIONS = ("1\n00:00:01,869 --> 00:00:03,152\n   SAM:   \n   The lights went out.   \n\n\n"
            + "".join(f"{n}\n00:00:{n:02d},000 --> 00:00:{n:02d},500\n   [THUNDER]   \n\n"
                      for n in range(2, 50)))


async def test_a_dvd_episode_gets_its_captions_as_an_sdh_subtitle(tmp_path, monkeypatch):
    """Every DVD episode in the library had no subtitles at all: HandBrake keeps
    none (subtitles are sidecars), and only Blu-ray subtitles were OCR'd."""
    pipe, job, todo, _asked = _sub_rig(tmp_path, monkeypatch, captions=DVD_CAPTIONS)
    log = _Log()

    await pipe._subtitles_for_titles(job, todo, log)

    srt = (tmp_path / "out" / "TV Shows" / "How I Met Your Mother (2005)" / "Season 09"
           / "How I Met Your Mother - S09E17.en.sdh.srt")
    assert srt.read_text(encoding="utf-8").startswith(
        "1\n00:00:01,869 --> 00:00:03,152\nSAM:\nThe lights went out.\n")
    assert job.checkpoints[0]["subs_done"]                  # done: no redo on resume
    assert any("closed captions as its subtitles" in e for e in log.events)


async def test_captions_that_could_not_be_extracted_are_tried_again(tmp_path, monkeypatch):
    """Not recorded as done with nothing, or a NAS hiccup would leave the episode
    without subtitles for good."""
    pipe, job, todo, _asked = _sub_rig(tmp_path, monkeypatch, captions=RuntimeError)
    log = _Log()

    await pipe._subtitles_for_titles(job, todo, log)

    assert "subs_done" not in job.checkpoints[0]
    assert any("tried again on a retry" in e for e in log.events)


async def test_no_captions_or_captions_in_an_unkept_language_place_nothing(
        tmp_path, monkeypatch):
    for captions, lang in ((None, "eng"), (DVD_CAPTIONS, "fre")):
        pipe, job, todo, _asked = _sub_rig(tmp_path / lang, monkeypatch,
                                           captions=captions, lang=lang)
        await pipe._subtitles_for_titles(job, todo, _Log())
        assert job.checkpoints[0]["subs_done"] == []          # done, with none
        assert not list((tmp_path / lang / "out").rglob("*.srt"))


async def test_a_title_with_blu_ray_subtitles_does_not_look_for_captions(
        tmp_path, monkeypatch):
    sup = tmp_path / "s.sup"; sup.write_bytes(b"x")
    pipe, job, todo, asked = _sub_rig(
        tmp_path, monkeypatch, captions=DVD_CAPTIONS,
        extracted=[(sup, SubtitleTrack(track_id=3, codec="PGS", language="eng", is_pgs=True))])

    async def ocr(job, todo, report):
        return {0: []}
    monkeypatch.setattr(pipe, "_ocr_all", ocr)

    await pipe._subtitles_for_titles(job, todo, _Log())

    assert asked == []


async def test_captions_are_named_sdh_even_when_they_read_like_plain_dialogue(
        tmp_path, monkeypatch):
    """Content sniffing only calls a track SDH past a share of sound cues and
    speaker names. Closed captions are made for deaf and hard-of-hearing viewers
    either way, and the file says so."""
    plain = "".join(f"{n}\n00:00:{n:02d},000 --> 00:00:{n:02d},500\n   Line {n} of talk.   \n\n"
                    for n in range(1, 50))
    pipe, job, todo, _asked = _sub_rig(tmp_path, monkeypatch, captions=plain)

    await pipe._subtitles_for_titles(job, todo, _Log())

    placed = [p.name for p in (tmp_path / "out").rglob("*.srt")]
    assert placed == ["How I Met Your Mother - S09E17.en.sdh.srt"]
