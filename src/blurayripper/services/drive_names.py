"""Persistent user-assigned names for optical drives.

Lets the UI show a friendly label ("Top drive") instead of the model/serial.

Keyed on the drive's model+serial, NOT on its device path. The path is assigned
by discovery order: power-cycling one drive renumbered both on a live box, so
labels keyed on the path stayed with the path and swapped drives. The panel then
offered an eject button labelled "Silver Drive" that opened the black one —
which, on a machine that is ripping, means ejecting a disc mid-rip.

Entries written under an older device-keyed version are still read, so existing
labels survive; they move to the stable key the next time they are set.
"""

from __future__ import annotations

import json
from pathlib import Path


class DriveNames:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._names: dict[str, str] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                data = None
            # A file that parses but isn't an object ("null", "[]", a hand-edit
            # gone wrong) used to be stored as-is, and the AttributeError only
            # surfaced later at .get() — taking out the drives panel rather than
            # falling back to no custom names, which is what the guard is for.
            self._names = data if isinstance(data, dict) else {}

    def get(self, key: str | None, legacy: str | None = None) -> str | None:
        """The label for a drive. ``key`` is its model+serial; ``legacy`` is the
        device path, read only so labels saved by an older version survive."""
        name = self._names.get((key or "").strip())
        if name is None and legacy:
            name = self._names.get(legacy.strip())
        return name

    def set(self, key: str, name: str | None, legacy: str | None = None) -> None:
        """Save under the stable key, and drop any device-keyed entry it
        replaces — leaving that behind would re-attach the old label to whatever
        drive next lands on that path."""
        key = (key or "").strip()
        if not key:
            return
        if name and name.strip():
            self._names[key] = name.strip()
        else:
            self._names.pop(key, None)
        if legacy and legacy.strip() != key:
            self._names.pop(legacy.strip(), None)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._names, indent=2), encoding="utf-8")
