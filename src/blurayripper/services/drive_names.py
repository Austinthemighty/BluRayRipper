"""Persistent user-assigned names for optical drives, keyed by device (e.g. 'F:').

Lets the UI show a friendly label ("Top drive") instead of the model/serial, and
keeps the drive identifiable by its letter before a disc has mounted.
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

    def get(self, device: str | None) -> str | None:
        return self._names.get(device or "")

    def set(self, device: str, name: str | None) -> None:
        if name and name.strip():
            self._names[device] = name.strip()
        else:
            self._names.pop(device, None)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._names, indent=2), encoding="utf-8")
