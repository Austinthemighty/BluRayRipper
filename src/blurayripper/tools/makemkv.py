"""MakeMKV (makemkvcon) wrapper: robot-mode scan + rip.

makemkvcon's ``-r`` (robot) mode emits stable, parseable, comma-separated,
quoted lines. We parse:

  DRV:index,visible,enabled,flags,driveName,discName
  TCOUNT:count
  CINFO:id,code,value                 (disc-level attributes)
  TINFO:titleId,id,code,value         (per-title attributes)
  SINFO:titleId,streamId,id,code,value(per-stream attributes)
  MSG:code,flags,count,message,...    (human log)
  PRGV:current,total,max              (progress bars)
  PRGC/PRGT:code,id,name              (current/total operation labels)

Attribute IDs (AP_ItemAttributeId from MakeMKV's apdefs.h) we rely on:
  1=Type 2=Name 3=LangCode 5=CodecId 6=CodecShort
  8=ChapterCount 9=Duration 11=DiskSizeBytes 16=SourceFileName
  27=OutputFileName 32=VolumeName
"""

from __future__ import annotations

import asyncio
import csv
import io
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..models import DiscInfo, Stream, StreamType, Title
from . import process


@dataclass
class Drive:
    index: int           # makemkvcon disc:N selector index (-1 = MakeMKV-blind)
    name: str            # drive model
    device: str | None   # OS device, e.g. "F:" or "/dev/sr0"
    disc_name: str | None  # loaded disc label, if any
    os_detected: bool = False  # disc name came from the OS, not makemkvcon's probe
    makemkv_blind: bool = False  # OS sees a disc here but makemkvcon can't open the drive


# --- Linux optical probing ---------------------------------------------------
#
# The three os_* helpers below answer "what does the OPERATING SYSTEM think is
# in this drive", independently of makemkvcon. Every one of them was Windows
# only, so on a Linux rip box makemkvcon's cached quick probe was the single
# source of truth about a disc: when it reported nothing, the panel showed
# nothing, with no recovery and no way to tell an empty drive from a drive that
# had dropped off the bus. That is the half of "the newer drive doesn't find
# discs" that is ours rather than the drive's.

# CDROM_DRIVE_STATUS from <linux/cdrom.h>: the kernel's own media answer, and
# the only one that needs neither root, nor a mount, nor a read of the disc.
_CDROM_DRIVE_STATUS = 0x5326
_CDS_DISC_OK = 4


def _linux_optical_devices() -> list[str]:
    """Every /dev/srN the kernel has created, in index order."""
    import glob as _glob
    import re as _re

    def key(dev: str) -> int:
        m = _re.search(r"(\d+)$", dev)
        return int(m.group(1)) if m else 0

    return sorted(_glob.glob("/dev/sr[0-9]*"), key=key)


def _linux_media_present(device: str) -> bool:
    """Whether ``device`` holds readable media, via CDROM_DRIVE_STATUS.

    O_NONBLOCK is what makes this safe to poll: opening an optical device
    normally waits for the disc to spin up and settle, which on a drive that has
    stopped answering is exactly the unbounded wait we must not take."""
    import fcntl
    import os as _os

    try:
        fd = _os.open(device, _os.O_RDONLY | _os.O_NONBLOCK)
    except OSError:
        return False
    try:
        return fcntl.ioctl(fd, _CDROM_DRIVE_STATUS, 0) == _CDS_DISC_OK
    except OSError:
        return False
    finally:
        try:
            _os.close(fd)
        except OSError:
            pass


def _linux_disc_label(device: str) -> str | None:
    """The volume label udev read when the disc was inserted, or None.

    Asked of udev rather than parsed off the disc: commercial Blu-rays are UDF,
    DVDs are ISO 9660, and udev's probe already knows both. Cold path only (a
    full enumeration), never the media poll."""
    import subprocess

    try:
        out = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={device}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "ID_FS_LABEL" and value.strip():
            return value.strip()
    return None


