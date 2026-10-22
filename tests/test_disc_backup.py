"""Reading a disc once, then making each title from that copy.

MakeMKV re-opens and re-scans the disc for every title, and `makemkvcon mkv`
cannot be told to rip a chosen subset in one invocation — it takes one title
index or `all`. On an eight-episode DVD on the live box that cost 39% of the
job: 503 seconds re-opening against 791 copying, with the opens getting slower
as it went (54s for the first titles, 177s for the last).

So the disc is copied once and the ordinary per-title rip runs against the copy.
The risk this trades for is worse than slowness, which is why most of this file
is about refusing: titles are ripped BY INDEX, so a copy numbered differently
from the disc would file every episode under the wrong name.
"""

from pathlib import Path

import pytest

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.pipeline import Job, Pipeline


class _Report:
    def __init__(self): self.events, self.progress = [], []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, stage, frac=None): self.progress.append(stage)
    def checkpoint(self): pass

    @property
    def text(self): return "\n".join(self.events)


def _titles(*specs):
    """(title_id, seconds, source_filename) triples -> Title objects."""
    return [Title(title_id=t, duration_seconds=d, source_filename=f)
            for t, d, f in specs]


def _job(titles, selected=None):
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="HIMYM_S9_D1",
                        drive_device="/dev/sr0", titles=list(titles))
    job.selected_title_ids = list(selected if selected is not None
                                  else [t.title_id for t in titles])
    job.assignments = {
        t.title_id: EpisodeAssignment(
            title_id=t.title_id, kind=MediaKind.TV,
            show_or_movie_title="How I Met Your Mother", season=9,
            episode=t.title_id)
        for t in titles}
    return job


def _pipe(tmp_path, monkeypatch, *, backup_first=True):
    s = load_settings()
    s.backup_first = backup_first
    s.auto_eject = True
    s.rip_duration_tolerance_pct = 0      # the duration check has its own tests
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    return Pipeline(s)


def _wire(pipe, monkeypatch, *, copy_titles=None, backup_fails=False):
    """Stand in for the disc: record the backup, and what each rip read from."""
    seen = {"backups": [], "rips": [], "ejected": []}

    async def backup_disc(source, dest_dir, on_progress=None, on_message=None):
        seen["backups"].append((source, Path(dest_dir)))
        if backup_fails:
            raise RuntimeError("drive stopped answering")
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        (Path(dest_dir) / "BDMV").mkdir(exist_ok=True)

    async def scan(source, on_message=None):
        return DiscInfo(source=source, disc_name="copy",
                        titles=list(copy_titles or []))

    async def rip_title(source, title_id, rip_dir, on_progress=None, on_message=None):
        seen["rips"].append((source, title_id))
        Path(rip_dir).mkdir(parents=True, exist_ok=True)
        out = Path(rip_dir) / f"t{title_id}.mkv"
        out.write_bytes(b"\x1aE\xdf\xa3")
        return [out]

    monkeypatch.setattr(pipe.makemkv, "backup_disc", backup_disc)
    monkeypatch.setattr(pipe.makemkv, "scan", scan)
    monkeypatch.setattr(pipe.makemkv, "rip_title", rip_title)
    monkeypatch.setattr("blurayripper.pipeline.eject",
                        lambda dev: seen["ejected"].append(dev))

    async def dev(_job): return "/dev/sr0"
    monkeypatch.setattr(pipe, "_eject_device_for", dev)
    return seen


DISC = (( 0, 1292, None), (1, 1252, None), (2, 1293, None))


async def test_the_disc_is_read_once_and_every_title_comes_from_the_copy(
        tmp_path, monkeypatch):
    """The point of the exercise: one disc read, then the same per-title loop as
    ever — same number of rip calls, same title ids, same checkpoints — just
    against a source that does not need re-opening."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job(_titles(*DISC))
    seen = _wire(pipe, monkeypatch, copy_titles=_titles(*DISC))
    r = _Report()

    await pipe._prepare_disc_once(job, tmp_path / "job_1", r)
    for tid in job.selected_title_ids:
        await pipe._ensure_ripped(job, tid, tmp_path / "job_1", r)

    assert len(seen["backups"]) == 1
    assert seen["backups"][0][0] == "disc:0"
    # Still one rip per title, in order — just reading from the copy.
    assert [tid for _src, tid in seen["rips"]] == [0, 1, 2]
    assert {src for src, _tid in seen["rips"]} == {f"file:{tmp_path / 'job_1' / 'disc'}"}
    assert all(job.checkpoints[t]["ripped_path"] for t in (0, 1, 2))


async def test_the_disc_is_ejected_before_the_first_title_not_after_the_last(
        tmp_path, monkeypatch):
    """The user-visible half of this: the disc is finished with at the START of
    the job, so the next one can go in while the titles are still being made."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job(_titles(*DISC))
    seen = _wire(pipe, monkeypatch, copy_titles=_titles(*DISC))
    r = _Report()

    await pipe._prepare_disc_once(job, tmp_path / "job_1", r)
    assert seen["ejected"] == ["/dev/sr0"], "the disc should already be free"

    for tid in job.selected_title_ids:
        await pipe._ensure_ripped(job, tid, tmp_path / "job_1", r)
    assert seen["ejected"] == ["/dev/sr0"], "and must not be ejected a second time"


