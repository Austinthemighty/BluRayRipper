"""Persistent store of confirmed disc-fingerprint -> episode mappings.

The durable payoff of the project: once a user confirms how a disc's titles map
to episodes, we remember it keyed by the disc fingerprint. Re-ripping the same
disc (or an identical copy) then runs fully automatically. Uses stdlib sqlite3.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..models import EpisodeAssignment, MediaKind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS disc_mapping (
    fingerprint TEXT PRIMARY KEY,
    disc_name   TEXT,
    assignments TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class MappingStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def get(self, fingerprint: str) -> list[EpisodeAssignment] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT assignments FROM disc_mapping WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if row is None:
            return None
        return [_deserialize(d) for d in json.loads(row[0])]

    def save(
        self,
        fingerprint: str,
        disc_name: str | None,
        assignments: list[EpisodeAssignment],
    ) -> None:
        payload = json.dumps([_serialize(a) for a in assignments])
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO disc_mapping (fingerprint, disc_name, assignments) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET "
                "disc_name = excluded.disc_name, assignments = excluded.assignments",
                (fingerprint, disc_name, payload),
            )

    def forget(self, fingerprint: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM disc_mapping WHERE fingerprint = ?", (fingerprint,))


def _serialize(a: EpisodeAssignment) -> dict:
    return {
        "title_id": a.title_id,
        "kind": a.kind.value,
        "show_or_movie_title": a.show_or_movie_title,
        "year": a.year,
        "season": a.season,
        "episode": a.episode,
        "episode_title": a.episode_title,
    }


def _deserialize(d: dict) -> EpisodeAssignment:
    return EpisodeAssignment(
        title_id=d["title_id"],
        kind=MediaKind(d["kind"]),
        show_or_movie_title=d["show_or_movie_title"],
        year=d.get("year"),
        season=d.get("season"),
        episode=d.get("episode"),
        episode_title=d.get("episode_title"),
    )
