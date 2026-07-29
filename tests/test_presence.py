"""Offline detection: who's alive, and which transitions are worth a notification."""

from blurayripper.presence import PresenceWatch, describe, is_serve, serve_id


def test_reports_a_box_going_offline_once():
    w = PresenceWatch(threshold_seconds=300)
    assert w.update({"w1": 5}) == ([], [])          # first sight: alive, no news

    assert w.update({"w1": 400}) == (["w1"], [])    # gone quiet -> alert
    assert w.update({"w1": 500}) == ([], [])        # still gone -> stay quiet
    assert w.update({"w1": 900}) == ([], [])


def test_reports_recovery_once():
    w = PresenceWatch(threshold_seconds=300)
    w.update({"w1": 5})
    w.update({"w1": 400})
    assert w.update({"w1": 3}) == ([], ["w1"])      # back -> one recovery alert
    assert w.update({"w1": 4}) == ([], [])
    assert w.update({"w1": 999}) == (["w1"], [])    # and it can alert again later


def test_vanishing_from_the_list_counts_as_offline():
    """The store drops rows it hasn't heard from in a while, so a long outage
    shows up as the box disappearing rather than as a large age."""
    w = PresenceWatch(threshold_seconds=300)
    w.update({"w1": 5, "w2": 5})
    assert w.update({"w2": 5}) == (["w1"], [])
    assert w.update({"w2": 5}) == ([], [])          # not repeated
    assert w.update({"w1": 5, "w2": 5}) == ([], ["w1"])


def test_never_alerts_about_a_box_it_has_not_seen_alive():
    """A fresh start must not alert about machines that were already down —
    otherwise every restart pages you about a worker you switched off weeks ago."""
    w = PresenceWatch(threshold_seconds=300)
    assert w.update({}) == ([], [])
    assert w.update({"old": 9999}) == ([], [])      # only ever seen stale -> silent
    assert w.update({}) == ([], [])


def test_several_boxes_are_tracked_independently():
    w = PresenceWatch(threshold_seconds=300)
    w.update({"a": 1, "b": 1, "c": 1})
    assert w.update({"a": 1, "b": 400, "c": 400}) == (["b", "c"], [])
    assert w.update({"a": 1, "b": 1, "c": 400}) == ([], ["b"])


def test_threshold_of_zero_disables_watching():
    w = PresenceWatch(threshold_seconds=0)
    w.update({"w1": 1})
    assert w.update({"w1": 100000}) == ([], [])


def test_serve_id_round_trip():
    """The rip box shares the workers table but must not show up as a worker."""
    assert is_serve(serve_id("RIPBOX"))
    assert not is_serve("SteamMachine")
    assert not is_serve("")
    assert serve_id("RIPBOX").endswith("RIPBOX")


def test_describe_durations():
    assert describe(45) == "45s"
    assert describe(420) == "7m"
    assert describe(7500) == "2h05m"
    assert describe(-5) == "0s"
