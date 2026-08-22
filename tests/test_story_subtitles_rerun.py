"""Journey: "The subtitles are wrong and I am not re-ripping a disc for them."

This is the flow that recovers a cluster whose OCR tool was broken for days: the
rips are fine, the videos are placed, only the .srt sidecars are missing or
garbage. Re-ripping twenty discs to fix them is hours of drive time for work
already done, so what matters here is precisely what is and is not redone —
which is why these tests count tool invocations rather than trusting a log line.
"""

import asyncio

import pytest

from blurayripper.config import load_settings
from blurayripper.pipeline import Job, Pipeline
from blurayripper.tools.mkvtools import SubtitleTrack


class _Report:
    def __init__(self): self.events = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a, **k): pass
    def checkpoint(self): pass
    def notify(self, *a, **k): pass
    def ejected(self, *a, **k): pass


def _pipe(tmp_path):
    s = load_settings()
    s.storage.output_dir = tmp_path / "out"
    s.storage.staging_dir = tmp_path / "stage"
    s.storage.output_dir.mkdir(parents=True, exist_ok=True)
    s.languages.keep = ["eng"]
    s.languages.default = "eng"
    s.subtitles.keep_extracted = True
    return Pipeline(s)


def _track(i=0, lang="eng"):
    return SubtitleTrack(track_id=i, codec="S_HDMV/PGS", is_pgs=True,
                         language=lang, forced=False, sdh=False)


def _placed_video(p, name="Film (2019).mkv"):
    video = p.settings.storage.output_dir / name
    video.write_bytes(b"video")
    return video


def _cache_one_sup(p, video, text=b"pgs-bitmaps"):
    """Put a .sup in the cache the way a completed rip would have."""
    sup = p.settings.storage.staging_dir / "orig" / "t000.sup"
    sup.parent.mkdir(parents=True, exist_ok=True)
    sup.write_bytes(text)
    p._cache_subs(video, [(sup, _track())])
    return sup


# --- the point of the whole feature: no disc, no re-rip ----------------------

def test_a_re_ocr_uses_the_cached_images_and_never_asks_for_the_disc(
        tmp_path, monkeypatch):
    """The .sup images were kept precisely so this needs no drive. If it
    extracted again it would need the original rip — which on a cleaned-up
    staging dir means re-ripping the disc."""
    p = _pipe(tmp_path)
    video = _placed_video(p)
    _cache_one_sup(p, video)
    extracted = []

    async def never_extract(*a, **k):
        extracted.append(a)
        return []

    async def ocr(job, todo, report):
        out = {}
        for tid, _a, _r, sub_dir, sub_jobs in todo:
            srts = []
            for i, (sup, track) in enumerate(sub_jobs):
                srt = sub_dir / f"{i}.srt"
                srt.parent.mkdir(parents=True, exist_ok=True)
                srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
                srts.append((srt, track))
            out[tid] = srts
        return out

    monkeypatch.setattr(p, "_extract_subs", never_extract)
    monkeypatch.setattr(p, "_ocr_all", ocr)

    placed = asyncio.run(p.resubtitle(video, _Report()))

    assert extracted == []                     # the disc was never needed
    assert placed and all(pth.exists() for pth in placed)
    assert placed[0].parent == video.parent    # sidecars land beside the video
    assert placed[0].name.startswith(video.stem)


def test_without_a_cache_it_says_so_instead_of_silently_doing_nothing(tmp_path):
    """A job ripped before caching existed has no .sup to reuse. Returning an
    empty list would read as success and leave the video with no subtitles."""
    p = _pipe(tmp_path)
    video = _placed_video(p)

    with pytest.raises(FileNotFoundError, match="No cached subtitles"):
        asyncio.run(p.resubtitle(video, _Report()))


def test_a_named_source_mkv_that_is_gone_is_named_in_the_error(tmp_path):
    """The rip may have been cleaned up. Saying which file is missing is the
    difference between a two-second fix and a re-rip."""
    p = _pipe(tmp_path)
    video = _placed_video(p)
    missing = tmp_path / "gone" / "t00.mkv"

    with pytest.raises(FileNotFoundError, match="t00.mkv"):
        asyncio.run(p.resubtitle(video, _Report(), source_mkv=missing))


# --- what a retry must and must not redo -------------------------------------

def test_a_failed_ocr_leaves_the_work_to_be_redone(tmp_path, monkeypatch):
    """_ocr_all drops a track whose OCR raised, so the result is empty — the
    same shape as a disc with no subtitles. Recording that as done is what
    sealed these jobs as 'nothing to do' while their encode marked them placed."""
    p = _pipe(tmp_path)
    job = Job(source="disc:0")
    cp = job.checkpoints.setdefault(0, {})

    p._record_if_complete(cp, extracted=[( tmp_path / "a.sup", _track())],
                          pairs=[], result=_Res(), report=_Report())

    assert "subs_done" not in cp
    assert p._subs_already_done(cp) is None      # so the retry re-runs it


