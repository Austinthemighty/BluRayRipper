"""A scheduled maintenance window: have every worker idle by a given time.

Draining AT the deadline does not achieve "drained BY the deadline". A drain
tells a worker to finish its current job and exit, and an encode here runs
anywhere from a few hours to several days — so a worker that claimed a job a
minute before the deadline overruns it by days.

So the deadline works backwards instead: workers stop CLAIMING once the
deadline is within ``lead_hours``, which is the window you expect a job to fit
in. Whatever is already running keeps going, and at the deadline the ordinary
drain signal is raised for anything still busy.

The lead time is yours to set because only you know your longest encode. Too
small and a job claimed just inside it overruns; too large and the cluster sits
idle waiting for a maintenance window that is still days away.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

KEY = "maintenance.v1"          # shared-config key, same store as settings
DEFAULT_LEAD_HOURS = 24.0


@dataclass(frozen=True)
class Window:
    """When the cluster must be idle, and how far ahead to stop taking work."""

    drain_at: datetime | None = None      # always tz-aware UTC
    lead_hours: float = DEFAULT_LEAD_HOURS
    note: str = ""

    @property
    def scheduled(self) -> bool:
        return self.drain_at is not None

    def quiet_from(self) -> datetime | None:
        """When workers stop claiming new jobs."""
        if self.drain_at is None:
            return None
        return self.drain_at - timedelta(hours=max(0.0, self.lead_hours))

    def holds_new_work(self, now: datetime) -> bool:
        """Should a worker refuse to claim anything right now?"""
        start = self.quiet_from()
        return start is not None and now >= start

    def due(self, now: datetime) -> bool:
        """Has the deadline itself passed (time to raise the drain signal)?"""
        return self.drain_at is not None and now >= self.drain_at


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    # A naive timestamp is read as UTC rather than as the reading box's local
    # time: workers sit in whatever timezone they sit in, and a deadline that
    # meant a different instant on each machine would be worse than none.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def from_dict(data: dict | None) -> Window:
    if not data:
        return Window()
    try:
        lead = float(data.get("lead_hours", DEFAULT_LEAD_HOURS))
    except (TypeError, ValueError):
        lead = DEFAULT_LEAD_HOURS
    return Window(drain_at=_parse(data.get("drain_at")),
                  lead_hours=max(0.0, lead),
                  note=str(data.get("note") or ""))


def to_dict(window: Window) -> dict:
    return {"drain_at": window.drain_at.isoformat() if window.drain_at else None,
            "lead_hours": window.lead_hours,
            "note": window.note}


def load(store) -> Window:
    """Read the window from shared config; an unreachable store means none.

    "I could not read the schedule" must not become "stop taking work": a
    database blip would idle the whole cluster."""
    try:
        return from_dict(store.get_shared_config(KEY))
    except Exception:
        return Window()


def save(store, window: Window) -> None:
    store.set_shared_config(KEY, to_dict(window))
