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


def test_use_workers_env_override(tmp_path):
    """use_workers can be set from the gitignored secrets.env (per-machine), and
    the string is parsed as a bool (so "false" is not truthy)."""
    _write(tmp_path / "settings.toml", "[cluster]\nuse_workers = false\n")
    _write(tmp_path / "secrets.env", "BLURAYRIPPER_USE_WORKERS=true\n")
    assert load_settings(tmp_path).cluster.use_workers is True

    _write(tmp_path / "secrets.env", "BLURAYRIPPER_USE_WORKERS=false\n")
    assert load_settings(tmp_path).cluster.use_workers is False


def test_cluster_bools_fall_back_to_toml_when_env_blank(tmp_path):
    _write(tmp_path / "settings.toml",
           "[cluster]\nuse_workers = true\nworker_does_subs = true\n")
    _write(tmp_path / "secrets.env", "BLURAYRIPPER_USE_WORKERS=\n")  # blank -> ignore
    s = load_settings(tmp_path)
    assert s.cluster.use_workers is True          # from toml
    assert s.cluster.worker_does_subs is True     # from toml (no env key at all)


def test_settings_local_overlay_overrides_toml(tmp_path):
    """The web-edited settings.local.json overlay wins over the tracked TOML."""
    _write(tmp_path / "settings.toml",
           '[encode]\nffmpeg_preset = "medium"\ncontainer = "mkv"\n')
    _write(tmp_path / "settings.local.json",
           '{"encode": {"ffmpeg_preset": "slower", "dovi": true}}')
    s = load_settings(tmp_path)
    assert s.encode.ffmpeg_preset == "slower"  # overlay wins
    assert s.encode.dovi is True               # overlay adds a key
    assert s.encode.container == "mkv"         # untouched TOML key preserved


def test_secrets_env_wins_over_overlay(tmp_path):
    """secrets.env / env vars still beat the web overlay, so the DB URL and other
    per-machine secrets stay authoritative."""
    _write(tmp_path / "settings.toml", "[cluster]\nuse_workers = false\n")
    _write(tmp_path / "settings.local.json", '{"cluster": {"use_workers": true}}')
    _write(tmp_path / "secrets.env", "BLURAYRIPPER_USE_WORKERS=false\n")
    assert load_settings(tmp_path).cluster.use_workers is False  # env beats overlay


def test_save_overlay_round_trips_and_ignores_secrets(tmp_path):
    from blurayripper.settings_store import save_overlay, settings_ui_payload
    _write(tmp_path / "settings.toml", '[encode]\nffmpeg_crf = 18\n')
    save_overlay(tmp_path, {
        "encode": {"ffmpeg_crf": "16", "ffmpeg_uhd": "true"},
        "languages": {"keep": "eng, pol"},
        "database": {"url": "postgres://SECRET"},  # not in schema -> dropped
        "bogus": {"nope": 1},                      # unknown section -> dropped
    })
    s = load_settings(tmp_path)
    assert s.encode.ffmpeg_crf == 16 and s.encode.ffmpeg_uhd is True
    assert s.languages.keep == ["eng", "pol"]
    assert s.database_url is None  # the secret never reached the overlay

    # A bad choice is rejected up front.
    import pytest
    with pytest.raises(ValueError):
        save_overlay(tmp_path, {"encode": {"ffmpeg_audio": "flac"}})

    # The UI payload reflects the merged (overlay) value.
    enc = {f["key"]: f for g in settings_ui_payload(tmp_path)["groups"]
           if g["title"] == "Encode" for f in g["fields"]}
    assert enc["ffmpeg_crf"]["value"] == 16


def test_ui_payload_flags_env_shadowed_fields(tmp_path):
    from blurayripper.settings_store import settings_ui_payload
    _write(tmp_path / "settings.toml", "")
    _write(tmp_path / "secrets.env", "BLURAYRIPPER_FFMPEG=/from/env/ffmpeg\n")
    tools = {f["key"]: f for g in settings_ui_payload(tmp_path)["groups"]
             if g["title"] == "Tools" for f in g["fields"]}
    assert tools["ffmpeg"]["env_value"] == "/from/env/ffmpeg"
    assert tools["mkvmerge"]["env_value"] is None
