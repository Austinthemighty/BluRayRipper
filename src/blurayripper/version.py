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

import re

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from . import __version__ as _BASE

_REPO = Path(__file__).resolve().parents[2]  # repo root (…/src/blurayripper -> …)

def _git_candidates() -> list[Path]:
    """Common Git-for-Windows install locations, probed when git isn't on PATH
    (the `serve` shell often lacks it, esp. a no-admin per-user install)."""
    cands = [
        Path(r"C:\Program Files\Git\cmd\git.exe"),
        Path(r"C:\Program Files (x86)\Git\cmd\git.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "Git" / "cmd" / "git.exe",
    ]
    # GitHub Desktop bundles git but never adds it to PATH; its app dir is
    # versioned (app-X.Y.Z), so glob and prefer the newest.
    gh = Path.home() / "AppData" / "Local" / "GitHubDesktop"
    try:
        cands += sorted(gh.glob("app-*/resources/app/git/cmd/git.exe"), reverse=True)
    except OSError:
        pass
    return cands


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


def _sha_from_git_dir() -> str | None:
    """Read the current commit's short SHA straight from .git, no git binary
    needed — so the version still identifies the build on a box without git on
    PATH (which otherwise pins everyone to a useless bare '0.1.0')."""
    git_dir = _REPO / ".git"
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head[:7]  # detached HEAD holds the SHA directly
    ref = head[4:].strip()
    try:
        return (git_dir / ref).read_text().strip()[:7]  # loose ref
    except OSError:
        pass
    try:  # packed-refs fallback
        for line in (git_dir / "packed-refs").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0][:7]
    except OSError:
        pass
    return None


def commit_count(version: str | None) -> int | None:
    """The commit number out of a version like ``0.1.0+242.bc2d392``, or None.

    That count is monotonic, so it's the one part of the string that can say which
    of two boxes is NEWER. None when git wasn't available to build the version
    (``0.1.0+gabc1234``), in which case callers must not guess."""
    m = re.match(r"^\d+\.\d+\.\d+\+(\d+)\.", version or "")
    return int(m.group(1)) if m else None


def commit_of(version: str | None) -> str | None:
    """The commit hash out of a version like ``0.1.0+242.bc2d392`` or
    ``0.1.0+gbc2d392``, or None."""
    m = re.match(r"^\d+\.\d+\.\d+\+(?:\d+\.|g)([0-9a-f]{7,40})", version or "")
    return m.group(1) if m else None


def _git_in(repo: Path, *args: str) -> subprocess.CompletedProcess | None:
    exe = _git_exe()
    if not exe:
        return None
    try:
        return subprocess.run([exe, "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None


def code_relation(version: str | None, head_version: str | None = None,
                  repo: Path = _REPO) -> tuple[str, int | None]:
    """Where another box's build sits against this checkout.

    One of ("current", 0), ("behind", n), ("ahead", n), ("off_main", None) or
    ("unknown", None). "off_main" is the one that matters: a commit that is not
    in this checkout's history, so a fast-forward on that box can never reach
    main. After main was rewritten, handbreak01 and handbreak02 sat on one for
    six weeks while the workers panel showed them as healthy.
    """
    theirs = commit_of(version)
    ours = commit_of(head_version if head_version is not None else get_version())
    if not theirs or not ours:
        return ("unknown", None)
    return _relation(theirs, ours, str(repo))


@lru_cache(maxsize=128)
def _relation(theirs: str, ours: str, repo: str) -> tuple[str, int | None]:
    def full(rev: str) -> str | None:
        r = _git_in(Path(repo), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        return r.stdout.strip() if r is not None and r.returncode == 0 else None

    def ancestor(a: str, b: str) -> bool:
        r = _git_in(Path(repo), "merge-base", "--is-ancestor", a, b)
        return r is not None and r.returncode == 0

    def count(a: str, b: str) -> int | None:
        r = _git_in(Path(repo), "rev-list", "--count", f"{a}..{b}")
        out = r.stdout.strip() if r is not None and r.returncode == 0 else ""
        return int(out) if out.isdigit() else None

    head, other = full(ours), full(theirs)
    if head is None:
        return ("unknown", None)
    if other is None:
        return ("off_main", None)      # a commit this history has never had
    if other == head:
        return ("current", 0)
    if ancestor(other, head):
        return ("behind", count(other, head))
    if ancestor(head, other):
        return ("ahead", count(head, other))
    return ("off_main", None)          # diverged: same story, a rewritten main


@lru_cache(maxsize=1)
def get_version() -> str:
    count = _git("rev-list", "--count", "HEAD")
    short = _git("rev-parse", "--short", "HEAD")
    if count and short:
        dirty = bool(_git("status", "--porcelain"))
        return f"{_BASE}+{count}.{short}{'-dirty' if dirty else ''}"
    # git binary unavailable — still surface the commit SHA from .git directly.
    sha = _sha_from_git_dir()
    return f"{_BASE}+g{sha}" if sha else _BASE
