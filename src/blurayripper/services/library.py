"""Move finished files from the staging area into the Plex output library and
clean up per-job staging so drives (local or NAS) don't accumulate intermediates.

``shutil.move`` handles cross-device moves (staging on a local disk, output on a
NAS) by copying then deleting the source.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def place(src: Path, dest: Path, *, overwrite: bool = True) -> Path:
    """Move ``src`` to ``dest``, creating parent dirs. Returns ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not overwrite:
        raise FileExistsError(dest)
    # Move into place via a temp name in the SAME directory, then swap. Deleting
    # the destination first opened a window where a failed move (a share that
    # drops, a full disk) left the old file gone and nothing to replace it —
    # reachable whenever a re-OCR re-places sidecars over good ones.
    staged = dest.with_name(dest.name + ".partial-replace")
    try:
        staged.unlink()
    except OSError:
        pass
    shutil.move(str(src), str(staged))
    try:
        staged.replace(dest)          # atomic within a filesystem
    except OSError:
        # Cross-device or a filesystem without atomic replace: fall back, but
        # only now, when the replacement is already written and local.
        if dest.exists():
            dest.unlink()
        shutil.move(str(staged), str(dest))
    return dest


def cleanup(job_staging_dir: Path) -> None:
    """Remove a job's staging directory and all intermediates within it."""
    if job_staging_dir.exists():
        shutil.rmtree(job_staging_dir, ignore_errors=True)


def rename_movie(old_video: Path, new_video: Path) -> None:
    """Rename an already-placed movie: move the video + its stem-named sidecars
    (.srt, .nfo) and the folder artwork (poster/fanart) from the old Plex movie
    folder to the new one, and remove the old folder if it's left empty."""
    old_video, new_video = Path(old_video), Path(new_video)
    old_dir, new_dir = old_video.parent, new_video.parent
    old_stem, new_stem = old_video.stem, new_video.stem
    new_dir.mkdir(parents=True, exist_ok=True)
    # Video + sidecars share the stem: "Old (Year).mkv", "Old (Year).en.srt", ".nfo".
    for f in sorted(old_dir.glob(old_stem + "*")):
        dest = new_dir / (new_stem + f.name[len(old_stem):])
        place(f, dest)
    # Folder artwork keeps its fixed name; just move it into the new folder.
    for art in ("poster.jpg", "poster.png", "fanart.jpg", "fanart.png", "background.jpg"):
        p = old_dir / art
        if p.exists():
            place(p, new_dir / art)
    if old_dir != new_dir and old_dir.exists():
        try:
            old_dir.rmdir()  # only if now empty
        except OSError:
            pass
