"""Version stays informative even without the git binary (reads .git directly)."""

from blurayripper import version


def test_get_version_carries_commit_marker_in_a_checkout():
    v = version.get_version()
    assert v.startswith("0.1.0")
    assert "+" in v  # a git checkout should always yield a commit suffix


def test_sha_from_git_dir_reads_head():
    sha = version._sha_from_git_dir()
    assert sha is None or (isinstance(sha, str) and 5 <= len(sha) <= 12)


def test_fallback_used_when_git_binary_missing(monkeypatch):
    monkeypatch.setattr(version.shutil, "which", lambda _n: None)
    monkeypatch.setattr(version, "_git_candidates", list)  # no candidate paths
    version._git_exe.cache_clear()
    version.get_version.cache_clear()
    try:
        v = version.get_version()
        # no git binary -> the "+g<sha>" (SHA-only) form, not bare base
        assert v == "0.1.0" or v.startswith("0.1.0+g")
    finally:
        version._git_exe.cache_clear()
        version.get_version.cache_clear()
