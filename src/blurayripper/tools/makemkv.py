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

import csv
import io
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..config import Settings
from ..models import DiscInfo, Stream, StreamType, Title
from . import process

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
            drv_index = _to_int(fields[0])
            disc_name = fields[5]
            if disc_name and not disc.disc_name:
                disc.disc_name = disc_name
            if len(fields) >= 7 and fields[6]:
                drv_devices[drv_index] = fields[6]

        elif prefix == "TINFO" and len(fields) >= 4:
            title_id = _to_int(fields[0])
            attr_id = _to_int(fields[1])
            value = fields[3]
            title = titles.setdefault(title_id, Title(title_id=title_id))
            _apply_title_attr(title, attr_id, value)

        elif prefix == "SINFO" and len(fields) >= 5:
            title_id = _to_int(fields[0])
            stream_id = _to_int(fields[1])
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


class MakeMKV:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def _bin(self) -> str:
        return self._settings.tools.require("makemkvcon")

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
