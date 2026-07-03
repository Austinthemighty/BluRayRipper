"""Runtime version string, derived from git so it advances on every commit
without any manual bump step.

Format: ``0.1.0+<count>.<shorthash>`` (e.g. ``0.1.0+142.4b07ff5``), where
<count> is the total commit count on HEAD — a monotonic build number — and
<shorthash> pins the exact commit. A worker or the rip box printing this makes
it obvious at a glance when a machine is running stale code after a git pull.

If git isn't available (e.g. installed from a wheel), falls back to the base
package version.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from . import __version__ as _BASE

_REPO = Path(__file__).resolve().parents[2]  # repo root (…/src/blurayripper -> …)

def _git_candidates() -> list[Path]:
    """Common Git-for-Windows install locations, probed when git isn't on PATH
    (the `serve` shell often lacks it, esp. a no-admin per-user install)."""
    return [
        Path(r"C:\Program Files\Git\cmd\git.exe"),
        Path(r"C:\Program Files (x86)\Git\cmd\git.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "Git" / "cmd" / "git.exe",
    ]


@lru_cache(maxsize=1)
def _git_exe() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    for candidate in _git_candidates():
        if candidate.exists():
            return str(candidate)
    return None


def _git(*args: str) -> str | None:
    exe = _git_exe()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, *args], cwd=_REPO, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


@lru_cache(maxsize=1)
def get_version() -> str:
    count = _git("rev-list", "--count", "HEAD")
    short = _git("rev-parse", "--short", "HEAD")
    if not count or not short:
        return _BASE
    dirty = bool(_git("status", "--porcelain"))
    return f"{_BASE}+{count}.{short}{'-dirty' if dirty else ''}"
