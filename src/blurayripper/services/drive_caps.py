"""What each optical drive has been observed to be able to do.

Keyed by the drive's model+serial string ("BD-RE ASUS BW-16D1HT 3.10
KX1P71E4206"), which is a physical drive's only stable identity: both
makemkvcon's ``disc:N`` index and the kernel's ``/dev/srN`` are assigned by
discovery order and get swapped across reboots. Keying this on either would
eventually attribute one drive's capabilities to the other — which, for "can
this drive read UHD", means confidently sending a UHD disc to the drive that
cannot read it.

One thing is tracked: whether this drive has been seen reading a UHD-sized
disc. Deliberately an OUTCOME and not a capability flag, because the capability
cannot be observed reliably: it comes down to LibreDrive, which is not implied
by the firmware version (it needs *patched* firmware, so the higher version
number may well be the drive without it), and MakeMKV's "Using LibreDrive mode"
line is emitted for whichever capable drive happens to hold a disc rather than
for the drive being opened — so attributing it to a drive got it backwards the
first time this was written, marking the drive that cannot read UHD as the one
that can.

A read either worked on that drive or it didn't. That is attributable.
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

    def uhd_ok(self, drive_name: str | None) -> bool | None:
        """Whether this drive has been seen reading a UHD-sized disc, else None.

        None means "never tried", and callers must treat it as permission to
        proceed: refusing work because we have never put a big disc in a drive
        would make a fresh install unable to rip anything.
        """
        entry = self._caps.get((drive_name or "").strip())
        if not isinstance(entry, dict):
            return None
        value = entry.get("uhd_ok")
        return value if isinstance(value, bool) else None

    def note_uhd_result(self, drive_name: str | None, value: bool | None) -> None:
        """Record an outcome. ``None`` is not an outcome and is ignored, so a
        scan that proves nothing never erases what a real one taught us."""
        name = (drive_name or "").strip()
        if not name or value is None:
            return
        if self.uhd_ok(name) is value:
            return  # unchanged — don't rewrite the file on every scan
        self._caps.setdefault(name, {})["uhd_ok"] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._caps, indent=2, sort_keys=True),
                              encoding="utf-8")

    def uhd_capable_drives(self) -> list[str]:
        """Drives seen reading a UHD disc, so a refusal can name where it should
        go instead."""
        return sorted(n for n in self._caps if self.uhd_ok(n) is True)
