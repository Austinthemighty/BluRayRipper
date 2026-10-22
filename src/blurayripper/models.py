"""Shared dataclasses describing discs, titles, streams, and the pipeline state.

These are plain data carriers used across the tool wrappers, services, and
pipeline. Keeping them in one place avoids circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class StreamType(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    UNKNOWN = "unknown"


@dataclass
class Stream:
    stream_id: int
    stream_type: StreamType
    codec: str | None = None
    language: str | None = None  # ISO 639-2/B 3-letter where available
    name: str | None = None


@dataclass
class Title:
    """A single rippable title as reported by MakeMKV."""

    title_id: int
    duration_seconds: int = 0
    chapter_count: int = 0
    size_bytes: int = 0
    source_filename: str | None = None  # e.g. "00800.mpls"
    output_filename: str | None = None  # MakeMKV's default .mkv name
    name: str | None = None
    streams: list[Stream] = field(default_factory=list)

    @property
    def audio_languages(self) -> list[str]:
        return [s.language for s in self.streams
                if s.stream_type is StreamType.AUDIO and s.language]

    @property
    def subtitle_languages(self) -> list[str]:
        return [s.language for s in self.streams
                if s.stream_type is StreamType.SUBTITLE and s.language]

    @property
    def duration_hhmmss(self) -> str:
        h, rem = divmod(self.duration_seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:d}:{m:02d}:{s:02d}"


# The prefixes makemkvcon understands as a source. A job whose source has none
# of them is a plain filesystem path — a job built from an existing .mkv, which
# never runs MakeMKV. Handing such a path to makemkvcon only ever errors.
SOURCE_SELECTORS = ("disc:", "dev:", "file:", "iso:")


@dataclass
class DiscInfo:
    """Result of scanning a disc/folder/ISO."""

    source: str  # makemkvcon source selector, e.g. "disc:0", "file:/path", "iso:/path"
    disc_name: str | None = None
    volume_label: str | None = None
    drive_device: str | None = None  # OS device for eject, e.g. "F:" or "/dev/sr0"
    # The drive this scan actually used, as model+serial ("BD-RE ASUS BW-16D1HT
    # 3.10 KX1P71E4206"). The stable identity of a physical drive: disc:N and
    # /dev/srN both get reassigned across reboots, the serial never does.
    drive_name: str | None = None
    # Whether MakeMKV hit a SCSI/hardware read error on this scan. Unlike the
    # "Using LibreDrive mode" announcement — which is emitted for whichever
    # capable drive has a disc, not necessarily the one being opened, so it
    # cannot be attributed — this is about the drive that was actually read.
    read_error: bool = False
    # MakeMKV's own word for the medium ("Blu-ray disc", "DVD disc"). Decides
    # whether the disc can be copied whole first: MakeMKV backs up Blu-rays only.
    disc_type: str | None = None
    titles: list[Title] = field(default_factory=list)

    def title(self, title_id: int) -> Title | None:
        for t in self.titles:
            if t.title_id == title_id:
                return t
        return None


class MediaKind(str, Enum):
    MOVIE = "movie"
    TV = "tv"


@dataclass
class EpisodeAssignment:
    """User/auto mapping of one ripped title to its identity for naming."""

    title_id: int
    kind: MediaKind
    show_or_movie_title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    edition: str | None = None  # movies: Plex {edition-...} tag, e.g. "Extended Cut"
    # movies: Plex multi-version suffix, e.g. "4K" -> "Jumanji (1995) - 4K.mkv".
    # Stored on the assignment rather than derived at placement time because
    # _video_path_for feeds the resume check, the subtitle sidecars and the
    # poster folder — a path that changed with the folder's contents would make
    # a placed job look unplaced and strand its sidecars beside a name that no
    # longer exists.
    version: str | None = None
    # The last episode when one title holds more than one: a two-part episode
    # mastered as a single 43-minute title (HIMYM S8 "The Final Page", E11+E12).
    # None for the ordinary one-episode title.
    episode_end: int | None = None

    @property
    def episodes(self) -> list[int]:
        """Every episode this title holds, in order."""
        if self.episode is None:
            return []
        end = self.episode_end if (self.episode_end or 0) > self.episode else self.episode
        return list(range(self.episode, end + 1))


@dataclass
class RippedFile:
    title_id: int
    path: Path
