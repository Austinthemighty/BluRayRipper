"""Fire-and-forget push notifications via a simple webhook.

Posts the message as the request body with a ``Title`` (and optional ``Priority``)
header — the ntfy.sh convention (self-hosted or ntfy.sh), which is the easiest
lightweight push to run. Any endpoint that accepts a plain-text POST works.
Best-effort: failures are swallowed so a down notifier never affects a job.
"""

from __future__ import annotations

import httpx


async def notify(url: str | None, title: str, message: str,
                 priority: str | None = None) -> bool:
    """POST ``message`` to ``url``. Returns True on a 2xx, False otherwise (or if
    no url is configured). Never raises."""
    if not url:
        return False
    headers = {"Title": title}
    if priority:
        headers["Priority"] = priority  # ntfy: 1..5 / min..urgent
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, content=message.encode("utf-8"), headers=headers)
        return resp.is_success
    except Exception:
        return False
