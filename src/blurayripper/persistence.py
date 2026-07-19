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
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .models import DiscInfo, EpisodeAssignment, MediaKind, Stream, StreamType, Title

# Matches an already-stamped line — with or without the date, so lines written
# by an older build (time-only) aren't re-stamped either.
_STAMPED_RE = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}\] ")


def stamp_log(message: str) -> str:
    """Prefix a job-log line with the local date + wall-clock time it occurred,
    so the UI shows when each step (disc scan, naming, encode, …) actually
    started. Already-stamped lines (reloaded from the store) are left untouched."""
    if _STAMPED_RE.match(message):
        return message
    return f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"


# --- serializable record ------------------------------------------------

@dataclass
class WorkerInfo:
    """A worker's last-reported presence (for the UI's workers panel)."""
    worker_id: str
    version: str | None
    job_id: int | None
    stage: str | None
    seconds_since_seen: float
    uptime_seconds: float = 0.0  # since the worker PROCESS (re)started


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
    # Queue priority: higher is claimed/encoded sooner (0 = normal). "Encode next"
    # bumps this above everything currently queued.
    priority: int = 0
    # Display fields (so a worker's live progress shows in the rip box's UI):
    stage: str = ""
    progress: float = 0.0
    log: list[str] = field(default_factory=list)
    worker: str | None = None
    # Subtitle-based episode identification (survives restart mid-identify):
    auto_identify: bool = False
    show_tmdb_id: int | None = None
    season: int | None = None
    candidate_episodes: list[int] = field(default_factory=list)
    show_title: str | None = None
    show_year: int | None = None

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
        "edition": a.edition,
    }


