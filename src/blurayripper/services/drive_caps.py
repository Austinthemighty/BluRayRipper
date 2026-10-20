"""What each optical drive has been observed to be able to do.

Keyed by the drive's model+serial string ("BD-RE ASUS BW-16D1HT 3.10
KX1P71E4206"), which is a physical drive's only stable identity: both
makemkvcon's ``disc:N`` index and the kernel's ``/dev/srN`` are assigned by
discovery order and get swapped across reboots. Keying this on either would
eventually attribute one drive's capabilities to the other — which, for "can
this drive read UHD", means confidently sending a UHD disc to the drive that
cannot read it.

Only LibreDrive is tracked today. It is what decides whether a drive can read a
UHD disc at all, it cannot be inferred from the firmware version (LibreDrive
needs *patched* firmware, so a higher version number may well be the drive
without it), and MakeMKV only reports it when a disc is actually opened. So it
has to be remembered from the last time one was.
"""

from __future__ import annotations

import json
from pathlib import Path


class DriveCaps:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._caps: dict[str, dict] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                data = None
            # Same guard as DriveNames: a file that parses but isn't an object
            # ("null", "[]", a hand-edit gone wrong) must fall back to "nothing
            # known", not blow up at the first .get() and take out the panel.
            self._caps = data if isinstance(data, dict) else {}

    def libredrive(self, drive_name: str | None) -> bool | None:
        """True/False if this drive has been seen opening a disc, else None.

        None means "not known yet", and callers must treat it as permission to
        proceed: refusing work because we have never scanned a drive would make
        a fresh install unable to rip anything.
        """
        entry = self._caps.get((drive_name or "").strip())
        if not isinstance(entry, dict):
            return None
        value = entry.get("libredrive")
        return value if isinstance(value, bool) else None

    def note_libredrive(self, drive_name: str | None, value: bool | None) -> None:
        """Record an observation. ``None`` is not an observation and is ignored,
        so a failed open never erases what a successful one taught us."""
        name = (drive_name or "").strip()
        if not name or value is None:
            return
        if self.libredrive(name) is value:
            return  # unchanged — don't rewrite the file on every scan
        self._caps.setdefault(name, {})["libredrive"] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._caps, indent=2, sort_keys=True),
                              encoding="utf-8")

    def uhd_capable_drives(self) -> list[str]:
        """Drive names known to have LibreDrive, so a refusal can name where the
        disc should go instead."""
        return sorted(n for n in self._caps if self.libredrive(n) is True)
