"""Updating a box's checkout, including after main's history was rewritten.

Every commit on main was rewritten to change the author email. The same changes
now have new commit ids, so `git pull --ff-only` can never move an old checkout
forward. handbreak01 and handbreak02 sat on 8ecc42f for six weeks: they tried
at every restart, failed, and restarted on the old code, while the workers panel
showed them as healthy. These tests run real git against throwaway repositories.
"""

import shutil
import subprocess

import pytest

from blurayripper.cli import _sync_checkout

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

GIT = shutil.which("git") or "git"
OLD = ["-c", "user.name=Austin", "-c", "user.email=old@aol.example"]
NEW = ["-c", "user.name=Austin", "-c", "user.email=new@noreply.example"]


def git(repo, *args, ident=OLD):
    out = subprocess.run([GIT, "-C", str(repo), *ident, *args],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def commit(repo, name, text, ident=OLD):
    (repo / name).write_text(text)
    git(repo, "add", name, ident=ident)
    git(repo, "commit", "-q", "-m", f"add {name}", ident=ident)


@pytest.fixture
def cluster(tmp_path):
    """An upstream with two commits, and a worker cloned from it."""
    up = tmp_path / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    commit(up, "a.txt", "a\n")
    commit(up, "b.txt", "b\n")
    worker = tmp_path / "worker"
    subprocess.run([GIT, "clone", "-q", str(up), str(worker)], check=True)
    return up, worker


def rewrite_authors(up):
    """What happened to main: every commit re-made under a new email."""
    git(up, "rebase", "-q", "--root", "--exec",
        f"{GIT} -c user.name=Austin -c user.email=new@noreply.example "
        "commit -q --amend --no-edit --reset-author", ident=NEW)


def test_an_ordinary_update_fast_forwards(cluster):
    up, worker = cluster
    commit(up, "c.txt", "c\n")

    ok, _ = _sync_checkout(GIT, worker)

    assert ok and (worker / "c.txt").exists()


def test_a_rewritten_main_is_followed_instead_of_failing_forever(cluster):
    up, worker = cluster
    rewrite_authors(up)
    commit(up, "c.txt", "c\n", ident=NEW)        # and new work on top

    ok, said = _sync_checkout(GIT, worker)

    assert ok, said
    assert "rewritten upstream" in said
    assert git(worker, "rev-parse", "HEAD") == git(up, "rev-parse", "HEAD")
    assert (worker / "c.txt").exists()


def test_a_local_commit_that_is_not_upstream_is_never_thrown_away(cluster):
    up, worker = cluster
    rewrite_authors(up)
    commit(worker, "mine.txt", "only here\n")
    before = git(worker, "rev-parse", "HEAD")

    ok, said = _sync_checkout(GIT, worker)

    assert not ok and "1 local commit(s)" in said
    assert git(worker, "rev-parse", "HEAD") == before


def test_local_edits_are_never_thrown_away(cluster):
    up, worker = cluster
    rewrite_authors(up)
    (worker / "a.txt").write_text("edited on the box\n")

    ok, said = _sync_checkout(GIT, worker)

    assert not ok and "local changes" in said
    assert (worker / "a.txt").read_text() == "edited on the box\n"


def test_an_untracked_file_upstream_now_tracks_is_never_overwritten(cluster):
    up, worker = cluster
    rewrite_authors(up)
    commit(up, "secrets.env", "upstream\n", ident=NEW)
    (worker / "secrets.env").write_text("the box's own\n")

    ok, said = _sync_checkout(GIT, worker)

    assert not ok and "secrets.env" in said
    assert (worker / "secrets.env").read_text() == "the box's own\n"


def test_a_pull_that_fails_for_another_reason_changes_nothing(cluster, tmp_path):
    up, worker = cluster
    git(worker, "remote", "set-url", "origin", str(tmp_path / "gone"))
    before = git(worker, "rev-parse", "HEAD")

    ok, said = _sync_checkout(GIT, worker)

    assert not ok and "git pull failed" in said
    assert git(worker, "rev-parse", "HEAD") == before


# --- the workers panel says when a box cannot update -------------------------

from blurayripper.version import code_relation  # noqa: E402


def ver(repo, rev="HEAD"):
    return f"0.1.0+9.{git(repo, 'rev-parse', '--short', rev)}"


def test_the_panel_can_tell_a_box_that_can_never_update_from_one_that_is_behind(cluster):
    up, worker = cluster
    old = ver(worker)
    assert code_relation(old, ver(up), repo=up) == ("current", 0)

    commit(up, "c.txt", "c\n")
    commit(up, "d.txt", "d\n")
    assert code_relation(old, ver(up), repo=up) == ("behind", 2)

    # handbreak01/02: main rewritten under them. Behind is fixable by Update;
    # this is not, and must not look like it is.
    rewrite_authors(up)
    assert code_relation(old, ver(up), repo=up) == ("off_main", None)
    assert code_relation("0.1.0+9.0123abc", ver(up), repo=up) == ("off_main", None)


def test_a_box_ahead_of_the_rip_box_or_without_git_is_not_flagged(cluster):
    up, _worker = cluster
    commit(up, "c.txt", "c\n")
    newer = ver(up)
    git(up, "reset", "-q", "--hard", "HEAD~1")         # the rip box is a commit behind
    assert code_relation(newer, ver(up), repo=up) == ("ahead", 1)
    assert code_relation("0.1.0", ver(up), repo=up) == ("unknown", None)