@pytest.mark.parametrize("copy, why", [
    (((0, 1292, None), (1, 1252, None)), "has 2 titles"),
    (((0, 1292, None), (1, 1252, None), (2, 9999, None)), "runs 9999s"),
    (((0, 1292, "00800.mpls"), (1, 1252, "00801.mpls"), (2, 1293, "00999.mpls")),
     "00999.mpls"),
])
async def test_a_copy_that_does_not_line_up_with_the_disc_is_thrown_away(
        tmp_path, monkeypatch, copy, why):
    """Titles are ripped by INDEX. A copy numbered differently from the disc
    would put every episode under the wrong name — and each file would be a
    real, playable episode, so nothing downstream would notice.

    Fewer titles, a title of the wrong length, or (on Blu-ray, where titles
    carry one) a different source filename all mean this is not the same disc
    laid out the same way."""
    disc = _titles((0, 1292, "00800.mpls"), (1, 1252, "00801.mpls"),
                   (2, 1293, "00802.mpls"))
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job(disc)
    seen = _wire(pipe, monkeypatch, copy_titles=_titles(*copy))
    r = _Report()

    await pipe._prepare_disc_once(job, tmp_path / "job_1", r)

    assert getattr(job, "_rip_source", None) is None
    assert why in r.text and "discarding it" in r.text
    assert not (tmp_path / "job_1" / "disc").exists(), "the bad copy must not linger"
    # ...and the rips fall back to the disc, which is slower but correct.
    await pipe._ensure_ripped(job, 0, tmp_path / "job_1", r)
    assert seen["rips"] == [("disc:0", 0)]


async def test_a_backup_that_fails_just_means_ripping_from_the_disc(
        tmp_path, monkeypatch):
    """This is an optimisation. It must never be the reason a disc does not get
    ripped — a drive that cannot be copied in one pass can still be read title
    by title, which is exactly what it did before this existed."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job(_titles(*DISC))
    seen = _wire(pipe, monkeypatch, copy_titles=_titles(*DISC), backup_fails=True)
    r = _Report()

    await pipe._prepare_disc_once(job, tmp_path / "job_1", r)

    assert getattr(job, "_rip_source", None) is None
    assert "drive stopped answering" in r.text
    assert "ripping title by title" in r.text
    assert seen["ejected"] == [], "nothing was copied, so the disc is still needed"
    await pipe._ensure_ripped(job, 0, tmp_path / "job_1", r)
    assert seen["rips"] == [("disc:0", 0)]


async def test_a_disc_too_big_for_the_space_left_is_ripped_the_old_way(
        tmp_path, monkeypatch):
    """A copy needs room for the whole disc AND the titles beside it. Filling
    staging would break every other job on the box, not just this one."""
    import shutil as _shutil

    pipe = _pipe(tmp_path, monkeypatch)
    job = _job(_titles(*DISC))
    seen = _wire(pipe, monkeypatch, copy_titles=_titles(*DISC))
    monkeypatch.setattr("blurayripper.pipeline.os_disc_capacity_bytes",
                        lambda dev: 60_000_000_000)
    monkeypatch.setattr(_shutil, "disk_usage",
                        lambda p: _shutil._ntuple_diskusage(0, 0, 10_000_000_000))
    r = _Report()

    await pipe._prepare_disc_once(job, tmp_path / "job_1", r)

    assert getattr(job, "_rip_source", None) is None
    assert "Not enough room" in r.text
    assert seen["backups"] == []


async def test_a_single_title_disc_is_never_copied_first(tmp_path, monkeypatch):
    """One title opens the disc once either way, and a copy would additionally
    read every title that was NOT wanted — on job 385 that is a 7.9 GB 'play
    all' nobody asked for."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job(_titles((0, 6500, None)))
    seen = _wire(pipe, monkeypatch, copy_titles=_titles((0, 6500, None)))

    await pipe._prepare_disc_once(job, tmp_path / "job_1", _Report())

    assert seen["backups"] == []
    assert getattr(job, "_rip_source", None) is None


async def test_a_file_job_and_a_disabled_setting_both_skip_the_copy(
        tmp_path, monkeypatch):
    """A job added from an .mkv has no disc to read, and the feature is opt-in."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job(_titles(*DISC))
    job.source = "/mnt/share/Some Movie.mkv"
    seen = _wire(pipe, monkeypatch, copy_titles=_titles(*DISC))
    await pipe._prepare_disc_once(job, tmp_path / "job_1", _Report())
    assert seen["backups"] == []

    off = _pipe(tmp_path, monkeypatch, backup_first=False)
    job2 = _job(_titles(*DISC))
    seen2 = _wire(off, monkeypatch, copy_titles=_titles(*DISC))
    await off._prepare_disc_once(job2, tmp_path / "job_2", _Report())
    assert seen2["backups"] == []


async def test_the_copy_survives_until_the_last_title_is_out_of_it(
        tmp_path, monkeypatch):
    """Kept while anything still needs it, so a job that dies half way resumes
    from the copy instead of asking for the disc back. Removed the moment it is
    spent — it is the size of a disc, sitting on a network share."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job(_titles(*DISC))
    _wire(pipe, monkeypatch, copy_titles=_titles(*DISC))
    staging = tmp_path / "job_1"
    r = _Report()

    await pipe._prepare_disc_once(job, staging, r)
    await pipe._ensure_ripped(job, 0, staging, r)
    pipe._release_disc_copy(job, staging, r)
    assert (staging / "disc").exists(), "titles 1 and 2 still need it"

    for tid in (1, 2):
        await pipe._ensure_ripped(job, tid, staging, r)
    pipe._release_disc_copy(job, staging, r)
    assert not (staging / "disc").exists()
    assert "Disc copy removed" in r.text