def os_disc_label(device: str | None) -> str | None:
    """The OS's volume label for an optical drive letter (Windows only), or None.

    makemkvcon's quick drive enumeration sometimes reports NO disc for a disc the
    OS has mounted (so it's visible in File Explorer, or `lsblk` on Linux shows
    its label). Reading the OS's own volume label recovers the disc name in that
    case. Best-effort; never raises."""
    if not device:
        return None
    if not sys.platform.startswith("win"):
        if sys.platform.startswith("linux") and _linux_media_present(device):
            return _linux_disc_label(device)
        return None
    letter = device.strip().rstrip("\\/").rstrip(":")
    if len(letter) != 1 or not letter.isalpha():
        return None
    try:
        import ctypes

        k = ctypes.windll.kernel32
        prev = k.SetErrorMode(0x0001)  # SEM_FAILCRITICALERRORS: suppress "no disk" popup
        try:
            buf = ctypes.create_unicode_buffer(261)
            ok = k.GetVolumeInformationW(
                ctypes.c_wchar_p(f"{letter}:\\"), buf, 260, None, None, None, None, 0)
        finally:
            k.SetErrorMode(prev)
        if not ok:
            return None
        return buf.value.strip() or None
    except Exception:
        return None


def os_optical_drives() -> list[tuple[str, str | None]]:
    """(drive_letter, volume_label) for each Windows optical drive that has media.

    On Linux the pair is (device, label) — "/dev/sr1" rather than "F:" — which is
    the same form ``Drive.device`` takes there, so the caller's de-duplication
    against the drives makemkvcon did enumerate lines up on both platforms.

    Lets the app show a disc the OS has mounted (visible in File Explorer) even
    when makemkvcon can't enumerate the drive at all — a wedged/half-offline
    drive returns "no usable optical drives". Empty on an unsupported OS or on
    error."""
    if not sys.platform.startswith("win"):
        if not sys.platform.startswith("linux"):
            return []
        try:
            return [(dev, _linux_disc_label(dev))
                    for dev in _linux_optical_devices()
                    if _linux_media_present(dev)]
        except Exception:
            return []
    try:
        import ctypes

        k = ctypes.windll.kernel32
        DRIVE_CDROM = 5
        prev = k.SetErrorMode(0x0001)  # suppress "no disk" popups while probing
        try:
            bitmask = k.GetLogicalDrives()
            out: list[tuple[str, str | None]] = []
            for i in range(26):
                if not (bitmask >> i) & 1:
                    continue
                letter = f"{chr(ord('A') + i)}:"
                if k.GetDriveTypeW(ctypes.c_wchar_p(f"{letter}\\")) != DRIVE_CDROM:
                    continue
                label = os_disc_label(letter)  # None if the drive is empty
                if label:
                    out.append((letter, label))
            return out
        finally:
            k.SetErrorMode(prev)
    except Exception:
        return []


def os_media_snapshot() -> dict[str, str] | None:
    """``{drive_letter: volume_label}`` for optical drives that currently hold
    readable media, or None where the OS can't be probed cheaply (non-Windows).

    Cheap enough to poll every couple of seconds, and — unlike makemkvcon — it
    keeps answering while a rip has a drive busy. Used to spot a tray closing on a
    new disc right away instead of waiting for the next full enumeration. A None
    return means "can't tell", so callers must keep doing their regular poll.

    On Linux the value is a placeholder, not the label: this is polled, and only
    the KEYS have to change when a tray does. Reading a label costs a subprocess
    per drive, which belongs on the enumeration path (os_disc_label), not here.
    """
    if sys.platform.startswith("win"):
        return dict(os_optical_drives())
    if not sys.platform.startswith("linux"):
        return None
    try:
        return {dev: "disc" for dev in _linux_optical_devices()
                if _linux_media_present(dev)}
    except Exception:
        return None


