from blurayripper.config import LanguageSettings, load_settings


def test_language_keep_matching_is_case_insensitive():
    langs = LanguageSettings(keep=["eng", "pol"], default="eng")
    assert langs.is_kept("eng")
    assert langs.is_kept("POL")
    assert not langs.is_kept("fre")
    assert not langs.is_kept(None)


def test_load_default_settings():
    s = load_settings()  # reads repo config/settings.toml
    assert s.encode.container in ("mp4", "mkv")
    assert s.languages.default
    assert s.min_length_seconds > 0
    # Output roots derive from output_dir + subdirs.
    assert s.storage.movies_root.name == s.storage.movies_subdir
    assert s.storage.tv_root.name == s.storage.tv_subdir


def _write(p, text):
    p.write_text(text, encoding="utf-8")


def test_secrets_env_overrides_tracked_settings(tmp_path):
    """Paths in the gitignored secrets.env win over settings.toml so they
    survive a `git pull` that updates settings.toml."""
    _write(tmp_path / "settings.toml", """
[tools]
handbrakecli = "/from-toml/HandBrakeCLI"
[storage]
staging_dir = "/from-toml/staging"
""")
    # Backslash Windows path passes through verbatim (no TOML escaping in .env).
    _write(tmp_path / "secrets.env", "BLURAYRIPPER_HANDBRAKECLI=C:\\from-env\\HandBrakeCLI.exe\n"
                                     "BLURAYRIPPER_STAGING_DIR=/nas/staging\n")
    s = load_settings(tmp_path)
    assert s.tools.handbrakecli == "C:\\from-env\\HandBrakeCLI.exe"
    assert str(s.storage.staging_dir) == "/nas/staging"


def test_blank_env_value_falls_back_to_toml(tmp_path):
    _write(tmp_path / "settings.toml", '[tools]\nhandbrakecli = "C:/from-toml/HandBrakeCLI.exe"\n')
    _write(tmp_path / "secrets.env", "BLURAYRIPPER_HANDBRAKECLI=\n")
    s = load_settings(tmp_path)
    assert s.tools.handbrakecli == "C:/from-toml/HandBrakeCLI.exe"