def test_a_title_that_genuinely_has_no_subtitles_is_left_alone(tmp_path):
    """The distinction that makes the retry safe: nothing extracted and nothing
    OCR'd is complete. Treating it as failed would re-run OCR on every resume
    forever and never be able to succeed."""
    p = _pipe(tmp_path)
    job = Job(source="disc:0")
    cp = job.checkpoints.setdefault(0, {})

    p._record_if_complete(cp, extracted=[], pairs=[], result=_Res(),
                          report=_Report())

    assert cp["subs_done"] == []
    assert p._subs_already_done(cp) == []


def test_a_completed_title_is_skipped_on_the_next_pass(tmp_path, monkeypatch):
    """The payoff for the whole checkpoint: a requeued job must not re-OCR
    tracks whose sidecars are sitting next to the video."""
    p = _pipe(tmp_path)
    job = Job(source="disc:0")
    srt = p.settings.storage.output_dir / "Film (2019).en.srt"
    srt.write_text("x")
    job.checkpoints[0] = {"subs_done": ["Film (2019).en.srt"]}
    touched = []

    async def boom(*a, **k):
        touched.append("ran")
        return []

    monkeypatch.setattr(p, "_extract_title_subs", boom)
    monkeypatch.setattr(p, "_ocr_all", boom)

    rep = _Report()
    asyncio.run(p._subtitles_for_titles(
        job, [(0, _Assign(), tmp_path / "r.mkv", tmp_path / "s")], rep))

    assert touched == []
    assert any("already done" in e for e in rep.events)


# --- and the audit that tells you which jobs still need this -----------------

def test_the_audit_names_the_jobs_whose_subtitles_never_landed(tmp_path):
    """After an OCR outage you need the list. A DONE job whose disc carried a
    kept language but has no sidecar beside its video is exactly the one to
    re-run — and it is indistinguishable from a healthy job in the UI."""
    from blurayripper import subtitle_audit

    sidecars = {"/lib/Good.mkv": ["Good.en.srt"], "/lib/Bad.mkv": []}
    records = [
        _done_record(1, "Good", ["/lib/Good.mkv"]),
        _done_record(2, "Bad", ["/lib/Bad.mkv"]),
    ]

    result = subtitle_audit.audit_done_jobs(
        records, is_kept=lambda lang: lang == "eng",
        list_sidecars=lambda v: sidecars.get(v))

    by_id = {r["id"]: r for r in result}
    assert by_id[1]["status"] == "ok"
    assert by_id[2]["status"] == "missing"      # expected subs, none on disk


def test_a_library_the_box_cannot_reach_is_not_reported_as_missing(tmp_path):
    """An unmapped NAS makes every video look sidecar-less. Calling that
    "missing" would send the user re-OCR'ing a library that is actually fine —
    the audit has to distinguish "checked and absent" from "could not check"."""
    from blurayripper import subtitle_audit

    result = subtitle_audit.audit_done_jobs(
        [_done_record(3, "Unreachable", ["/mnt/gone/Film.mkv"])],
        is_kept=lambda lang: lang == "eng",
        list_sidecars=lambda v: None)          # None = folder unreachable

    assert result[0]["status"] == "unreachable"


def test_a_disc_with_no_kept_language_subtitles_is_not_flagged(tmp_path):
    """Plenty of discs carry nothing we keep. Flagging those would bury the real
    failures in noise."""
    from blurayripper import subtitle_audit

    result = subtitle_audit.audit_done_jobs(
        [_done_record(4, "NoSubs", ["/lib/NoSubs.mkv"], sub_lang="fra")],
        is_kept=lambda lang: lang == "eng",
        list_sidecars=lambda v: [])

    assert result[0]["expected"] is False and result[0]["status"] == "ok"


def _done_record(job_id, name, outputs, sub_lang="eng"):
    from blurayripper.persistence import JobRecord
    return JobRecord(
        id=job_id, source="disc:0", state="done",
        disc={"source": "disc:0", "disc_name": name, "titles": [
            {"title_id": 0, "duration_seconds": 100, "size_bytes": 1,
             "chapter_count": 1, "source_filename": "t00.mkv", "streams": [
                 {"stream_id": 1, "type": "subtitle",
                  "language": sub_lang}]}]},
        selected_title_ids=[0], outputs=outputs)


class _Res:
    subtitle_paths: list = []


class _Assign:
    title_id = 0
    kind = None
    show_or_movie_title = "Film"
    year = 2019
    season = None
    episode = None
    episode_title = None
    edition = None