def assignment_from_dict(d: dict) -> EpisodeAssignment:
    return EpisodeAssignment(
        title_id=d["title_id"], kind=MediaKind(d["kind"]),
        show_or_movie_title=d["show_or_movie_title"], year=d.get("year"),
        season=d.get("season"), episode=d.get("episode"), episode_title=d.get("episode_title"),
        edition=d.get("edition"),
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

    @abstractmethod
    def claim_next(
        self, worker_id: str, states: list[str], stale_after_seconds: int = 180
    ) -> JobRecord | None:
        """Atomically claim one job whose state is claimable and that is either
        unclaimed or whose worker has gone stale (no heartbeat). Returns the
        claimed record, or None if nothing is available."""

    @abstractmethod
    def heartbeat(self, job_id: int, worker_id: str) -> None:
        """Mark a claimed job alive so it isn't reclaimed by another worker."""

    @abstractmethod
    def release(self, job_id: int) -> None:
        """Drop a job's claim so another worker can pick it up."""

    def max_id(self) -> int:
        return max((r.id for r in self.load_all()), default=0)

    def record_worker(self, worker_id: str, version: str | None,
                      job_id: int | None, stage: str | None,
                      run_token: str | None = None) -> None:
        """Upsert a worker's presence/heartbeat. No-op for non-durable stores.

        ``run_token`` is a per-process id: when it changes (a worker restart), the
        store resets the worker's started_at so uptime is measured from the new
        process, not from when the worker was first ever seen."""

    def list_workers(self) -> list[WorkerInfo]:
        """Workers seen recently (for the UI). Empty for non-durable stores."""
        return []

    def get_worker_settings(self, worker_id: str) -> dict:
        """The central per-worker settings overlay for one worker ({} if none)."""
        return {}

    def set_worker_settings(self, worker_id: str, overlay: dict) -> None:
        """Store (or clear, if empty) a worker's settings overlay. No-op if
        non-durable — per-worker config needs a shared store to reach the worker."""

    def all_worker_settings(self) -> dict[str, dict]:
        """{worker_id: overlay} for every worker that has an override configured."""
        return {}

    def request_worker_update(self) -> None:
        """Bump the cluster-wide 'workers: drain + update' signal. No-op if the
        store isn't shared (there are no remote workers to reach)."""

    def worker_update_token(self) -> int:
        """Current value of the worker-update signal (0 if never requested). A
        worker updates when this rises above the value it saw at startup."""
        return 0

    @property
    def durable(self) -> bool:
        return True


class MemoryStore(Store):
    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}
        self._claimed: set[int] = set()
        self._wsettings: dict[str, dict] = {}
        self._update_token = 0

    def request_worker_update(self) -> None:
        self._update_token += 1

    def worker_update_token(self) -> int:
        return self._update_token

    def init(self) -> None:
        pass

    def get_worker_settings(self, worker_id: str) -> dict:
        import copy
        return copy.deepcopy(self._wsettings.get(worker_id, {}))

    def set_worker_settings(self, worker_id: str, overlay: dict) -> None:
        if overlay:
            import copy
            self._wsettings[worker_id] = copy.deepcopy(overlay)
        else:
            self._wsettings.pop(worker_id, None)

    def all_worker_settings(self) -> dict[str, dict]:
        import copy
        return copy.deepcopy(self._wsettings)

    def save(self, record: JobRecord) -> None:
        self._rows[record.id] = record.to_json()

    def load_all(self) -> list[JobRecord]:
        return [JobRecord.from_json(d) for _, d in sorted(self._rows.items())]

    def delete(self, job_id: int) -> None:
        self._rows.pop(job_id, None)
        self._claimed.discard(job_id)

    def claim_next(
        self, worker_id: str, states: list[str], stale_after_seconds: int = 180
    ) -> JobRecord | None:
        # Single-process: no real concurrency/heartbeat staleness to worry about.
        # Highest priority first, then oldest (lowest id).
        ordered = sorted(self._rows.items(), key=lambda kv: (-int(kv[1].get("priority", 0) or 0), kv[0]))
        for jid, data in ordered:
            if data.get("state") in states and jid not in self._claimed:
                self._claimed.add(jid)
                return JobRecord.from_json(data)
        return None

    def heartbeat(self, job_id: int, worker_id: str) -> None:
        pass

    def release(self, job_id: int) -> None:
        self._claimed.discard(job_id)

    @property
    def durable(self) -> bool:
        return False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         BIGINT PRIMARY KEY,
    data       JSONB NOT NULL,
    state      TEXT,
    claimed_by TEXT,
    heartbeat  TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS heartbeat TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS workers (
    worker_id  TEXT PRIMARY KEY,
    version    TEXT,
    job_id     BIGINT,
    stage      TEXT,
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_token  TEXT
);
ALTER TABLE workers ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE workers ADD COLUMN IF NOT EXISTS run_token TEXT;
CREATE TABLE IF NOT EXISTS worker_settings (
    worker_id  TEXT PRIMARY KEY,
    overlay    JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Cluster-wide signals (e.g. "all workers: drain + update"). A monotonic token
-- per signal; workers act when it advances past the value they saw at startup.
CREATE TABLE IF NOT EXISTS cluster_signals (
    key   TEXT PRIMARY KEY,
    token BIGINT NOT NULL DEFAULT 0
);
"""


class PostgresStore(Store):
    """Durable store backed by Postgres; one JSONB row per job."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._down_until = 0.0  # circuit breaker: skip connects until this time

    def _connect(self):
        import psycopg  # lazy: only needed when Postgres is configured

        # Circuit breaker: once connects fail, fail the next few seconds of calls
        # INSTANTLY instead of blocking on each. Without this, a dead DB freezes
        # the async event loop (and thus the whole UI) on every persist.
        if time.monotonic() < self._down_until:
            raise ConnectionError("database unreachable (in cooldown after a recent failure)")
        # A fresh connection per call means an occasional transient blip (a brief
        # network hiccup, a momentary refusal while Postgres is busy) would trip
        # the breaker and blank the store for 15s even though the DB is fine. Retry
        # a couple of times with a short backoff first, so only a SUSTAINED failure
        # opens the breaker. connect_timeout bounds a single hang (libpq clamps >=2s).
        last: Exception | None = None
        for attempt in range(3):
            try:
                conn = psycopg.connect(self._url, autocommit=True, connect_timeout=4)
                self._down_until = 0.0  # recovered / healthy
                return conn
            except Exception as exc:
                last = exc
                if attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
        self._down_until = time.monotonic() + 15  # sustained failure — back off
        raise last

    def init(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_SCHEMA)

    def save(self, record: JobRecord) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (id, data, state) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "data = EXCLUDED.data, state = EXCLUDED.state, updated_at = now()",
                (record.id, Jsonb(record.to_json()), record.state),
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

    def claim_next(
        self, worker_id: str, states: list[str], stale_after_seconds: int = 180
    ) -> JobRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET claimed_by = %s, heartbeat = now() "
                "WHERE id = ("
                "  SELECT id FROM jobs"
                "  WHERE state = ANY(%s)"
                "    AND (claimed_by IS NULL OR heartbeat < now() - make_interval(secs => %s))"
                "  ORDER BY COALESCE((data->>'priority')::int, 0) DESC, id ASC "
                "  FOR UPDATE SKIP LOCKED LIMIT 1"
                ") RETURNING data",
                (worker_id, list(states), stale_after_seconds),
            )
            row = cur.fetchone()
        if not row:
            return None
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return JobRecord.from_json(data)

    def heartbeat(self, job_id: int, worker_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET heartbeat = now() WHERE id = %s AND claimed_by = %s",
                (job_id, worker_id),
            )

    def release(self, job_id: int) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE jobs SET claimed_by = NULL WHERE id = %s", (job_id,))

    def record_worker(self, worker_id: str, version: str | None,
                      job_id: int | None, stage: str | None,
                      run_token: str | None = None) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workers (worker_id, version, job_id, stage, last_seen, "
                "run_token) VALUES (%s, %s, %s, %s, now(), %s) "
                "ON CONFLICT (worker_id) DO UPDATE SET "
                "version = EXCLUDED.version, job_id = EXCLUDED.job_id, "
                "stage = EXCLUDED.stage, last_seen = now(), "
                # Reset the clock only when the process changed (new run_token).
                "started_at = CASE WHEN workers.run_token IS DISTINCT FROM EXCLUDED.run_token "
                "THEN now() ELSE workers.started_at END, "
                "run_token = EXCLUDED.run_token",
                (worker_id, version, job_id, stage, run_token),
            )

    def request_worker_update(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cluster_signals (key, token) VALUES ('worker_update', 1) "
                "ON CONFLICT (key) DO UPDATE SET token = cluster_signals.token + 1")

    def worker_update_token(self) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT token FROM cluster_signals WHERE key = 'worker_update'")
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def get_worker_settings(self, worker_id: str) -> dict:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT overlay FROM worker_settings WHERE worker_id = %s", (worker_id,))
            row = cur.fetchone()
        if not row:
            return {}
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])

    def set_worker_settings(self, worker_id: str, overlay: dict) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            if not overlay:
                cur.execute("DELETE FROM worker_settings WHERE worker_id = %s", (worker_id,))
                return
            from psycopg.types.json import Jsonb
            cur.execute(
                "INSERT INTO worker_settings (worker_id, overlay) VALUES (%s, %s) "
                "ON CONFLICT (worker_id) DO UPDATE SET overlay = EXCLUDED.overlay, "
                "updated_at = now()",
                (worker_id, Jsonb(overlay)),
            )

    def all_worker_settings(self) -> dict[str, dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT worker_id, overlay FROM worker_settings")
            rows = cur.fetchall()
        return {wid: (ov if isinstance(ov, dict) else json.loads(ov)) for wid, ov in rows}

    def list_workers(self) -> list[WorkerInfo]:
        with self._connect() as conn, conn.cursor() as cur:
            # Only workers seen in the last 10 minutes; drop long-gone ones.
            cur.execute(
                "DELETE FROM workers WHERE last_seen < now() - interval '1 hour'")
            cur.execute(
                "SELECT worker_id, version, job_id, stage, "
                "EXTRACT(EPOCH FROM (now() - last_seen)), "
                "EXTRACT(EPOCH FROM (now() - started_at)) FROM workers "
                "WHERE last_seen > now() - interval '10 minutes' ORDER BY worker_id")
            rows = cur.fetchall()
        return [WorkerInfo(w, v, j, s, float(age or 0), float(up or 0))
                for (w, v, j, s, age, up) in rows]


def make_store(database_url: str | None) -> Store:
    """Build the store for the configured database URL (Memory if unset)."""
    if database_url:
        return PostgresStore(database_url)
    return MemoryStore()
