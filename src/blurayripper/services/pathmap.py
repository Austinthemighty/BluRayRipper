"""Translate stored absolute paths between machines.

Most stored paths already float: a rip checkpoint under ``staging_dir`` is saved
relative to it, so each box resolves it against its own root. But a path OUTSIDE
the known roots — a job added from ``Z:\\media\\MakeMKV\\To Process\\x.mkv`` —
has no anchor and is stored verbatim. Move the server to another OS and every one
of those breaks.

A prefix map fixes them without touching the database: configure
``Z:\\media = /mnt/TrueNas/media`` and the old paths resolve on the new box.

The original path always wins when it exists, so on a machine where the stored
path is still valid (the Windows worker in a mixed cluster) nothing is rewritten.
That makes a rule safe to leave configured everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


@dataclass(frozen=True)
class Rule:
    old: str   # prefix as written, e.g. "Z:\\media"
    new: str   # replacement, e.g. "/mnt/TrueNas/media"


def _norm(p: str) -> str:
    """Compare paths ignoring separator style and case.

    Windows paths are case-insensitive, and the same location is written with
    either separator depending on which box stored it, so neither can be trusted
    for matching."""
    return (p or "").replace("\\", "/").rstrip("/").casefold()


def parse_rules(entries) -> list[Rule]:
    """Build rules from ``["Z:\\media = /mnt/TrueNas/media", ...]``.

    Split on the FIRST ``=`` only, so a replacement containing one survives.
    Malformed entries are skipped rather than raising — a typo in config must not
    stop the server booting."""
    rules: list[Rule] = []
    for entry in entries or []:
        if not isinstance(entry, str) or "=" not in entry:
            continue
        old, new = entry.split("=", 1)
        old, new = old.strip(), new.strip()
        if old and new:
            rules.append(Rule(old=old, new=new))
    # Longest prefix first, so a specific rule beats a general one.
    return sorted(rules, key=lambda r: len(_norm(r.old)), reverse=True)


def translate(stored: str, rules: list[Rule]) -> str:
    """Rewrite ``stored`` under the first matching rule, or return it unchanged.

    Only whole path COMPONENTS match, so a rule for ``Z:\\ach`` can't rewrite
    ``Z:\\media``."""
    if not stored or not rules:
        return stored
    target = _norm(stored)
    for rule in rules:
        prefix = _norm(rule.old)
        if target == prefix:
            return rule.new
        if target.startswith(prefix + "/"):
            rest = stored.replace("\\", "/")[len(prefix):].lstrip("/")
            new = rule.new.rstrip("/\\")
            sep = "\\" if ("\\" in rule.new and "/" not in rule.new) else "/"
            return f"{new}{sep}{rest}" if sep == "/" else f"{new}\\{rest.replace('/', chr(92))}"
    return stored


def resolve(stored: str, rules: list[Rule]) -> Path:
    """The usable path for ``stored`` on THIS machine.

    The original wins when it exists, so a box where the stored path is still
    valid is never rewritten — that's what lets one rule sit safely in a mixed
    Windows/Linux cluster."""
    if not stored:
        return Path(stored)
    original = Path(stored)
    try:
        if original.exists():
            return original
    except OSError:
        pass  # an unusable path (e.g. a Windows path on POSIX) — fall through
    mapped = translate(stored, rules)
    return Path(mapped) if mapped != stored else original


def looks_foreign(stored: str) -> bool:
    """Whether a stored path was clearly written by a different OS — a drive
    letter or backslashes. Used to explain a failure rather than to resolve it."""
    s = (stored or "").strip()
    if not s:
        return False
    if "\\" in s:
        return True
    return len(s) > 1 and s[1] == ":" and s[0].isalpha()


__all__ = ["Rule", "parse_rules", "translate", "resolve", "looks_foreign",
           "PurePosixPath", "PureWindowsPath"]
