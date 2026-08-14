"""A job must not report Done until its files are actually on disk.

Every stage reports its own success and none of them looks at the finished
folder, so a partial failure still finished green. Job 42 is the real case:
subtitle OCR died, the encode carried on, the video was placed with no sidecars,
and nothing said so.
"""

from pathlib import Path

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, Stream, StreamType, Title
from blurayripper.pipeline import Job, Pipeline, TitleResult


def _pipeline(tmp_path, **over):
    s = load_settings()
    s.storage.output_dir = tmp_path
    s.write_nfo = over.pop("write_nfo", False)
    s.languages.keep = over.pop("keep", ["eng"])
    return Pipeline(s)


def _job(video: Path | None, subs=(), *, sub_langs=()):
    job = Job(source="disc:0")
    streams = [Stream(stream_id=i, stream_type=StreamType.SUBTITLE, language=l)
               for i, l in enumerate(sub_langs)]
    job.disc = DiscInfo(source="disc:0", disc_name="X", titles=[
        Title(title_id=0, duration_seconds=100, size_bytes=1,
              source_filename="t00.mkv", streams=streams)])
    job.results = [TitleResult(title_id=0, video_path=video,
                               subtitle_paths=list(subs))]
    return job


def test_a_complete_job_verifies_clean(tmp_path):
    v = tmp_path / "Movie (2020).mkv"; v.write_bytes(b"video")
    srt = tmp_path / "Movie (2020).en.srt"; srt.write_text("1")
    assert _pipeline(tmp_path)._verify_outputs(
        _job(v, [srt], sub_langs=["eng"])) == []


def test_the_job_42_case_subtitles_that_never_got_produced(tmp_path):
    """The disc carried English subtitles and none came out. There is no path to
    check here — OCR failed before producing one — so the only evidence is the
    disc's tracks versus an empty result."""
    v = tmp_path / "The Grinch (2018).mkv"; v.write_bytes(b"video")

    problems = _pipeline(tmp_path)._verify_outputs(_job(v, [], sub_langs=["eng"]))

    assert len(problems) == 1
    assert "carries eng subtitle track(s)" in problems[0]
    assert "no .srt was produced" in problems[0]


def test_a_disc_with_no_subtitles_at_all_is_not_flagged(tmp_path):
    """Plenty of discs genuinely have none. Demanding a sidecar there would fail
    every one of them."""
    v = tmp_path / "Movie (2020).mkv"; v.write_bytes(b"video")
    assert _pipeline(tmp_path)._verify_outputs(_job(v, [])) == []


def test_subtitles_only_in_languages_we_do_not_keep_are_not_expected(tmp_path):
    v = tmp_path / "Movie (2020).mkv"; v.write_bytes(b"video")
    assert _pipeline(tmp_path, keep=["eng"])._verify_outputs(
        _job(v, [], sub_langs=["pol", "fra"])) == []


def test_a_video_that_vanished_after_placing_is_caught(tmp_path):
    missing = tmp_path / "Gone (2020).mkv"
    problems = _pipeline(tmp_path)._verify_outputs(_job(missing, []))
    assert any("missing video" in p for p in problems)


def test_a_truncated_zero_byte_video_is_caught(tmp_path):
    """A 0-byte file exists, so an existence check alone would pass it."""
    v = tmp_path / "Empty (2020).mkv"; v.write_bytes(b"")
    problems = _pipeline(tmp_path)._verify_outputs(_job(v, []))
    assert any("0 bytes" in p for p in problems)


def test_a_subtitle_that_was_placed_but_is_gone_is_caught(tmp_path):
    v = tmp_path / "Movie (2020).mkv"; v.write_bytes(b"video")
    problems = _pipeline(tmp_path)._verify_outputs(
        _job(v, [tmp_path / "Movie (2020).en.srt"], sub_langs=["eng"]))
    assert any("missing subtitle" in p for p in problems)


def test_a_missing_nfo_is_caught_only_when_nfo_writing_is_on(tmp_path):
    v = tmp_path / "Movie (2020).mkv"; v.write_bytes(b"video")
    assert _pipeline(tmp_path, write_nfo=False)._verify_outputs(_job(v, [])) == []
    problems = _pipeline(tmp_path, write_nfo=True)._verify_outputs(_job(v, []))
    assert any(".nfo" in p for p in problems)


def test_a_job_that_produced_nothing_is_never_done(tmp_path):
    job = _job(None, [])
    job.results = []
    assert _pipeline(tmp_path)._verify_outputs(job) == [
        "nothing was produced — no titles were processed"]
