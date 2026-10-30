"""Fire-and-forget push notifications via a simple webhook.

Posts the message as the request body with a ``Title`` (and optional ``Priority``
and ``Click``) header — the ntfy.sh convention (self-hosted or ntfy.sh), which is
the easiest lightweight push to run. Any endpoint that accepts a plain-text POST
works.
Best-effort: failures are swallowed so a down notifier never affects a job.
"""

from __future__ import annotations

import httpx


def click_link(settings, job_id: int | None = None, *, derive: bool = True) -> str | None:
    """The web UI address a tapped notification opens, or None if there is none
    a phone could reach.

    The configured notify_click_url wins. Otherwise (``derive``) it is this box's
    own address when serve listens beyond localhost. A worker passes
    derive=False: its own address is not where the UI is. ``job_id`` opens the
    page at that job (the UI reads #job-N on load)."""
    base = (getattr(settings, "notify_click_url", None) or "").strip().rstrip("/")
    if not base and derive:
        host = settings.web_host
        if host in ("0.0.0.0", "::", ""):
            from ..worker import local_address
            host = local_address(settings.database_url)
        if host and host not in ("127.0.0.1", "localhost", "::1"):
            base = f"http://{host}:{settings.web_port}"
    if not base:
        return None
    return f"{base}/#job-{job_id}" if job_id else base


async def notify(url: str | None, title: str, message: str,
                 priority: str | None = None, click: str | None = None) -> bool:
    """POST ``message`` to ``url``. Returns True on a 2xx, False otherwise (or if
    no url is configured). Never raises. ``click`` is the link ntfy opens when
    the notification is tapped."""
    if not url:
        return False
    # The Title goes in an HTTP header, which httpx encodes as latin-1 — an emoji
    # (💿/❌/📺…) would raise UnicodeEncodeError and be swallowed as a failure. Send
    # it as raw UTF-8 bytes instead; ntfy decodes that and keeps the emoji.
    headers: dict[str, str | bytes] = {"Title": title.encode("utf-8")}
    if priority:
        headers["Priority"] = priority  # ntfy: 1..5 / min..urgent (ASCII)
    if click:
        headers["Click"] = click.encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, content=message.encode("utf-8"), headers=headers)
        return resp.is_success
    except Exception:
        return False
