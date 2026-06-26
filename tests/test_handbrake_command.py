"""Lock in the HandBrake command construction: preset import, language-ordered
audio selection, and the subtitle-none override that protects the SRT-sidecar
workflow from preset burn-in behavior."""

from pathlib import Path

from blurayripper.config import (
    EncodeSettings,
    LanguageSettings,
    Settings,
    StorageSettings,
    SubtitleSettings,
    ToolPaths,
)
from blurayripper.tools.handbrake import HandBrake


def _settings() -> Settings:
    return Settings(
        tools=ToolPaths(handbrakecli="HandBrakeCLI"),
        storage=StorageSettings(staging_dir=Path("/tmp/s"), output_dir=Path("/tmp/o")),
        encode=EncodeSettings(
            container="mp4", preset_file=Path("config/presets.json"), preset_name="BluRay UHD"
        ),
        languages=LanguageSettings(keep=["eng", "pol"], default="eng"),
        subtitles=SubtitleSettings(),
    )


def test_command_imports_preset_and_selects_it():
    cmd = HandBrake(_settings()).build_command(Path("in.mkv"), Path("out.mp4"))
    assert "--preset-import-file" in cmd
    assert cmd[cmd.index("-Z") + 1] == "BluRay UHD"


def test_default_language_is_first_in_audio_list():
    cmd = HandBrake(_settings()).build_command(Path("in.mkv"), Path("out.mp4"))
    lang_arg = cmd[cmd.index("--audio-lang-list") + 1]
    assert lang_arg == "eng,pol"  # default (eng) first so Plex flags it default
    assert "--all-audio" in cmd


def test_subtitles_are_disabled():
    cmd = HandBrake(_settings()).build_command(Path("in.mkv"), Path("out.mp4"))
    assert cmd[cmd.index("--subtitle") + 1] == "none"