def parse_drives(lines: list[str]) -> list[Drive]:
    """Parse DRV lines from `makemkvcon -r info disc:<big>` into present drives.

    Deduplicated by index. One makemkvcon run can emit the DRV block more than
    once — it re-enumerates after LibreDrive/driver init, which is also why
    "MakeMKV ... started" appears twice in a scan log — and every one of those
    lines is the same physical drive. Appending them blindly showed each drive
    twice in the panel, each with its own Add button queueing the same disc.

    A later line wins, being the more initialised enumeration, except that it
    must not blank a disc name or device the earlier one already had.
    """
    by_index: dict[int, Drive] = {}
    for line in lines:
        prefix, _, payload = line.partition(":")
        if prefix != "DRV":
            continue
        fields = _split_robot_line(payload)
        if len(fields) < 6 or not fields[4]:  # require a non-empty drive name
            continue
        index = _to_id(fields[0])
        if index is None:
            continue  # junk index: it would take over (and rename) drive 0
        drive = Drive(
            index=index,
            name=fields[4],
            device=fields[6] if len(fields) >= 7 and fields[6] else None,
            disc_name=fields[5] or None,
        )
        prior = by_index.get(drive.index)
        if prior is not None:
            drive.disc_name = drive.disc_name or prior.disc_name
            drive.device = drive.device or prior.device
        by_index[drive.index] = drive
    return list(by_index.values())

# Attribute IDs we care about.
ATTR_TYPE = 1
ATTR_NAME = 2
ATTR_LANG_CODE = 3
ATTR_CODEC_ID = 5
ATTR_CODEC_SHORT = 6
ATTR_CHAPTER_COUNT = 8
ATTR_DURATION = 9
ATTR_DISK_SIZE_BYTES = 11
ATTR_SOURCE_FILENAME = 16
ATTR_OUTPUT_FILENAME = 27
ATTR_VOLUME_NAME = 32

# Progress callback: (label, fraction 0..1). Fraction may be None for indeterminate.
ProgressHandler = Callable[[str, float | None], None] | Callable[[str, float | None], Awaitable[None]]


def _split_robot_line(payload: str) -> list[str]:
    """Split the comma-separated, optionally-quoted fields of a robot line."""
    reader = csv.reader(io.StringIO(payload), skipinitialspace=False)
    try:
        return next(reader)
    except StopIteration:
        return []
    except csv.Error:
        # csv refuses a single field longer than 131072 chars. A crashing
        # makemkvcon that dumps a buffer with no newline hits that, and the
        # error would escape the stdout pump and lose the whole scan or rip
        # over one unparseable line.
        return []


def _parse_duration(value: str) -> int:
    """'1:23:45' or '23:45' -> total seconds."""
    parts = value.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def _stream_type_from_value(value: str) -> StreamType:
    v = value.strip().lower()
    if v.startswith("video"):
        return StreamType.VIDEO
    if v.startswith("audio"):
        return StreamType.AUDIO
    if v.startswith("subtitle"):
        return StreamType.SUBTITLE
    return StreamType.UNKNOWN


