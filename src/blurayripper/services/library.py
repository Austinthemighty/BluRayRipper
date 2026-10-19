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


def publish(staged_video: Path, final_video: Path) -> list[Path]:
    """Move a title's finished set out of staging and into the library.

    Everything a title contributes — the video, its subtitle sidecars, its .nfo,
    and the folder artwork — is assembled under staging first, so the library
    never shows a half-built item. Writing them as they were produced meant the
    movie folder appeared with a poster and subtitles the moment naming was
    confirmed and gained its video hours later, when the encode finished: long
    enough to copy the folder, or for Plex to scan it, and find no film in it.

    Two ways in, picked by whether the destination folder already exists:

    * New folder (a movie, or the first episode of a season): the staged folder
      is RENAMED into place. One filesystem operation, so the item appears whole
      or not at all.
    * Existing folder (later episodes): files are moved individually and the
      VIDEO GOES LAST, because the video is what makes a scanner treat the item
      as present — its sidecars are already there when it lands.

    Artwork sitting above the video's folder (a show's poster, beside Season NN)
    is published too; it belongs to the show rather than this title, and later
    titles simply find it already gone from staging.

    Returns the paths actually written, newest layout first.
    """
    staged_video, final_video = Path(staged_video), Path(final_video)
    written: list[Path] = []
    src_dir, dst_dir = staged_video.parent, final_video.parent
    if not src_dir.exists():
        return written

    if not dst_dir.exists():
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_dir), str(dst_dir))
        written.extend(sorted(dst_dir.iterdir()))
    else:
        for item in sorted(src_dir.iterdir()):
            if item.name == staged_video.name or item.is_dir():
                continue                      # the video lands last
            written.append(place(item, dst_dir / item.name))
        if staged_video.exists():
            written.append(place(staged_video, final_video))

    # Show-level artwork, which sits above the season folder.
    parent = src_dir.parent
    final_parent = dst_dir.parent
    if parent.exists() and parent.name and final_parent != dst_dir:
        for item in sorted(parent.iterdir()):
            if item.is_dir():
                continue
            written.append(place(item, final_parent / item.name))
    return written


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
    # Matched by a plain prefix test, NOT a glob: the stem is a movie title, and
    # glob reads "[", "]", "?" and "*" as wildcards. "Movie [Extended] (2020)"
    # became a character class that matches nothing, so the video and every
    # sidecar stayed behind while the fixed-name artwork below moved — the film
    # ended up split across two folders and the rename reported success.
    #
    # The is_dir() guard keeps glob's old answer for a folder that isn't there:
    # iterdir() would raise, and re-running a rename that already happened has to
    # stay a no-op.
    old_entries = sorted(old_dir.iterdir()) if old_dir.is_dir() else []
    for f in [p for p in old_entries if p.name.startswith(old_stem)]:
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
