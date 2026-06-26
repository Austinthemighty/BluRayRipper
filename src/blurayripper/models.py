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


@dataclass
class DiscInfo:
    """Result of scanning a disc/folder/ISO."""

    source: str  # makemkvcon source selector, e.g. "disc:0", "file:/path", "iso:/path"
    disc_name: str | None = None
    volume_label: str | None = None
    drive_device: str | None = None  # OS device for eject, e.g. "F:" or "/dev/sr0"
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


@dataclass
class RippedFile:
    title_id: int
    path: Path