def parse_info(lines: list[str], source: str) -> DiscInfo:
    """Pure parser for ``makemkvcon -r info`` output. Unit-test target."""
    disc = DiscInfo(source=source)
    titles: dict[int, Title] = {}
    streams: dict[tuple[int, int], Stream] = {}
    drv_devices: dict[int, str] = {}

    for line in lines:
        prefix, _, payload = line.partition(":")
        if not _:
            continue
        fields = _split_robot_line(payload)

        if prefix == "CINFO" and len(fields) >= 3:
            attr_id = _to_int(fields[0])
            value = fields[2]
            if attr_id == ATTR_NAME:
                # CINFO Name is the authoritative friendly title; it overrides
                # the coarser DRV disc label parsed earlier.
                disc.disc_name = value
            elif attr_id == ATTR_VOLUME_NAME:
                disc.volume_label = value

        elif prefix == "DRV" and len(fields) >= 6:
            drv_index = _to_id(fields[0])
            disc_name = fields[5]
            if disc_name and not disc.disc_name:
                disc.disc_name = disc_name
            if drv_index is not None and len(fields) >= 7 and fields[6]:
                drv_devices[drv_index] = fields[6]

        elif prefix == "TINFO" and len(fields) >= 4:
            title_id = _to_id(fields[0])
            if title_id is None:
                continue  # junk id: it would be filed under (and overwrite) title 0
            attr_id = _to_int(fields[1])
            value = fields[3]
            title = titles.setdefault(title_id, Title(title_id=title_id))
            _apply_title_attr(title, attr_id, value)

        elif prefix == "SINFO" and len(fields) >= 5:
            title_id = _to_id(fields[0])
            stream_id = _to_id(fields[1])
            if title_id is None or stream_id is None:
                continue
            attr_id = _to_int(fields[2])
            value = fields[4]
            stream = streams.setdefault(
                (title_id, stream_id), Stream(stream_id=stream_id, stream_type=StreamType.UNKNOWN)
            )
            _apply_stream_attr(stream, attr_id, value)
            titles.setdefault(title_id, Title(title_id=title_id))

    # Attach streams to their titles, ordered by stream id.
    for (title_id, _stream_id), stream in sorted(streams.items()):
        titles[title_id].streams.append(stream)

    disc.titles = [titles[k] for k in sorted(titles)]

    # Resolve the OS device for the selected drive (for auto-eject).
    if source.startswith(("disc:", "dev:")):
        selector = source.split(":", 1)[1]
        if selector.isdigit():
            disc.drive_device = drv_devices.get(int(selector))
        else:
            disc.drive_device = selector  # dev:/dev/sr0 -> "/dev/sr0"
    return disc


def _apply_title_attr(title: Title, attr_id: int, value: str) -> None:
    if attr_id == ATTR_DURATION:
        title.duration_seconds = _parse_duration(value)
    elif attr_id == ATTR_CHAPTER_COUNT:
        title.chapter_count = _to_int(value)
    elif attr_id == ATTR_DISK_SIZE_BYTES:
        title.size_bytes = _to_int(value)
    elif attr_id == ATTR_SOURCE_FILENAME:
        title.source_filename = value
    elif attr_id == ATTR_OUTPUT_FILENAME:
        title.output_filename = value
    elif attr_id == ATTR_NAME:
        title.name = value


def _apply_stream_attr(stream: Stream, attr_id: int, value: str) -> None:
    if attr_id == ATTR_TYPE:
        stream.stream_type = _stream_type_from_value(value)
    elif attr_id == ATTR_LANG_CODE:
        stream.language = value.strip().lower() or None
    elif attr_id in (ATTR_CODEC_SHORT, ATTR_CODEC_ID) and not stream.codec:
        stream.codec = value
    elif attr_id == ATTR_NAME:
        stream.name = value


def _to_int(value: str) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return 0


def _to_id(value: str) -> int | None:
    """A robot line's IDENTITY field (title id, stream id, drive index), or None.

    Deliberately not _to_int. Answering 0 for junk is harmless for a VALUE, but
    for an id it silently files a garbage line under entry 0: a truncated or
    interleaved 'TINFO:junk,9,0,"0:00:07"' overwrote the main feature's duration
    (a 1:47:12 title became 7 seconds and dropped under the min-length cut), and
    a junk DRV line renamed drive 0 or pointed auto-eject at the wrong device.
    Everywhere else a junk field degrades to "unknown"; here it degraded to
    "the main feature".
    """
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


