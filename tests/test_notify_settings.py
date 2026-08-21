"""ntfy configuration lives on the Settings page, with its own group and a test.

A push target is the one setting you cannot verify by looking at it: a typo'd
topic or an unreachable self-hosted server behaves exactly like "nothing has
gone wrong yet" until the alert you actually needed never arrives.
"""

from blurayripper.config import load_settings
from blurayripper.settings_store import SCHEMA, _FIELDS


def test_ntfy_has_its_own_settings_group():
    """It was buried in 'Serve' among process-cleanup and retry knobs, which is
    why it read as missing."""
    titles = [title for title, _ in SCHEMA]
    assert "Notifications" in titles
    fields = next(f for t, f in SCHEMA if t == "Notifications")
    assert {f.key for f in fields} == {"notify_url", "notify_priority"}


def test_the_url_still_defers_to_secrets_env():
    """A webhook URL usually embeds a token, so secrets.env has to keep winning —
    otherwise the settings page would quietly overwrite it."""
    assert _FIELDS[("serve", "notify_url")].env == "BLURAYRIPPER_NOTIFY_URL"


def test_priority_is_a_closed_choice():
    """ntfy only accepts these; a free-text box invites a value that silently
    downgrades every alert."""
    assert _FIELDS[("serve", "notify_priority")].choices == (
        "min", "low", "default", "high", "urgent")


def test_priority_defaults_to_default_and_is_configurable():
    assert load_settings().notify_priority == "default"


def test_a_configured_priority_is_used_when_an_alert_does_not_set_one(monkeypatch):
    """The point of configuring it: routine alerts should ring the phone if the
    user asked for that, without every call site passing a priority."""
    from blurayripper.persistence import MemoryStore
    from blurayripper.web.manager import JobManager

    settings = load_settings()
    settings.notify_url = "https://ntfy.example.com/topic"
    settings.notify_priority = "high"
    m = JobManager(settings, store=MemoryStore())

    sent = []
    monkeypatch.setattr("blurayripper.web.manager.notify",
                        lambda url, title, msg, prio: sent.append(prio))
    monkeypatch.setattr(m, "_spawn", lambda coro: None)
    m._notify("t", "m")
    assert sent == ["high"]

    sent.clear()
    m._notify("t", "m", "urgent")     # an explicit escalation still wins
    assert sent == ["urgent"]
