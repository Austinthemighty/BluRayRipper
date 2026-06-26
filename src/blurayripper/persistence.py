"""Job persistence for crash recovery.

A ``Store`` snapshots each job (its gate state, naming mapping, and
per-title checkpoints) so that if the server dies mid-run it can recover on
restart and resume from the last completed checkpoint — most importantly
without re-ripping a disc whose rip already finished.

Two backends:
  - MemoryStore   — default; no durability (behaves like the old in-memory app).
  - PostgresStore — durable; persists each job as a JSONB row.

The serialization layer (JobRecord + disc/assignment helpers) is shared, so the
recovery logic is identical regardless of backend and is unit-tested via
MemoryStore without needing a database.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

from .models import DiscInfo, EpisodeAssignment, MediaKind, Stream, StreamType, Title


# --- serializable record ------------------------------------------------

@dataclass
class JobRecord:
    """A JSON-native snapshot of a job, durable across restarts."""

    id: int
    source: str
    state: str
    fingerprint: str | None = None
    disc: dict | None = None                       # serialized DiscInfo
    selected_title_ids: list[int] = field(default_factory=list)
    assignments: list[dict] = field(default_factory=list)  # serialized EpisodeAssignment
    grain_mode: str = "auto"
    tune_grain: bool | None = None
    checkpoints: dict = field(default_factory=dict)  # {str(title_id): {ripped_path, placed}}
    outputs: list[str] = field(default_factory=list)
    error: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "JobRecord":
        return cls(**data)


# --- disc / assignment (de)serialization (shared) -----------------------

def disc_to_dict(disc: DiscInfo | None) -> dict | None:
    if disc is None:
        return None
    return {
        "source": disc.source,
        "disc_name": disc.disc_name,
        "volume_label": disc.volume_label,
        "drive_device": disc.drive_device,
        "titles": [
            {
                "title_id": t.title_id,
                "duration_seconds": t.duration_seconds,
                "chapter_count": t.chapter_count,
                "size_bytes": t.size_bytes,
                "source_filename": t.source_filename,
                "output_filename": t.output_filename,
                "streams": [
                    {"stream_id": s.stream_id, "type": s.stream_type.value,
                     "codec": s.codec, "language": s.language}
                    for s in t.streams
                ],
            }
            for t in disc.titles
        ],
    }


def disc_from_dict(data: dict | None) -> DiscInfo | None:
    if not data:
        return None
    titles = []
    for t in data.get("titles", []):
        streams = [
            Stream(stream_id=s["stream_id"], stream_type=StreamType(s["type"]),
                   codec=s.get("codec"), language=s.get("language"))
            for s in t.get("streams", [])
        ]
        titles.append(Title(
            title_id=t["title_id"], duration_seconds=t["duration_seconds"],
            chapter_count=t["chapter_count"], size_bytes=t["size_bytes"],
            source_filename=t.get("source_filename"), output_filename=t.get("output_filename"),
            streams=streams,
        ))
    return DiscInfo(
        source=data["source"], disc_name=data.get("disc_name"),
        volume_label=data.get("volume_label"), drive_device=data.get("drive_device"),
        titles=titles,
    )


def assignment_to_dict(a: EpisodeAssignment) -> dict:
    return {
        "title_id": a.title_id, "kind": a.kind.value,
        "show_or_movie_title": a.show_or_movie_title, "year": a.year,
        "season": a.season, "episode": a.episode, "episode_title": a.episode_title,
    }


def assignment_from_dict(d: dict) -> EpisodeAssignment:
    return EpisodeAssignment(
        title_id=d["title_id"], kind=MediaKind(d["kind"]),
        show_or_movie_title=d["show_or_movie_title"], year=d.get("year"),
        season=d.get("season"), episode=d.get("episode"), episode_title=d.get("episode_title"),
    )


# --- stores -------------------------------------------------------------

class Store(ABC):
    @abstractmethod
    def init(self) -> None:
        """Create any required schema. Safe to call repeatedly."""

    @abstractmethod
    def save(self, record: JobRecord) -> None:
        """Insert or update a job record (keyed by id)."""

    @abstractmethod
    def load_all(self) -> list[JobRecord]:
        """Return all persisted records, ordered by id."""

    @abstractmethod
    def delete(self, job_id: int) -> None: ...

    def max_id(self) -> int:
        return max((r.id for r in self.load_all()), default=0)

    @property
    def durable(self) -> bool:
        return True


class MemoryStore(Store):
    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}

    def init(self) -> None:
        pass

    def save(self, record: JobRecord) -> None:
        self._rows[record.id] = record.to_json()

    def load_all(self) -> list[JobRecord]:
        return [JobRecord.from_json(d) for _, d in sorted(self._rows.items())]

    def delete(self, job_id: int) -> None:
        self._rows.pop(job_id, None)

    @property
    def durable(self) -> bool:
        return False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         BIGINT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PostgresStore(Store):
    """Durable store backed by Postgres; one JSONB row per job."""

    def __init__(self, url: str) -> None:
        self._url = url

    def _connect(self):
        import psycopg  # lazy: only needed when Postgres is configured

        return psycopg.connect(self._url, autocommit=True)

    def init(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_SCHEMA)

    def save(self, record: JobRecord) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (id, data) VALUES (%s, %s) "
                "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                (record.id, Jsonb(record.to_json())),
            )

    def load_all(self) -> list[JobRecord]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM jobs ORDER BY id")
            rows = cur.fetchall()
        out = []
        for (data,) in rows:
            out.append(JobRecord.from_json(data if isinstance(data, dict) else json.loads(data)))
        return out

    def delete(self, job_id: int) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))


def make_store(database_url: str | None) -> Store:
    """Build the store for the configured database URL (Memory if unset)."""
    if database_url:
        return PostgresStore(database_url)
    return MemoryStore()