class MakeMKV:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def _bin(self) -> str:
        return self._settings.tools.require("makemkvcon")

    async def list_drives(self, *, timeout: float = 60.0) -> list[Drive]:
        """Enumerate optical drives (and any loaded disc) without opening a disc.

        Bounded by a timeout: while a rip is active makemkvcon can be slow to
        respond, and we don't want the drives endpoint to hang. On timeout the
        subprocess is killed and a TimeoutError is raised so the UI keeps its
        last-known list and retries on the next refresh.
        """
        cmd = [self._bin, "-r", "--cache=1", "info", "disc:99999"]
        result = await asyncio.wait_for(process.run(cmd, check=False), timeout=timeout)
        drives = parse_drives(result.stdout_lines)
        # Recover a disc the OS has mounted but makemkvcon's quick probe missed
        # (shows in File Explorer but came back with an empty disc name).
        for d in drives:
            if not d.disc_name and d.device:
                label = os_disc_label(d.device)
                if label:
                    d.disc_name = label
                    d.os_detected = True
        # Surface a disc the OS has but makemkvcon couldn't enumerate at all (the
        # drive dropped off the bus / "no usable optical drives"). Not rippable
        # until the drive is reset, so it's flagged makemkv_blind (index -1).
        seen = {(d.device or "").rstrip("\\/").upper() for d in drives}
        for letter, label in os_optical_drives():
            if letter.rstrip("\\/").upper() in seen:
                continue
            drives.append(Drive(index=-1, name="(optical drive)", device=letter,
                                 disc_name=label, os_detected=True, makemkv_blind=True))
        return drives

    async def scan(
        self, source: str, *, on_message: Callable[[str], None] | None = None
    ) -> DiscInfo:
        """Scan a disc/folder/ISO. ``source`` is a makemkvcon selector:
        e.g. 'disc:0', 'dev:/dev/sr0', 'file:/path/to/BDMV_parent', 'iso:/path'.

        MakeMKV's MSG lines (registration state, disc-open errors, AACS/UHD
        failures) are forwarded to ``on_message`` so the caller can surface the
        real reason a scan returns no titles.
        """
        cmd = [
            self._bin,
            "-r",
            "--cache=1",
            f"--minlength={self._settings.min_length_seconds}",
            "info",
            source,
        ]

        def handle(line: str) -> None:
            if on_message is not None and line.startswith("MSG:"):
                fields = _split_robot_line(line[4:])
                if len(fields) >= 4:
                    on_message(fields[3])

        result = await process.run(cmd, on_line=handle)
        return parse_info(result.stdout_lines, source)

    async def rip_title(
        self,
        source: str,
        title_id: int,
        dest_dir: Path,
        *,
        on_progress: ProgressHandler | None = None,
        on_message: Callable[[str], None] | None = None,
    ) -> list[Path]:
        """Rip a single title to ``dest_dir``; returns the produced .mkv paths."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        before = set(dest_dir.glob("*.mkv"))
        cmd = [
            self._bin,
            "-r",
            "--progress=-same",
            f"--minlength={self._settings.min_length_seconds}",
            "mkv",
            source,
            str(title_id),
            str(dest_dir),
        ]
        current_label = {"text": ""}

        async def handle(line: str) -> None:
            prefix, _, payload = line.partition(":")
            if prefix in ("PRGC", "PRGT"):
                fields = _split_robot_line(payload)
                if len(fields) >= 3:
                    current_label["text"] = fields[2]
            elif prefix == "PRGV":
                fields = _split_robot_line(payload)
                if len(fields) >= 3 and on_progress is not None:
                    total = _to_int(fields[1])
                    maximum = _to_int(fields[2])
                    frac = (total / maximum) if maximum else None
                    res = on_progress(current_label["text"], frac)
                    if res is not None and hasattr(res, "__await__"):
                        await res
            elif prefix == "MSG":
                fields = _split_robot_line(payload)
                if len(fields) >= 4 and on_message is not None:
                    on_message(fields[3])

        await process.run(cmd, on_line=handle)
        after = set(dest_dir.glob("*.mkv"))
        return sorted(after - before)
