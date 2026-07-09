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
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("unreachable")

    monkeypatch.setitem(sys.modules, "psycopg", _fake_psycopg(boom))
    store = PostgresStore("postgresql://nope")

    # First connect actually tries (and fails), tripping the breaker.
    with pytest.raises(RuntimeError):
        store._connect()
    assert calls["n"] == 1

    # Next connect fails INSTANTLY without calling psycopg.connect again.
    with pytest.raises(ConnectionError):
        store._connect()
    assert calls["n"] == 1  # not retried during cooldown


def test_breaker_resets_after_successful_connect(monkeypatch):
    store = PostgresStore("postgresql://ok")
    store._down_until = 0.0

    monkeypatch.setitem(sys.modules, "psycopg", _fake_psycopg(lambda *a, **k: "conn"))
    assert store._connect() == "conn"
    assert store._down_until == 0.0  # healthy, no cooldown
