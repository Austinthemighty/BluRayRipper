"""Three ways the app could destroy or disturb something it did not own.

Unlike a job that fails, these reach outside the job: an existing library file,
another box's claim, or the disc physically sitting in a drive. A failure that
only loses work is recoverable; these are not.
"""

import asyncio
import shutil

import pytest

from blurayripper.persistence import MemoryStore
from blurayripper.services import library


# --- 1. never delete the destination before the replacement is in place ------

def test_a_failed_replace_leaves_the_existing_file_intact(tmp_path, monkeypatch):
    """place() unlinked the destination and then moved. If the share dropped
    between the two, the good file was gone and nothing replaced it — reachable
    on any re-OCR that re-places sidecars over existing ones."""
    dest = tmp_path / "lib" / "Film (1999).en.srt"
    dest.parent.mkdir(parents=True)
    dest.write_text("the good subtitles")
    src = tmp_path / "new.srt"
    src.write_text("the replacement")

    real_move = shutil.move
    calls = {"n": 0}

    def flaky_move(a, b):
        calls["n"] += 1
        if calls["n"] == 1:              # the move into place fails
            raise OSError("share went away")
        return real_move(a, b)

    monkeypatch.setattr(library.shutil, "move", flaky_move)

    with pytest.raises(OSError):
        library.place(src, dest)

    assert dest.exists()
    assert dest.read_text() == "the good subtitles"   # not destroyed


def test_a_successful_replace_still_overwrites(tmp_path):
    """The guard must not stop the normal case: replacing is the point."""
    dest = tmp_path / "lib" / "Film (1999).mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old")
    src = tmp_path / "new.mkv"
    src.write_bytes(b"new")

    library.place(src, dest)

    assert dest.read_bytes() == b"new"
    assert not src.exists()
    assert not list(dest.parent.glob("*.partial-replace"))   # no litter


def test_refusing_to_overwrite_still_refuses(tmp_path):
    dest = tmp_path / "Film.mkv"
    dest.write_bytes(b"old")
    src = tmp_path / "new.mkv"
    src.write_bytes(b"new")

    with pytest.raises(FileExistsError):
        library.place(src, dest, overwrite=False)
    assert dest.read_bytes() == b"old"


# --- 2. a box may only release its own claim ---------------------------------

def test_a_box_cannot_release_a_claim_that_has_moved_to_another_box():
    """release() was unscoped, so a late release from a box that had already
    lost the job dropped the NEW owner's claim — and the job was then picked up
    a second time, two encodes writing the same output path."""
    store = MemoryStore()
    store.save(_rec(7))

    first = store.claim_next("box-A", ["ready"])
    assert first is not None
    store.release(7, "box-A")                    # A gives it up
    second = store.claim_next("box-B", ["ready"])
    assert second is not None                    # B now owns it

    store.release(7, "box-A")                    # A's late release
    assert store.claim_next("box-C", ["ready"]) is None   # B still owns it


def test_an_unscoped_release_still_works_for_the_operator():
    """serve reclaims a dead box's job on the operator's behalf and legitimately
    does not know the owner, so the unscoped form must stay available."""
    store = MemoryStore()
    store.save(_rec(8))
    store.claim_next("box-A", ["ready"])

    store.release(8)                             # no owner given
    assert store.claim_next("box-B", ["ready"]) is not None


# --- 3. a retry must not eject a disc it did not rip -------------------------

async def test_a_resume_that_reuses_its_rip_does_not_eject_the_drive(tmp_path, monkeypatch):
    """_ripped_this_run lived on the Job, and retry() reuses that Job — so a
    resume that reused a checkpointed rip still ran the "finished ripping" block.
    That re-ejects, and the eject target is resolved against the drives as they
    are NOW: hours later that is whatever disc is loaded, possibly another job's,
    mid-rip. It also pushed a false "safe to swap the next one"."""
    from blurayripper.config import load_settings
    from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
    from blurayripper.pipeline import Job, Pipeline, TitleResult

    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path / "out")
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "stage")
    s.auto_eject = True
    p = Pipeline(s)

    ripped = tmp_path / "stage" / "job_1" / "rip" / "t000" / "t00.mkv"
    ripped.parent.mkdir(parents=True)
    ripped.write_bytes(b"a rip from the FIRST run")

    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="X", titles=[
        Title(title_id=0, duration_seconds=100, size_bytes=1,
              source_filename="t00.mkv", streams=[])])
    job.selected_title_ids = [0]
    job.assignments = {0: EpisodeAssignment(
        title_id=0, kind=MediaKind.MOVIE, show_or_movie_title="Film", year=1999)}
    job.checkpoints = {0: {"ripped_path": "job_1/rip/t000/t00.mkv"}}
    job._ripped_this_run = True          # left over from the run that DID rip

    ejected = []
    monkeypatch.setattr("blurayripper.pipeline.eject",
                        lambda dev: ejected.append(dev))

    async def one(job_, tid, a, root, sched, rep):
        return TitleResult(title_id=tid, video_path=tmp_path / "v.mkv")

    monkeypatch.setattr(p, "_process_one", one)
    monkeypatch.setattr(p, "_resolve_source_drive", _anoop)
    monkeypatch.setattr(p, "_place_cover_art", _anoop)
    monkeypatch.setattr(p, "_place_tmdb_art", _anoop)

    await p._process_titles(job, tmp_path / "stage" / "job_1", _Sched(), _Rep())

    assert ejected == []                 # nothing was ripped THIS run
    assert job._ripped_this_run is False


def _rec(job_id):
    from blurayripper.persistence import JobRecord
    return JobRecord(id=job_id, source="disc:0", state="ready",
                     selected_title_ids=[0], outputs=[])


class _Sched:
    async def acquire(self, weight): return weight
    async def release(self, acquired): pass


class _Rep:
    def __init__(self): self.events = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a, **k): pass
    def checkpoint(self): pass
    def notify(self, *a, **k): pass
    def ejected(self, *a, **k): pass


async def _anoop(*a, **k):
    return None
