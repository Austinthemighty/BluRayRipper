"""Track which boxes in the cluster are alive, and notice when one drops.

The rip box watches the workers; each worker watches the rip box — an offline
server can't report that it's offline, so something else has to. Both sides use
the same ``workers`` table, whose ``last_seen`` is written and aged with the
DATABASE clock, so staleness never depends on two machines agreeing on the time.

The state machine here is deliberately separate from the I/O so it can be tested
without a database: feed it what the store reported, get back the transitions
worth notifying about.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The rip box registers itself in the same table as the workers so it gets the
# same DB-clock heartbeat for free. The prefix keeps it out of the Workers panel.
SERVE_PREFIX = "serve:"


def serve_id(hostname: str) -> str:
    return f"{SERVE_PREFIX}{hostname}"


def is_serve(worker_id: str) -> bool:
    return (worker_id or "").startswith(SERVE_PREFIX)


@dataclass
class PresenceWatch:
    """Remembers who has been seen, and reports offline/back-online transitions.

    Only boxes seen at least once are ever reported missing, so a fresh start
    never alerts about a machine that was already down (and one that has never
    existed can't alert at all). Each transition fires once — an offline box
    stays quiet until it comes back.
    """

    threshold_seconds: float
    seen: set[str] = field(default_factory=set)
    offline: set[str] = field(default_factory=set)

    def update(self, ages: dict[str, float]) -> tuple[list[str], list[str]]:
        """Feed the current ``{box_id: seconds_since_last_seen}``.

        Returns ``(went_offline, came_back)`` — ids whose state changed on THIS
        update, so the caller notifies once per transition. A box missing from
        ``ages`` entirely counts as offline: the store drops rows it hasn't heard
        from in a while, so vanishing is how a long outage actually looks.
        """
        if self.threshold_seconds <= 0:
            return [], []          # watching disabled
        went_offline, came_back = [], []
        for box_id, age in ages.items():
            if age <= self.threshold_seconds:
                self.seen.add(box_id)
                if box_id in self.offline:
                    self.offline.discard(box_id)
                    came_back.append(box_id)
        for box_id in sorted(self.seen):
            age = ages.get(box_id)
            gone = age is None or age > self.threshold_seconds
            if gone and box_id not in self.offline:
                self.offline.add(box_id)
                went_offline.append(box_id)
        return sorted(went_offline), sorted(came_back)


def describe(seconds: float) -> str:
    """A short human duration for the alert text ('7m', '2h05m')."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"
