"""Finished subtitles are checkpointed, so a resume does not re-OCR them.

Only the extracted .sup images were recorded before, so every requeue re-ran
OCR -- the expensive half, minutes to hours per disc -- with the finished .srt
files sitting next to the video the whole time. Job 317 did exactly that.
"""

from pathlib import Path

from blurayripper.config import load_settings
from blurayripper.pipeline import Pipeline


def _pipeline(tmp_path):
    s = load_settings()
    s.storage.output_dir = tmp_path
    return Pipeline(s)


def test_a_finished_title_is_recorded_and_skipped_next_time(tmp_path):
    p = _pipeline(tmp_path)
    srt = tmp_path / "Movie (2020).en.srt"
    srt.write_text("1")
    cp: dict = {}

    p._record_subs_done(cp, [srt])
    assert p._subs_already_done(cp) == [srt]


def test_the_record_is_relative_so_it_survives_a_move_between_machines(tmp_path):
    """Sidecar paths are stored under output_dir the way rip checkpoints are
    stored under staging -- an absolute path would strand the job on the box
    that wrote it, which is what the migration taught."""
    p = _pipeline(tmp_path)
    srt = tmp_path / "Movies" / "Movie (2020).en.srt"
    srt.parent.mkdir(parents=True)
    srt.write_text("1")
    cp: dict = {}

    p._record_subs_done(cp, [srt])
    assert cp["subs_done"] == ["Movies/Movie (2020).en.srt"]   # POSIX, relative


def test_a_deleted_sidecar_means_redo_rather_than_pretend(tmp_path):
    p = _pipeline(tmp_path)
    srt = tmp_path / "Movie (2020).en.srt"
    srt.write_text("1")
    cp: dict = {}
    p._record_subs_done(cp, [srt])

    srt.unlink()
    assert p._subs_already_done(cp) is None


def test_a_title_with_no_subtitles_counts_as_done_not_as_never_run(tmp_path):
    """A disc with no kept-language subtitles legitimately produces zero
    sidecars. If that read as "not done" it would re-OCR on every single
    resume, forever, and never be able to succeed."""
    p = _pipeline(tmp_path)
    cp: dict = {}
    p._record_subs_done(cp, [])

    assert p._subs_already_done(cp) == []      # done, with nothing
    assert p._subs_already_done({}) is None    # genuinely never run


def test_a_partially_deleted_set_redoes_the_whole_title(tmp_path):
    """Half the sidecars is not "done" -- re-OCR is the only way back."""
    p = _pipeline(tmp_path)
    a, b = tmp_path / "M.en.srt", tmp_path / "M.pl.srt"
    a.write_text("1"); b.write_text("1")
    cp: dict = {}
    p._record_subs_done(cp, [a, b])

    b.unlink()
    assert p._subs_already_done(cp) is None
