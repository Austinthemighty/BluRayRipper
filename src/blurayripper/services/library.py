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
    if dest.exists():
        if not overwrite:
            raise FileExistsError(dest)
        dest.unlink()
    shutil.move(str(src), str(dest))
    return dest


def cleanup(job_staging_dir: Path) -> None:
    """Remove a job's staging directory and all intermediates within it."""
    if job_staging_dir.exists():
        shutil.rmtree(job_staging_dir, ignore_errors=True)
