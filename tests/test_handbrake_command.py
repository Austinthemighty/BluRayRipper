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


def test_deinterlace_flag_adds_comb_detect_decomb():
    hb = HandBrake(_settings())
    off = hb.build_command(Path("i.mkv"), Path("o.mkv"), deinterlace=False)
    on = hb.build_command(Path("i.mkv"), Path("o.mkv"), deinterlace=True)
    assert "--decomb" not in off and "--comb-detect" not in off
    assert "--comb-detect" in on and "--decomb" in on


def test_the_discs_own_audio_is_copied_not_re_encoded(tmp_path):
    """Every 1080p and DVD encode in the library came out with each audio track
    as MP3 stereo at 160 kbps: Inside Out's seven tracks, House S08's AC-3 5.1.
    The preset says copy, but with tracks picked on the command line and no
    encoder named, HandBrakeCLI did not apply it. Run on the rip box: without
    these flags mp3 2ch 160k, with them ac3 6ch 448k."""
    cmd = HandBrake(_settings()).build_command(Path("in.mkv"), Path("out.mkv"))

    assert cmd[cmd.index("--aencoder") + 1] == "copy"
    mask = cmd[cmd.index("--audio-copy-mask") + 1].split(",")
    assert {"ac3", "eac3", "truehd", "dts", "dtshd"} <= set(mask)   # surround + lossless
    assert not any(m.startswith("copy:") for m in mask)             # the CLI's spelling
    assert cmd[cmd.index("--audio-fallback") + 1] == "av_aac"


def test_the_copy_list_and_fallback_come_from_the_preset(tmp_path):
    import json
    from blurayripper.tools.handbrake import preset_audio
    p = tmp_path / "presets.json"
    p.write_text(json.dumps({"PresetList": [{"PresetName": "Folder", "ChildrenArray": [
        {"PresetName": "Mine", "AudioCopyMask": ["copy:ac3", "copy:truehd"],
         "AudioEncoderFallback": "ac3"}]}]}))

    assert preset_audio(p, "Mine") == (["ac3", "truehd"], "ac3")
    # Unreadable or unknown: keep everything lossless rather than guess smaller.
    mask, fallback = preset_audio(tmp_path / "missing.json", "Mine")
    assert {"truehd", "dtshd", "flac"} <= set(mask) and fallback == "av_aac"
    assert preset_audio(p, "Not There")[1] == "av_aac"
