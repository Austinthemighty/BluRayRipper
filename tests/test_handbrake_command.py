"""Lock in the HandBrake command construction: preset import, language-ordered
audio selection, and the subtitle-none override that protects the SRT-sidecar
workflow from preset burn-in behavior."""

from pathlib import Path

from blurayripper.config import (
    AudioSettings,
    EncodeSettings,
    LanguageSettings,
    Settings,
    StorageSettings,
    SubtitleSettings,
    ToolPaths,
)
from blurayripper.tools.handbrake import HandBrake


def _settings(audio_tracks: str = "all") -> Settings:
    return Settings(
        tools=ToolPaths(handbrakecli="HandBrakeCLI"),
        storage=StorageSettings(staging_dir=Path("/tmp/s"), output_dir=Path("/tmp/o")),
        encode=EncodeSettings(
            container="mkv", preset_file=Path("config/presets.json"), preset_name="BluRay UHD"
        ),
        languages=LanguageSettings(keep=["eng", "pol"], default="eng"),
        subtitles=SubtitleSettings(),
        audio=AudioSettings(tracks=audio_tracks),
    )


def test_command_imports_preset_and_selects_it():
    cmd = HandBrake(_settings()).build_command(Path("in.mkv"), Path("out.mp4"))
    assert "--preset-import-file" in cmd
    assert cmd[cmd.index("-Z") + 1] == "BluRay UHD"


def test_default_language_is_first_in_audio_list():
    cmd = HandBrake(_settings()).build_command(Path("in.mkv"), Path("out.mkv"))
    lang_arg = cmd[cmd.index("--audio-lang-list") + 1]
    assert lang_arg == "eng,pol"  # default (eng) first so Plex flags it default


def test_audio_all_vs_main_mode():
    all_cmd = HandBrake(_settings("all")).build_command(Path("in.mkv"), Path("out.mkv"))
    main_cmd = HandBrake(_settings("main")).build_command(Path("in.mkv"), Path("out.mkv"))
    assert "--all-audio" in all_cmd and "--first-audio" not in all_cmd
    assert "--first-audio" in main_cmd and "--all-audio" not in main_cmd


def test_subtitles_are_disabled():
    cmd = HandBrake(_settings()).build_command(Path("in.mkv"), Path("out.mkv"))
    assert cmd[cmd.index("--subtitle") + 1] == "none"


def test_grain_tune_toggle():
    hb = HandBrake(_settings())
    off = hb.build_command(Path("i.mkv"), Path("o.mkv"), tune_grain=False)
    on = hb.build_command(Path("i.mkv"), Path("o.mkv"), tune_grain=True)
    assert "--encoder-tune" not in off
    assert on[on.index("--encoder-tune") + 1] == "grain"


def test_grain_default_from_settings():
    s = _settings()
    s.encode.tune_grain = True
    cmd = HandBrake(s).build_command(Path("i.mkv"), Path("o.mkv"))  # no explicit override
    assert "grain" in cmd


def test_clip_command_has_start_stop():
    cmd = HandBrake(_settings()).build_command(
        Path("i.mkv"), Path("o.mkv"), tune_grain=True, start=600, duration=4)
    assert cmd[cmd.index("--start-at") + 1] == "seconds:600"
    assert cmd[cmd.index("--stop-at") + 1] == "seconds:4"
