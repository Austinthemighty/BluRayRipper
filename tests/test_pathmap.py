"""Translating stored absolute paths between machines (Windows <-> Linux)."""

from pathlib import Path

from blurayripper.services.pathmap import (
    looks_foreign, parse_rules, resolve, translate,
)

RULES = parse_rules([r"Z:\media = /mnt/TrueNas/media"])


def test_translates_a_windows_path_to_the_linux_mount():
    got = translate(r"Z:\media\MakeMKV\To Process\MKV's\Cars 3 (2017).mkv", RULES)
    assert got == "/mnt/TrueNas/media/MakeMKV/To Process/MKV's/Cars 3 (2017).mkv"


def test_matching_ignores_separator_style_and_case():
    """The same location gets written differently depending on which box stored
    it, and Windows paths are case-insensitive — neither can be trusted."""
    assert translate("Z:/MEDIA/MakeMKV/x.mkv", RULES) == "/mnt/TrueNas/media/MakeMKV/x.mkv"
    assert translate(r"z:\media\MakeMKV\x.mkv", RULES) == "/mnt/TrueNas/media/MakeMKV/x.mkv"


def test_only_whole_components_match():
    """A rule for Z:\\media must not rewrite Z:\\mediaX."""
    assert translate(r"Z:\mediaOTHER\x.mkv", RULES) == r"Z:\mediaOTHER\x.mkv"


def test_unmatched_paths_are_left_alone():
    assert translate(r"D:\somewhere\else.mkv", RULES) == r"D:\somewhere\else.mkv"
    assert translate("/already/linux/x.mkv", RULES) == "/already/linux/x.mkv"
    assert translate("", RULES) == ""
    assert translate("anything", []) == "anything"


def test_longest_rule_wins():
    rules = parse_rules([r"Z:\ = /mnt/a", r"Z:\media = /mnt/TrueNas/media"])
    assert translate(r"Z:\media\x.mkv", rules) == "/mnt/TrueNas/media/x.mkv"


def test_malformed_rules_are_skipped_not_fatal():
    """A typo in config must not stop the server booting."""
    rules = parse_rules(["no equals sign", "= /only/new", r"Z:\a =", None, 42,
                         r"Z:\media = /mnt/TrueNas/media"])
    assert len(rules) == 1 and rules[0].new == "/mnt/TrueNas/media"


def test_a_replacement_containing_equals_survives():
    rules = parse_rules(["Z:/a = /mnt/x=y/a"])
    assert translate("Z:/a/f.mkv", rules) == "/mnt/x=y/a/f.mkv"


def test_resolve_prefers_a_path_that_actually_exists(tmp_path):
    """On a box where the stored path is still valid — the Windows worker in a
    mixed cluster — nothing may be rewritten. That's what makes one shared rule
    safe to configure everywhere."""
    real = tmp_path / "movie.mkv"
    real.write_bytes(b"x")
    rules = parse_rules([f"{tmp_path} = /somewhere/else"])
    assert resolve(str(real), rules) == real          # exists -> untouched


def test_resolve_translates_when_the_original_is_absent():
    got = resolve(r"Z:\media\MakeMKV\x.mkv", RULES)
    assert str(got) == "/mnt/TrueNas/media/MakeMKV/x.mkv"


def test_resolve_without_rules_is_a_no_op():
    assert resolve(r"Z:\media\x.mkv", []) == Path(r"Z:\media\x.mkv")


def test_looks_foreign():
    assert looks_foreign(r"Z:\media\x.mkv")
    assert looks_foreign("C:/Users/x")
    assert not looks_foreign("/mnt/TrueNas/media/x.mkv")
    assert not looks_foreign("")


def test_pipeline_resolves_a_foreign_checkpoint_through_the_map(tmp_path, monkeypatch):
    """End-to-end: a job added from a file on the old Windows box stores an
    ABSOLUTE source (unlike rip checkpoints, which are relative and already
    float). The prefix map is what lets those 131 queued jobs resolve on Linux
    without touching a database row."""
    from blurayripper import pipeline as pl

    real = tmp_path / "MakeMKV" / "To Process" / "Cars 3 (2017).mkv"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"x")

    pl.configure_path_map([rf"Z:\media = {tmp_path}"])
    try:
        stored = r"Z:\media\MakeMKV\To Process\Cars 3 (2017).mkv"
        got = pl._resolve_path(tmp_path / "staging", stored)
        assert got == real and got.exists()

        # A relative checkpoint is unaffected — it never needed the map.
        rel = pl._resolve_path(tmp_path / "staging", "job_234/rip/t002/t002.mkv")
        assert rel == tmp_path / "staging" / "job_234" / "rip" / "t002" / "t002.mkv"
    finally:
        pl.configure_path_map([])


def test_no_rules_leaves_resolution_exactly_as_before(tmp_path):
    """Unconfigured must be a strict no-op — this is core path plumbing."""
    from blurayripper import pipeline as pl

    pl.configure_path_map([])
    stored = r"Z:\media\MakeMKV\x.mkv"
    assert str(pl._resolve_path(tmp_path, stored)) == stored
