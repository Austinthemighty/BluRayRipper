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
