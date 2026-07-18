"""PostgresStore circuit breaker: once a connect fails, further connects are
skipped for a cooldown so a dead DB can't block the event loop on every call."""

import sys
import types

import pytest

from blurayripper.persistence import PostgresStore


def _fake_psycopg(on_connect):
    mod = types.ModuleType("psycopg")
    mod.connect = on_connect
    return mod


def test_circuit_breaker_skips_connect_during_cooldown(monkeypatch):
    import blurayripper.persistence as P
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("unreachable")

    monkeypatch.setitem(sys.modules, "psycopg", _fake_psycopg(boom))
    monkeypatch.setattr(P.time, "sleep", lambda *_a: None)  # skip the retry backoff
    store = PostgresStore("postgresql://nope")

    # First connect retries a few times within the call (all fail), then trips the
    # breaker — so a single transient blip wouldn't open it, only a sustained one.
    with pytest.raises(RuntimeError):
        store._connect()
    assert calls["n"] == 3

    # Next connect fails INSTANTLY without calling psycopg.connect again.
    with pytest.raises(ConnectionError):
        store._connect()
    assert calls["n"] == 3  # cooldown — not retried


def test_breaker_resets_after_successful_connect(monkeypatch):
    store = PostgresStore("postgresql://ok")
    store._down_until = 0.0

    monkeypatch.setitem(sys.modules, "psycopg", _fake_psycopg(lambda *a, **k: "conn"))
    assert store._connect() == "conn"
    assert store._down_until == 0.0  # healthy, no cooldown
