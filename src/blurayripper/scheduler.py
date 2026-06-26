"""Weighted concurrency scheduler for the encode stage.

Ripping is I/O-bound and runs freely in parallel across drives. Encoding is
CPU-bound: two simultaneous 4K x265 encodes just split the cores and finish no
faster. But a heavy UHD encode leaves cores idle that a lighter 1080p/DVD
encode can use. So we gate encodes by a weight budget:

  UHD (2160p+) = 2 units,  HD/SD (<=1080p) = 1 unit,  capacity = 3 (default)

That allows "1 UHD + 1 lighter disc" or "up to 3 DVDs" concurrently, but never
two UHD encodes at once (2+2 > 3).
"""

from __future__ import annotations

import asyncio

# Height (px) at/above which an encode counts as "heavy" (UHD).
UHD_MIN_HEIGHT = 1700
WEIGHT_UHD = 2
WEIGHT_OTHER = 1


def encode_weight(video_height: int | None) -> int:
    """Map a video height to an encode weight."""
    if video_height and video_height >= UHD_MIN_HEIGHT:
        return WEIGHT_UHD
    return WEIGHT_OTHER


class EncodeScheduler:
    """Async weighted semaphore. ``acquire`` blocks until ``weight`` units fit."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self._in_use = 0
        self._cond = asyncio.Condition()

    async def acquire(self, weight: int) -> int:
        # Cap a single job's weight to capacity so it can never deadlock itself.
        w = max(1, min(weight, self.capacity))
        async with self._cond:
            while self._in_use + w > self.capacity:
                await self._cond.wait()
            self._in_use += w
        return w

    async def release(self, weight: int) -> None:
        async with self._cond:
            self._in_use = max(0, self._in_use - weight)
            self._cond.notify_all()
