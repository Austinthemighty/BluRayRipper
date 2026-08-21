"""The subprocess-driving half of the three tool wrappers.

The pure parsers in these modules are pinned elsewhere (``test_ffmpeg_unit``,
``test_mkvtools``, ``test_makemkv_parser``, ``test_makemkv_mkvtools_unit``).
What is pinned here is the layer around them: the argv each wrapper hands the
process runner, the progress and log lines it forwards back to the reporter,
and — the reason this file exists — what it does when the tool exits 0 having
written nothing at all.

That last case is not hypothetical. makemkvcon exits 0 on a disc it gave up
reading, ffmpeg exits 0 after failing to open its output on a full staging
drive, mkvmerge exits 0 writing a header-only file. A wrapper that returns
happily from any of those hands the pipeline a path to nothing, and the job goes
green with no video in the library.

Everything here stubs ``tools.process.run``: no real tool is spawned, no disc is
touched, nothing sleeps.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from blurayripper.config import (
    AudioSettings,
    EncodeSettings,
    LanguageSettings,
    Settings,
    StorageSettings,
    SubtitleSettings,
    ToolNotFoundError,
    ToolPaths,
)
from blurayripper.models import StreamType
from blurayripper.services.hdr import HdrInfo
from blurayripper.tools import ffmpeg as ffmpeg_mod
from blurayripper.tools import makemkv as makemkv_mod
from blurayripper.tools import mkvtools as mkvtools_mod
from blurayripper.tools import process as process_mod
from blurayripper.tools.ffmpeg import FFmpeg
from blurayripper.tools.makemkv import MakeMKV, parse_info
from blurayripper.tools.mkvtools import MkvTools


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class FakeProcess:
    """Stand-in for ``blurayripper.tools.process.run``.

    Faithful to the real runner in the ways the wrappers depend on: every stdout
    line is streamed through ``on_line`` (sync or async) *and* collected into
    ``ProcessResult.stdout_lines``, and a non-zero exit raises ``ProcessError``
    unless the caller passed ``check=False``.

    ``respond(cmd)`` returns the stdout lines for a spawn — optionally as
    ``(lines, returncode)`` — and is where a test puts the side effect the real
    tool would have had (writing, or conspicuously not writing, its output).
    """

    def __init__(self, respond=None) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self._respond = respond or (lambda cmd: [])

    @property
    def cmd(self) -> list[str]:
        assert len(self.calls) == 1, f"expected one spawn, got {len(self.calls)}"
        return self.calls[0]

    def spawn(self, containing: str) -> tuple[list[str], dict]:
        """The one spawn whose argv contains ``containing`` (a run can spawn a
        probe as well as the tool itself), with the kwargs it was given."""
        hits = [(c, k) for c, k in zip(self.calls, self.kwargs) if containing in c]
        assert len(hits) == 1, f"{containing!r} matched {len(hits)} of {self.calls}"
        return hits[0]

    async def __call__(self, cmd, *, on_line=None, cwd=None, check=True,
                       low_priority=False, timeout=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        self.kwargs.append({"on_line": on_line, "cwd": cwd, "check": check,
                            "low_priority": low_priority, "timeout": timeout})
        out = self._respond(cmd)
        lines, returncode = out if isinstance(out, tuple) else (out, 0)
        lines = list(lines)
        for line in lines:
            if on_line is not None:
                res = on_line(line)
                if inspect.isawaitable(res):
                    await res
        result = process_mod.ProcessResult(
            returncode=returncode, stdout_lines=lines, stderr="")
        if check and returncode != 0:
            raise process_mod.ProcessError(cmd, result)
        return result


def _install(monkeypatch, fake: FakeProcess) -> FakeProcess:
    """Point every wrapper's process layer at ``fake`` (they share the module)."""
    monkeypatch.setattr(process_mod, "run", fake)
    return fake


def _bound_timeouts(monkeypatch, module) -> list:
    """Record the timeout each ``asyncio.wait_for`` in ``module`` is given.

    Replaces only the module's own ``asyncio`` name, so the real event loop the
    test itself runs on is untouched.
    """
    seen: list = []

    async def fake_wait_for(awaitable, timeout=None):
        seen.append(timeout)
        return await awaitable

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(wait_for=fake_wait_for))
    return seen


def _settings(tmp_path: Path, **tools) -> Settings:
    return Settings(
        tools=ToolPaths(**tools),
        storage=StorageSettings(staging_dir=tmp_path / "stage",
                                output_dir=tmp_path / "out"),
        encode=EncodeSettings(),
        languages=LanguageSettings(),
        subtitles=SubtitleSettings(),
        audio=AudioSettings(),
    )


def _val(cmd: list[str], flag: str) -> str:
    """The argv token immediately following ``flag``."""
    return cmd[cmd.index(flag) + 1]


# --------------------------------------------------------------------------
# ffmpeg: ffprobe resolution
# --------------------------------------------------------------------------

def _ff(tmp_path: Path, ffmpeg: str | None = "/usr/bin/ffmpeg", **encode) -> FFmpeg:
    s = _settings(tmp_path, ffmpeg=ffmpeg, mkvmerge="/usr/bin/mkvmerge",
                  mkvextract="/usr/bin/mkvextract")
    for key, value in encode.items():
        setattr(s.encode, key, value)
    return FFmpeg(s)


def test_ffprobe_is_taken_from_beside_the_configured_ffmpeg(tmp_path, monkeypatch):
    """The boxes run ffmpeg out of an unpacked build directory that is not on
    PATH. Resolving ffprobe from PATH first would pick up a different (often
    much older) system build than the ffmpeg doing the encoding, and the HDR
    side-data it reports is what the encode's colour flags are built from."""
    (tmp_path / "ffmpeg").write_text("#!/bin/sh\n")
    (tmp_path / "ffprobe").write_text("#!/bin/sh\n")
    monkeypatch.setattr(ffmpeg_mod.shutil, "which",
                        lambda p: "/usr/bin/ffprobe-from-path")

    assert _ff(tmp_path, str(tmp_path / "ffmpeg"))._ffprobe_bin() == str(
        tmp_path / "ffprobe")


def test_only_the_filename_is_rewritten_never_the_directory(tmp_path, monkeypatch):
    """The Windows box keeps the tools under a folder with 'ffmpeg' in its name
    (…\\ffmpeg-7.1\\bin\\ffmpeg.exe). Rewriting the whole path would look for
    ffprobe under a directory that does not exist, silently demoting every probe
    to whatever PATH holds."""
    binaries = tmp_path / "ffmpeg-7.1" / "bin"
    binaries.mkdir(parents=True)
    (binaries / "ffmpeg.exe").write_text("")
    (binaries / "ffprobe.exe").write_text("")
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda p: None)

    resolved = _ff(tmp_path, str(binaries / "ffmpeg.exe"))._ffprobe_bin()

    assert resolved == str(binaries / "ffprobe.exe")
    assert "ffmpeg-7.1" in resolved      # the directory survived the rewrite


def test_a_build_without_a_sibling_ffprobe_falls_back_to_the_one_on_path(
        tmp_path, monkeypatch):
    """A static ffmpeg dropped in on its own is common. Probing has to keep
    working from PATH — probe_video failing is a hard stop on the UHD path, not
    a degraded encode."""
    (tmp_path / "ffmpeg").write_text("#!/bin/sh\n")     # no ffprobe beside it
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda p: "/usr/bin/ffprobe")

    assert _ff(tmp_path, str(tmp_path / "ffmpeg"))._ffprobe_bin() == "/usr/bin/ffprobe"


def test_with_no_ffprobe_anywhere_the_expected_path_is_still_named(
        tmp_path, monkeypatch):
    """When nothing can be found the spawn fails either way; returning the
    derived sibling path means the FileNotFoundError names where ffprobe was
    expected, instead of an empty argv[0] nobody can act on."""
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda p: None)

    assert _ff(tmp_path, str(tmp_path / "ffmpeg"))._ffprobe_bin() == str(
        tmp_path / "ffprobe")


def test_a_binary_not_called_ffmpeg_resolves_ffprobe_to_itself(tmp_path, monkeypatch):
    """Documenting a sharp edge, not endorsing it: the sibling is derived by
    replacing 'ffmpeg' in the FILENAME, so a binary named anything else (a
    wrapper script, avconv, ffmpeg7 renamed) yields the ffmpeg path unchanged —
    which exists — and that binary is then run with ffprobe's arguments. The
    PATH fallback never gets a chance. probe_video does at least fail loudly
    (ffmpeg rejects -print_format, stdout is not JSON) rather than reporting the
    file as SDR; see this file's notes."""
    wrapper = tmp_path / "encode-tool"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda p: "/usr/bin/ffprobe")

    assert _ff(tmp_path, str(wrapper))._ffprobe_bin() == str(wrapper)


def test_probing_without_a_configured_ffmpeg_names_the_missing_tool(tmp_path):
    """A worker provisioned without ffmpeg must say so by name. Deriving a probe
    path from an empty setting would spawn argv[0]='ffprobe' and fail as an
    unattributable FileNotFoundError."""
    with pytest.raises(ToolNotFoundError):
        _ff(tmp_path, None)._ffprobe_bin()


# --------------------------------------------------------------------------
# ffmpeg: probe_video
# --------------------------------------------------------------------------

PROBE_JSON = json.dumps({
    "format": {"duration": "6821.416667"},
    "streams": [{
        "codec_type": "video",
        "color_primaries": "bt2020",
        "color_transfer": "smpte2084",
        "color_space": "bt2020nc",
    }],
    "frames": [{"side_data_list": [
        {"side_data_type": "Mastering display metadata",
         "green_x": "13250/50000", "green_y": "34500/50000",
         "blue_x": "7500/50000", "blue_y": "3000/50000",
         "red_x": "34000/50000", "red_y": "16000/50000",
         "white_point_x": "15635/50000", "white_point_y": "16450/50000",
         "max_luminance": "10000000/10000", "min_luminance": "50/10000"},
        {"side_data_type": "Content light level metadata",
         "max_content": 1000, "max_average": 400},
    ]}],
})


def _probe_responder(payload: str, *, returncode: int = 0):
    def respond(cmd):
        assert "-print_format" in cmd, f"not an ffprobe spawn: {cmd}"
        return payload.splitlines(), returncode
    return respond


async def test_a_probe_reports_both_the_hdr_signalling_and_the_runtime(
        tmp_path, monkeypatch):
    """These two values steer the whole UHD path: the HDR block becomes the
    encode's colour flags and x265 params, and the duration is the denominator
    of every progress fraction the job reports for the next several hours."""
    fake = _install(monkeypatch, FakeProcess(_probe_responder(PROBE_JSON)))

    info, duration = await _ff(tmp_path).probe_video(Path("/stage/t00.mkv"))

    assert info.transfer == "smpte2084" and info.colorprim == "bt2020"
    assert info.max_cll == "1000,400"
    assert info.master_display.startswith("G(13250,34500)")
    assert duration == pytest.approx(6821.416667)
    assert fake.cmd[-1] == "/stage/t00.mkv"


async def test_a_file_with_no_reported_duration_probes_as_unknown_not_a_crash(
        tmp_path, monkeypatch):
    """Raw streams and some remuxes report no format duration at all. The HDR
    half of the answer is the half that must not be lost — dying here would fail
    a UHD encode outright over a missing progress denominator."""
    payload = json.dumps({
        "format": {},
        "streams": [{"codec_type": "video", "color_transfer": "smpte2084"}],
    })
    _install(monkeypatch, FakeProcess(_probe_responder(payload)))

    info, duration = await _ff(tmp_path).probe_video(Path("/stage/t00.mkv"))

    assert duration == 0.0
    assert info.transfer == "smpte2084"


async def test_a_non_numeric_duration_reads_as_unknown(tmp_path, monkeypatch):
    """ffprobe prints "N/A" for a duration it cannot work out. Letting the
    ValueError escape would turn an encodable file into a failed job."""
    payload = json.dumps({
        "format": {"duration": "N/A"},
        "streams": [{"codec_type": "video", "color_transfer": "smpte2084"}],
    })
    _install(monkeypatch, FakeProcess(_probe_responder(payload)))

    assert (await _ff(tmp_path).probe_video(Path("/stage/t00.mkv")))[1] == 0.0


async def test_a_noisy_ffprobe_exit_code_does_not_discard_usable_metadata(
        tmp_path, monkeypatch):
    """ffprobe exits non-zero over trailing decode complaints on a disc rip that
    it nonetheless described completely. Letting the runner raise on that would
    fail the encode with the answer already in hand — so this call opts out of
    the exit-code check and judges the JSON instead."""
    fake = _install(monkeypatch,
                    FakeProcess(_probe_responder(PROBE_JSON, returncode=1)))

    info, duration = await _ff(tmp_path).probe_video(Path("/stage/t00.mkv"))

    assert info.transfer == "smpte2084"
    assert duration == pytest.approx(6821.416667)
    assert fake.kwargs[0]["check"] is False


async def test_unreadable_probe_output_stops_the_job_rather_than_assuming_sdr(
        tmp_path, monkeypatch):
    """An empty or garbled probe must never be read as "this file is SDR": that
    is indistinguishable from a real SDR source, so the encode would drop HDR10
    and the output check would pass vacuously. Hours of encoding, silently
    wrong. The error has to name ffprobe so the fix is obvious."""
    _install(monkeypatch, FakeProcess(lambda cmd: ["ffprobe: command not found"]))

    with pytest.raises(RuntimeError, match="ffprobe"):
        await _ff(tmp_path).probe_video(Path("/stage/t00.mkv"))


# --------------------------------------------------------------------------
# ffmpeg: encode  (the UHD/HDR path)
# --------------------------------------------------------------------------

def _encode_responder(dst: Path, *, writes: bytes | None, progress=()):
    """ffprobe answers with HDR10 + a runtime; the encode spawn writes ``writes``
    (or, when None, exits 0 having written nothing at all)."""
    def respond(cmd):
        if "-print_format" in cmd:
            return PROBE_JSON.splitlines()
        if writes is not None:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(writes)
        return list(progress)
    return respond


async def test_an_encode_that_wrote_nothing_fails_instead_of_reporting_success(
        tmp_path, monkeypatch):
    """ffmpeg exits 0 when it never managed to open its output (a full staging
    drive, a dropped mapped drive, a path it cannot create). The caller places
    whatever this returns into the Plex library, so a wrapper that returned the
    path regardless would finish the job green with no file behind it.

    The control arm is the same stubbed run differing in one thing: the file."""
    dst = tmp_path / "encode" / "Movie (2024).mkv"

    _install(monkeypatch, FakeProcess(_encode_responder(dst, writes=b"\x1aE\xdf\xa3")))
    assert await _ff(tmp_path).encode(Path("/stage/t00.mkv"), dst) == dst

    dst.unlink()
    _install(monkeypatch, FakeProcess(_encode_responder(dst, writes=None)))
    with pytest.raises(RuntimeError) as raised:
        await _ff(tmp_path).encode(Path("/stage/t00.mkv"), dst)
    assert str(dst) in str(raised.value)      # names the file that never appeared


async def test_a_zero_byte_encode_is_rejected_as_firmly_as_a_missing_one(
        tmp_path, monkeypatch):
    """An out-of-space or killed ffmpeg leaves the output created but empty.
    Existence alone is the weaker test, and the empty file would move into the
    library and be checkpointed as done — the job can then never be retried into
    a good state without deleting it by hand."""
    dst = tmp_path / "encode" / "Movie (2024).mkv"
    _install(monkeypatch, FakeProcess(_encode_responder(dst, writes=b"")))

    with pytest.raises(RuntimeError, match="no output"):
        await _ff(tmp_path).encode(Path("/stage/t00.mkv"), dst)


async def test_the_encode_directory_exists_before_ffmpeg_is_spawned(
        tmp_path, monkeypatch):
    """ffmpeg does not create the output's parent — it exits immediately with
    "No such file or directory". The staging encode/ folder is fresh for every
    job, so this is the normal case, not an edge one."""
    dst = tmp_path / "staging" / "job7" / "encode" / "Movie (2024).mkv"
    seen = {}

    def respond(cmd):
        if "-print_format" in cmd:
            return PROBE_JSON.splitlines()
        seen["parent_existed"] = dst.parent.is_dir()
        dst.write_bytes(b"x")
        return []

    _install(monkeypatch, FakeProcess(respond))

    await _ff(tmp_path).encode(Path("/stage/t00.mkv"), dst)

    assert seen["parent_existed"] is True


async def test_the_encode_is_built_from_what_the_probe_found_and_what_is_configured(
        tmp_path, monkeypatch):
    """The command builder is unit-tested on its own; what is pinned here is the
    wiring into it. If the probed HDR block never reached the builder, or the
    quality settings were read from anywhere but this job's config, a UHD disc
    would encode with default settings and no HDR10 signalling — and nothing
    about the run would look wrong."""
    dst = tmp_path / "encode" / "Movie (2024).mkv"
    fake = _install(monkeypatch,
                    FakeProcess(_encode_responder(dst, writes=b"x")))
    ff = _ff(tmp_path, ffmpeg_crf=17, ffmpeg_preset="slower", ffmpeg_audio="aac")

    await ff.encode(Path("/stage/t00.mkv"), dst, tune_grain=True)

    probe_cmd, encode_cmd = fake.calls
    assert "-print_format" in probe_cmd
    assert _val(encode_cmd, "-crf") == "17"
    assert _val(encode_cmd, "-preset") == "slower"
    assert _val(encode_cmd, "-c:a") == "aac"
    assert _val(encode_cmd, "-color_trc") == "smpte2084"     # from the probe
    assert "max-cll=1000,400" in _val(encode_cmd, "-x265-params")
    assert "tune=grain" in _val(encode_cmd, "-x265-params")   # from the argument
    assert encode_cmd[-1] == str(dst)


async def test_encode_progress_is_reported_against_the_probed_runtime(
        tmp_path, monkeypatch):
    """The two halves have to meet: the duration ffprobe reported becomes the
    denominator for the -progress lines ffmpeg streams back. Wire them up wrong
    (a fresh handler per line, a zero duration, the wrong stream) and a six-hour
    UHD encode sits at 0% or jumps straight to nearly-done."""
    dst = tmp_path / "encode" / "Movie (2024).mkv"
    payload = json.dumps({"format": {"duration": "3600"},
                          "streams": [{"codec_type": "video"}]})

    def respond(cmd):
        if "-print_format" in cmd:
            return payload.splitlines()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"x")
        return ["fps=23.98", "out_time=00:15:00.000000", "speed=2.0x",
                "progress=continue", "progress=end"]

    _install(monkeypatch, FakeProcess(respond))
    events: list = []

    await _ff(tmp_path).encode(Path("/stage/t00.mkv"), dst, on_progress=events.append)

    assert [e.state for e in events] == ["working", "done"]
    assert events[0].fraction == 0.25          # 900s of the probed 3600s
    assert events[0].eta_seconds == 1350       # 2700s left at 2x
    assert events[0].fps == 23.98
    assert events[1].fraction == 1.0


async def test_encodes_run_below_normal_priority_unless_told_otherwise(
        tmp_path, monkeypatch):
    """A serve box rips while it encodes. At normal priority libx265 saturates
    every core and starves makemkvcon, which shows up as a rip that crawls and a
    UI that stops responding — so the flag has to reach the spawn, and has to
    still be overridable for a box that only encodes."""
    dst = tmp_path / "encode" / "Movie (2024).mkv"

    fake = _install(monkeypatch, FakeProcess(_encode_responder(dst, writes=b"x")))
    await _ff(tmp_path).encode(Path("/stage/t00.mkv"), dst)
    assert fake.spawn("-x265-params")[1]["low_priority"] is True

    fake = _install(monkeypatch, FakeProcess(_encode_responder(dst, writes=b"x")))
    await _ff(tmp_path).encode(Path("/stage/t00.mkv"), dst, low_priority=False)
    assert fake.spawn("-x265-params")[1]["low_priority"] is False


# --------------------------------------------------------------------------
# ffmpeg: encode_hevc / extract_hevc / extract_frame
# --------------------------------------------------------------------------

HDR10 = HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc")


async def test_the_dolby_vision_base_layer_must_actually_be_written(
        tmp_path, monkeypatch):
    """This raw stream is what dovi_tool injects the RPU into and what is then
    muxed to the library file. A missing or empty one has to stop the DV attempt
    here, where the pipeline can still fall back to a plain HDR10 encode."""
    out = tmp_path / "dovi" / "enc.hevc"

    def responder(writes):
        def respond(cmd):
            if writes is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(writes)
            return []
        return respond

    _install(monkeypatch, FakeProcess(responder(b"\x00\x00\x01\x40")))
    assert await _ff(tmp_path).encode_hevc(
        Path("/stage/t00.mkv"), out, hdr=HDR10) == out

    out.unlink()
    _install(monkeypatch, FakeProcess(responder(None)))
    with pytest.raises(RuntimeError, match="HEVC"):
        await _ff(tmp_path).encode_hevc(Path("/stage/t00.mkv"), out, hdr=HDR10)

    _install(monkeypatch, FakeProcess(responder(b"")))
    with pytest.raises(RuntimeError, match="HEVC"):
        await _ff(tmp_path).encode_hevc(Path("/stage/t00.mkv"), out, hdr=HDR10)


async def test_the_dolby_vision_pass_re_probes_nothing_and_encodes_video_only(
        tmp_path, monkeypatch):
    """The DV path probed the source already and hands the result down. Probing
    again would re-read a 60GB remux off the mapped drive for metadata already
    in hand — and worse, the two probes could disagree about the HDR block that
    the base layer and the injected RPU both have to describe."""
    out = tmp_path / "dovi" / "enc.hevc"

    def respond(cmd):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        return []

    fake = _install(monkeypatch, FakeProcess(respond))

    await _ff(tmp_path, ffmpeg_crf=16).encode_hevc(
        Path("/stage/t00.mkv"), out, hdr=HDR10)

    assert len(fake.calls) == 1                     # no ffprobe spawn
    assert _val(fake.cmd, "-f") == "hevc"
    assert _val(fake.cmd, "-crf") == "16"
    assert _val(fake.cmd, "-color_trc") == "smpte2084"
    assert "-an" in fake.cmd


async def test_the_dolby_vision_pass_reports_progress_against_the_given_runtime(
        tmp_path, monkeypatch):
    """The caller passes the runtime it already probed. Ignoring it would leave
    the longest single step in the pipeline with no progress at all, which is
    exactly when someone kills a healthy job thinking it has hung."""
    out = tmp_path / "dovi" / "enc.hevc"

    def respond(cmd):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        return ["out_time=00:30:00.000000", "progress=continue"]

    _install(monkeypatch, FakeProcess(respond))
    events: list = []

    await _ff(tmp_path).encode_hevc(Path("/stage/t00.mkv"), out, hdr=HDR10,
                                    duration=3600.0, on_progress=events.append)

    assert [e.fraction for e in events] == [0.5]


async def test_the_dolby_vision_encode_yields_to_ripping_like_any_other(
        tmp_path, monkeypatch):
    """The DV re-encode is the heaviest thing this software runs, and it runs on
    a box that is often ripping the next disc at the same time. The caller
    passes that box's own setting down, so it has to be honoured in both
    directions rather than being fixed at either end."""
    out = tmp_path / "dovi" / "enc.hevc"

    def respond(cmd):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        return []

    fake = _install(monkeypatch, FakeProcess(respond))
    await _ff(tmp_path).encode_hevc(Path("/stage/t00.mkv"), out, hdr=HDR10)
    assert fake.kwargs[0]["low_priority"] is True

    fake = _install(monkeypatch, FakeProcess(respond))
    await _ff(tmp_path).encode_hevc(Path("/stage/t00.mkv"), out, hdr=HDR10,
                                    low_priority=False)
    assert fake.kwargs[0]["low_priority"] is False


async def test_extracting_the_source_video_stream_never_re_encodes_it(
        tmp_path, monkeypatch):
    """This copy exists only so dovi_tool can read the RPU out of the original
    bitstream. Re-encoding here would destroy the very layer being extracted —
    and would take hours instead of minutes."""
    out = tmp_path / "dovi" / "src.hevc"
    seen = {}

    def respond(cmd):
        seen["parent_existed"] = out.parent.is_dir()
        return []

    fake = _install(monkeypatch, FakeProcess(respond))

    assert await _ff(tmp_path).extract_hevc(Path("/stage/t00.mkv"), out) == out

    assert seen["parent_existed"] is True
    assert _val(fake.cmd, "-c:v") == "copy"
    assert _val(fake.cmd, "-map") == "0:v:0"
    assert _val(fake.cmd, "-f") == "hevc"          # raw Annex-B, not a container
    assert "-y" in fake.cmd and fake.cmd[-1] == str(out)
    assert _val(fake.cmd, "-i") == "/stage/t00.mkv"


async def test_a_poster_frame_is_seek_first_single_frame_and_overwritable(
        tmp_path, monkeypatch):
    """Three separate ways this goes wrong on a real ffmpeg 7 build: -ss after
    -i decodes the whole clip up to the offset instead of seeking (slow enough
    to look hung), a missing -frames:v writes every frame of the sample, and
    without -update ffmpeg refuses a fixed output filename outright. Without -y
    a retried preview stops on an interactive overwrite prompt."""
    png = tmp_path / "preview" / "grain_off.png"
    seen = {}

    def respond(cmd):
        seen["parent_existed"] = png.parent.is_dir()
        return []

    fake = _install(monkeypatch, FakeProcess(respond))

    assert await _ff(tmp_path).extract_frame(
        Path("/stage/off.mp4"), png, at_seconds=2.0) == png

    assert seen["parent_existed"] is True
    assert fake.cmd.index("-ss") < fake.cmd.index("-i")
    assert _val(fake.cmd, "-ss") == "2.00"
    assert _val(fake.cmd, "-frames:v") == "1"
    assert _val(fake.cmd, "-update") == "1"
    assert "-y" in fake.cmd
    assert fake.cmd[-1] == str(png)


async def test_every_ffmpeg_argument_is_a_string(tmp_path, monkeypatch):
    """The offset is a float and the paths are Path objects. One unconverted
    value makes create_subprocess_exec raise before ffmpeg ever starts, which
    reads as a mysterious tooling failure rather than a bug."""
    png = tmp_path / "preview" / "grain_off.png"
    fake = _install(monkeypatch, FakeProcess())

    await _ff(tmp_path).extract_frame(Path("/stage/off.mp4"), png, at_seconds=61.5)

    assert all(isinstance(a, str) for a in fake.cmd), fake.cmd


# --------------------------------------------------------------------------
# mkvtools: the mkvmerge -J probes
# --------------------------------------------------------------------------

MKV_JSON = json.dumps({
    "container": {"properties": {"duration": 6700_000_000_000}},
    "tracks": [
        {"id": 0, "type": "video",
         "properties": {"pixel_dimensions": "3840x2160"}},
        {"id": 1, "type": "audio", "properties": {"language": "eng"}},
        {"id": 2, "type": "subtitles", "codec": "HDMV PGS",
         "properties": {"language": "eng"}},
        {"id": 3, "type": "subtitles", "codec": "HDMV PGS",
         "properties": {"language": "fre", "forced_track": True}},
    ],
})


def _mkv(tmp_path: Path) -> MkvTools:
    return MkvTools(_settings(tmp_path, mkvmerge="/usr/bin/mkvmerge",
                              mkvextract="/usr/bin/mkvextract"))


async def test_a_probe_asks_mkvmerge_for_json_and_returns_the_whole_summary(
        tmp_path, monkeypatch):
    """One spawn answers four questions the pipeline asks at different moments —
    runtime (the truncated-rip guard), height (which encoder), audio languages
    and subtitle tracks (which to OCR). Asking mkvmerge for its human-readable
    listing instead of -J would parse into nothing at all."""
    fake = _install(monkeypatch, FakeProcess(lambda cmd: MKV_JSON.splitlines()))

    probed = await _mkv(tmp_path).probe(Path("/z/rip/title00.mkv"))

    assert fake.cmd == ["/usr/bin/mkvmerge", "-J", "/z/rip/title00.mkv"]
    assert probed.duration_seconds == 6700
    assert probed.video_height == 2160
    assert probed.audio_languages == ["eng"]
    assert [t.track_id for t in probed.subtitles] == [2, 3]


async def test_identify_subtitles_returns_the_subtitle_tracks_of_that_file(
        tmp_path, monkeypatch):
    """This list is what the subtitle stage iterates: every PGS track it names
    gets extracted and OCR'd, and any it misses ships without a sidecar."""
    fake = _install(monkeypatch, FakeProcess(lambda cmd: MKV_JSON.splitlines()))

    tracks = await _mkv(tmp_path).identify_subtitles(Path("/z/rip/title00.mkv"))

    assert [(t.track_id, t.language, t.is_pgs) for t in tracks] == [
        (2, "eng", True), (3, "fre", True)]
    assert fake.cmd[-1] == "/z/rip/title00.mkv"
    assert "-J" in fake.cmd


async def test_video_height_reads_the_height_from_the_same_probe(
        tmp_path, monkeypatch):
    """Height alone decides ffmpeg-vs-HandBrake, and getting it wrong on a UHD
    title means HandBrake silently drops HDR10 over a multi-hour encode."""
    fake = _install(monkeypatch, FakeProcess(lambda cmd: MKV_JSON.splitlines()))

    assert await _mkv(tmp_path).video_height(Path("/z/rip/title00.mkv")) == 2160
    assert "-J" in fake.cmd


async def test_a_metadata_probe_cannot_hang_a_job_indefinitely(
        tmp_path, monkeypatch):
    """mkvmerge reading a file over the mapped Z: drive blocks forever when the
    NAS handle goes stale — no error, no exit. Reading a header is seconds of
    work, so it is bounded; without the bound the job sits in RIPPING until
    someone notices days later."""
    seen = _bound_timeouts(monkeypatch, mkvtools_mod)
    _install(monkeypatch, FakeProcess(lambda cmd: MKV_JSON.splitlines()))
    tools = _mkv(tmp_path)

    await tools.probe(Path("/z/rip/title00.mkv"))
    await tools.identify_subtitles(Path("/z/rip/title00.mkv"))
    await tools.video_height(Path("/z/rip/title00.mkv"))

    assert len(seen) == 3
    assert all(t is not None and 0 < t <= 300 for t in seen), seen


# --------------------------------------------------------------------------
# mkvtools: mkvextract
# --------------------------------------------------------------------------

async def test_several_tracks_are_extracted_in_a_single_pass(tmp_path, monkeypatch):
    """mkvextract reads the source once and writes every track named on the
    command line. Looping one track per spawn would re-read a 60GB remux across
    the network for each subtitle language on the disc."""
    a, b = tmp_path / "subs" / "2.sup", tmp_path / "subs" / "3.sup"
    fake = _install(monkeypatch, FakeProcess())

    await _mkv(tmp_path).extract_tracks(Path("/z/rip/t00.mkv"), [(2, a), (3, b)])

    assert len(fake.calls) == 1
    assert fake.cmd == ["/usr/bin/mkvextract", "tracks", "/z/rip/t00.mkv",
                        f"2:{a}", f"3:{b}"]


async def test_extraction_targets_have_their_directories_made_first(
        tmp_path, monkeypatch):
    """mkvextract will not create a directory; it fails the whole pass on the
    first target whose parent is missing. The subs/ folder is fresh per job, and
    tracks can be aimed at different folders in the same pass."""
    a, b = tmp_path / "subs" / "eng" / "2.sup", tmp_path / "subs" / "fre" / "3.sup"
    seen = {}

    def respond(cmd):
        seen["ready"] = a.parent.is_dir() and b.parent.is_dir()
        return []

    _install(monkeypatch, FakeProcess(respond))

    await _mkv(tmp_path).extract_tracks(Path("/z/rip/t00.mkv"), [(2, a), (3, b)])

    assert seen["ready"] is True


async def test_nothing_to_extract_means_no_mkvextract_run_at_all(
        tmp_path, monkeypatch):
    """A title with no PGS tracks reaches here with an empty list. Spawning
    mkvextract with a source and no track specs makes it print usage and exit
    non-zero, failing a job that had nothing to do."""
    def explode(cmd):
        raise AssertionError(f"mkvextract must not be spawned: {cmd}")

    _install(monkeypatch, FakeProcess(explode))

    await _mkv(tmp_path).extract_tracks(Path("/z/rip/t00.mkv"), [])


async def test_extraction_progress_is_forwarded_as_a_fraction(
        tmp_path, monkeypatch):
    """Extracting the PGS from a UHD remux is minutes of apparent silence. The
    percentage mkvextract prints is the only sign it is alive, and the UI wants
    a 0..1 fraction, not the integer percent."""
    out = tmp_path / "subs" / "2.sup"
    lines = ["Progress: 0%", "The file is being analyzed.",
             "Progress: 45%", "Progress: 100%"]
    _install(monkeypatch, FakeProcess(lambda cmd: lines))
    seen: list[float] = []

    await _mkv(tmp_path).extract_tracks(
        Path("/z/rip/t00.mkv"), [(2, out)], on_progress=seen.append)

    assert seen == [0.0, 0.45, 1.0]


async def test_extraction_progress_is_harmless_when_nobody_is_listening(
        tmp_path, monkeypatch):
    """The subtitle-audit and single-track callers pass no callback. An
    unguarded call would raise TypeError inside the stdout pump on the first
    percentage mkvextract prints, killing an extraction that was working."""
    out = tmp_path / "subs" / "2.sup"
    _install(monkeypatch, FakeProcess(lambda cmd: ["Progress: 45%"]))

    await _mkv(tmp_path).extract_tracks(Path("/z/rip/t00.mkv"), [(2, out)])


async def test_a_stuck_extraction_is_bounded_rather_than_endless(
        tmp_path, monkeypatch):
    """Same stale-handle failure as the probes, but on the long-running side: an
    extraction that stops making progress must eventually fail the job instead
    of holding a worker slot forever."""
    seen = _bound_timeouts(monkeypatch, mkvtools_mod)
    _install(monkeypatch, FakeProcess())

    await _mkv(tmp_path).extract_tracks(
        Path("/z/rip/t00.mkv"), [(2, tmp_path / "2.sup")])

    assert len(seen) == 1
    assert seen[0] is not None and 0 < seen[0] <= 7200


async def test_a_single_track_extraction_is_the_batch_with_one_spec(
        tmp_path, monkeypatch):
    """Two code paths that build mkvextract argv differently would drift; the
    single-track helper is where a "3:path" spec is easiest to get wrong (a
    space, a missing id, the pair reversed) and mkvextract would then treat the
    path as another track spec."""
    out = tmp_path / "subs" / "3.sup"
    fake = _install(monkeypatch, FakeProcess())

    assert await _mkv(tmp_path).extract_track(
        Path("/z/rip/t00.mkv"), 3, out) == out

    assert len(fake.calls) == 1
    assert fake.cmd[-1] == f"3:{out}"


# --------------------------------------------------------------------------
# mkvtools: mux_dovi
# --------------------------------------------------------------------------

async def test_the_dolby_vision_mux_takes_video_from_one_input_and_audio_from_the_other(
        tmp_path, monkeypatch):
    """mkvmerge applies each --no-* option to the input that FOLLOWS it. With
    the flags in the wrong place the original rip contributes its video too, and
    the output carries two video tracks — the second being the HDR10 one without
    the injected RPU, which is what Plex may well pick."""
    out = tmp_path / "encode" / "Movie (2024).mkv"
    seen = {}

    def respond(cmd):
        seen["parent_existed"] = out.parent.is_dir()
        return []

    fake = _install(monkeypatch, FakeProcess(respond))

    assert await _mkv(tmp_path).mux_dovi(
        Path("/stage/enc_dv.hevc"), Path("/stage/t00.mkv"), out) == out

    assert seen["parent_existed"] is True
    assert _val(fake.cmd, "-o") == str(out)
    video, audio = fake.cmd.index("/stage/enc_dv.hevc"), fake.cmd.index("/stage/t00.mkv")
    assert video < fake.cmd.index("--no-video") < audio
    assert video < fake.cmd.index("--no-subtitles") < audio


async def test_the_dolby_vision_mux_is_bounded_too(tmp_path, monkeypatch):
    """Remuxing a UHD title is a long copy over the mapped drive — generously
    bounded, but bounded, for the same stale-handle reason as the extraction."""
    seen = _bound_timeouts(monkeypatch, mkvtools_mod)
    _install(monkeypatch, FakeProcess())

    await _mkv(tmp_path).mux_dovi(Path("/stage/enc_dv.hevc"),
                                  Path("/stage/t00.mkv"),
                                  tmp_path / "encode" / "Movie.mkv")

    assert len(seen) == 1
    assert seen[0] is not None and 0 < seen[0] <= 86400


def test_an_unreadable_frame_duration_is_an_unknown_frame_rate_not_a_crash():
    """mkvmerge prints default_duration as text and some remuxes report it as
    "N/A". The frame rate is derived by dividing by it, and a ValueError here
    escapes a plain metadata probe and fails the encode — over a number only
    used for a display detail."""
    stdout = json.dumps({"tracks": [{"type": "video",
                                     "properties": {"default_duration": "N/A"}}]})

    assert mkvtools_mod.parse_video_fps(stdout) is None


async def test_a_missing_mkvtoolnix_is_named_before_anything_is_spawned(
        tmp_path, monkeypatch):
    """mkvmerge and mkvextract are separate settings and either can be blank on
    a freshly provisioned worker. Each entry point has to name the one it needs
    rather than spawning argv[0]=''."""
    _install(monkeypatch, FakeProcess())
    tools = MkvTools(_settings(tmp_path))

    with pytest.raises(ToolNotFoundError, match="mkvmerge"):
        await tools.probe(Path("/z/rip/t00.mkv"))
    with pytest.raises(ToolNotFoundError, match="mkvextract"):
        await tools.extract_tracks(Path("/z/rip/t00.mkv"), [(2, tmp_path / "2.sup")])


# --------------------------------------------------------------------------
# makemkv: scan
# --------------------------------------------------------------------------

SCAN_LINES = [
    'MSG:1005,0,1,"MakeMKV v1.17.7 started","%1 started","MakeMKV v1.17.7"',
    'MSG:5021,0,3,"Title #2 has length 22 seconds, which is less than minimum",'
    '"%1","x"',
    'DRV:0,2,999,12,"ASUS BD","THE_DISC","E:"',
    'CINFO:2,0,"The Disc"',
    'TINFO:0,9,0,"1:52:03"',
    'TINFO:0,27,0,"title_t00.mkv"',
    'TINFO:0,2,0,"Main Feature"',
    'SINFO:0,0,1,6201,"Video"',
]


def _mk(tmp_path: Path, **settings_kwargs) -> MakeMKV:
    s = _settings(tmp_path, makemkvcon="/usr/bin/makemkvcon")
    for key, value in settings_kwargs.items():
        setattr(s, key, value)
    return MakeMKV(s)


async def test_a_scan_returns_the_disc_the_robot_output_described(
        tmp_path, monkeypatch):
    """Everything the user is then asked to confirm — the disc name, which
    titles exist, how long each runs — comes out of this one call. Handing the
    parser the wrong stream (or dropping the source selector) yields a disc with
    no titles, which the UI can only report as an unreadable disc."""
    fake = _install(monkeypatch, FakeProcess(lambda cmd: SCAN_LINES))

    disc = await _mk(tmp_path).scan("disc:0")

    assert disc.source == "disc:0"
    assert disc.disc_name == "The Disc"
    assert [t.title_id for t in disc.titles] == [0]
    assert disc.titles[0].duration_seconds == 1 * 3600 + 52 * 60 + 3
    assert disc.drive_device == "E:"       # resolved from the DRV line, for eject
    assert fake.cmd[-1] == "disc:0"


async def test_makemkv_log_lines_reach_the_reporter_as_the_scan_runs(
        tmp_path, monkeypatch):
    """A scan that finds no titles is the single most common support question,
    and the reason is always in these messages (not registered, AACS failed,
    everything under the minimum length). Forwarding only the parsed titles
    leaves the UI saying "no titles" with no explanation."""
    _install(monkeypatch, FakeProcess(lambda cmd: SCAN_LINES))
    heard: list[str] = []

    await _mk(tmp_path).scan("disc:0", on_message=heard.append)

    assert heard == [
        "MakeMKV v1.17.7 started",
        "Title #2 has length 22 seconds, which is less than minimum",
    ]


async def test_only_whole_message_lines_are_forwarded_as_messages(
        tmp_path, monkeypatch):
    """Two things at once. The robot stream is mostly DRV/TINFO/SINFO records,
    and forwarding those would bury the lines that matter under hundreds of rows
    of attribute dumps. And a scan cancelled mid-write leaves a truncated final
    line: reaching for the message field that is not there raises IndexError
    inside the stdout pump and takes down a scan that had already succeeded."""
    _install(monkeypatch, FakeProcess(lambda cmd: [
        'DRV:0,2,999,12,"ASUS BD","THE_DISC","E:"',
        'TINFO:0,9,0,"1:52:03"',
        'MSG:5021,0,3,"the only message","%1","x"',
        "MSG:5021,0",                       # truncated: killed mid-line
    ]))
    heard: list[str] = []

    await _mk(tmp_path).scan("disc:0", on_message=heard.append)

    assert heard == ["the only message"]


async def test_a_scan_asks_makemkv_for_robot_output_and_the_configured_minimum(
        tmp_path, monkeypatch):
    """Without -r the output is human prose that parses to nothing. The minimum
    length is what keeps 40 studio logos and menu loops out of the title list,
    and it is a setting — a hardcoded default would ignore the box set that
    needed it lowered to catch 8-minute episodes."""
    fake = _install(monkeypatch, FakeProcess(lambda cmd: SCAN_LINES))

    await _mk(tmp_path, min_length_seconds=45).scan("file:/z/iso/BDMV")

    assert fake.cmd[0] == "/usr/bin/makemkvcon"
    assert "-r" in fake.cmd
    assert "--minlength=45" in fake.cmd
    assert fake.cmd[-2:] == ["info", "file:/z/iso/BDMV"]


async def test_a_scan_with_nobody_listening_still_scans(tmp_path, monkeypatch):
    """The CLI and the auto-scan watcher call this without a message callback.
    An unguarded forward would raise inside the stdout pump on MakeMKV's own
    startup banner — the first line of every scan there has ever been."""
    _install(monkeypatch, FakeProcess(lambda cmd: SCAN_LINES))

    disc = await _mk(tmp_path).scan("disc:0")

    assert [t.title_id for t in disc.titles] == [0]


# --------------------------------------------------------------------------
# makemkv: rip_title
# --------------------------------------------------------------------------

RIP_LINES = [
    'PRGC:5017,0,"Saving to MKV file"',
    "PRGV:12345,3000,65536",
    'MSG:5005,0,0,"Copy complete. 1 titles saved.","%1","x"',
]


def _rip_responder(dest: Path, *, produces=(), lines=RIP_LINES):
    def respond(cmd):
        for name in produces:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / name).write_bytes(b"\x1aE\xdf\xa3")
        return list(lines)
    return respond


async def test_a_rip_reports_only_the_files_this_run_produced(
        tmp_path, monkeypatch):
    """The destination is not always empty: a retried title, or a second title
    ripped into the same folder, leaves earlier .mkv files behind. Returning
    everything present would hand the encoder the PREVIOUS title's file and
    place it under this title's name — the wrong film, silently.

    The second arm varies exactly one thing: makemkvcon exits 0 without writing
    anything (a disc it gave up on), and the wrapper must report nothing rather
    than the file that was already sitting there."""
    dest = tmp_path / "rip"
    dest.mkdir()
    (dest / "title_t00.mkv").write_bytes(b"old")

    _install(monkeypatch, FakeProcess(_rip_responder(dest, produces=["title_t01.mkv"])))
    assert await _mk(tmp_path).rip_title("disc:0", 1, dest) == [dest / "title_t01.mkv"]

    _install(monkeypatch, FakeProcess(_rip_responder(dest)))
    assert await _mk(tmp_path).rip_title("disc:0", 1, dest) == []


async def test_several_files_from_one_title_come_back_in_a_stable_order(
        tmp_path, monkeypatch):
    """MakeMKV splits some titles into more than one file. The caller takes the
    first as the ripped video, so an order that depends on filesystem iteration
    would make a re-run pick a different file and encode a different segment."""
    dest = tmp_path / "rip"
    _install(monkeypatch, FakeProcess(
        _rip_responder(dest, produces=["title_t02.mkv", "title_t00.mkv",
                                       "title_t01.mkv"])))

    ripped = await _mk(tmp_path).rip_title("disc:0", 0, dest)

    assert [p.name for p in ripped] == [
        "title_t00.mkv", "title_t01.mkv", "title_t02.mkv"]


async def test_the_rip_destination_exists_before_makemkv_is_spawned(
        tmp_path, monkeypatch):
    """makemkvcon exits with "Destination directory does not exist" rather than
    creating it, and this folder is created fresh per job under staging."""
    dest = tmp_path / "staging" / "job7" / "rip"
    seen = {}

    def respond(cmd):
        seen["existed"] = dest.is_dir()
        return []

    _install(monkeypatch, FakeProcess(respond))

    await _mk(tmp_path).rip_title("disc:0", 0, dest)

    assert seen["existed"] is True


async def test_the_rip_command_names_the_title_and_destination_makemkv_expects(
        tmp_path, monkeypatch):
    """makemkvcon's positional order is `mkv <source> <title> <dest>`, and the
    title index is an int in this codebase. Swapping two positions rips the
    wrong title (or every title); an unconverted int makes the spawn TypeError
    before makemkvcon starts."""
    dest = tmp_path / "rip"
    fake = _install(monkeypatch, FakeProcess())

    await _mk(tmp_path, min_length_seconds=45).rip_title("disc:0", 7, dest)

    assert fake.cmd[-4:] == ["mkv", "disc:0", "7", str(dest)]
    assert "-r" in fake.cmd
    assert "--minlength=45" in fake.cmd
    assert all(isinstance(a, str) for a in fake.cmd), fake.cmd


async def test_rip_progress_is_the_current_operation_over_its_maximum(
        tmp_path, monkeypatch):
    """PRGV carries three numbers: current-operation, total-progress, maximum.
    The bar tracks the whole rip, so it is the SECOND over the third — reading
    the first instead gives a bar that restarts at every internal stage and a
    completely wrong ETA on a two-hour rip."""
    dest = tmp_path / "rip"
    _install(monkeypatch, FakeProcess(_rip_responder(dest, lines=[
        'PRGC:5017,0,"Saving to MKV file"',
        "PRGV:60000,16384,65536",
    ])))
    seen: list = []

    await _mk(tmp_path).rip_title("disc:0", 0, dest,
                                  on_progress=lambda label, frac: seen.append((label, frac)))

    assert seen == [("Saving to MKV file", 0.25)]


async def test_the_progress_label_follows_the_operation_makemkv_announces(
        tmp_path, monkeypatch):
    """PRGC/PRGT name the stage ("Analyzing seamless segments", "Saving to MKV
    file"). Carrying the label forward is what makes a rip that is analysing for
    ten minutes distinguishable from one that has stalled."""
    dest = tmp_path / "rip"
    _install(monkeypatch, FakeProcess(_rip_responder(dest, lines=[
        'PRGT:5018,0,"Analyzing seamless segments"',
        "PRGV:1,1,4",
        'PRGC:5017,0,"Saving to MKV file"',
        "PRGC:5017",                 # truncated line: must not replace the label
        "PRGV:1,3,4",
    ])))
    seen: list = []

    await _mk(tmp_path).rip_title("disc:0", 0, dest,
                                  on_progress=lambda label, frac: seen.append((label, frac)))

    assert seen == [("Analyzing seamless segments", 0.25),
                    ("Saving to MKV file", 0.75)]


async def test_an_unknown_maximum_reports_indeterminate_rather_than_dividing_by_zero(
        tmp_path, monkeypatch):
    """makemkvcon emits PRGV:0,0,0 before it knows the size of the job. A
    ZeroDivisionError raised inside the stdout pump aborts the rip itself, which
    is a real disc read thrown away over a progress line."""
    dest = tmp_path / "rip"
    _install(monkeypatch, FakeProcess(_rip_responder(dest, lines=[
        'PRGC:5017,0,"Saving to MKV file"', "PRGV:0,0,0"])))
    seen: list = []

    await _mk(tmp_path).rip_title("disc:0", 0, dest,
                                  on_progress=lambda label, frac: seen.append((label, frac)))

    assert seen == [("Saving to MKV file", None)]


async def test_an_async_progress_callback_is_awaited_not_merely_created(
        tmp_path, monkeypatch):
    """The web UI's reporter is a coroutine — it writes progress to the shared
    store and pushes an SSE frame. Calling it without awaiting produces a
    never-run coroutine: the rip appears frozen at 0% for its whole duration
    while Python quietly warns about it, and every other box in the cluster sees
    the job as stalled."""
    dest = tmp_path / "rip"
    _install(monkeypatch, FakeProcess(_rip_responder(dest, lines=[
        'PRGC:5017,0,"Saving to MKV file"', "PRGV:1,1,2"])))
    seen: list = []

    async def on_progress(label, frac):
        seen.append((label, frac))

    await _mk(tmp_path).rip_title("disc:0", 0, dest, on_progress=on_progress)

    assert seen == [("Saving to MKV file", 0.5)]


async def test_rip_progress_lines_are_harmless_with_no_listener_attached(
        tmp_path, monkeypatch):
    """The CLI rips without a progress callback. An unguarded call would raise
    TypeError inside the stdout pump on the first PRGV line and kill the rip —
    while the identical lines with a listener attached are two normal ticks."""
    dest = tmp_path / "rip"
    lines = ['PRGC:5017,0,"Saving to MKV file"', "PRGV:1,1,4", "PRGV:1,2,4"]

    _install(monkeypatch, FakeProcess(_rip_responder(dest, lines=lines)))
    heard: list = []
    await _mk(tmp_path).rip_title("disc:0", 0, dest,
                                  on_progress=lambda label, frac: heard.append(frac))
    assert heard == [0.25, 0.5]

    _install(monkeypatch, FakeProcess(_rip_responder(dest, lines=lines)))
    assert await _mk(tmp_path).rip_title("disc:0", 0, dest) == []


async def test_makemkv_log_lines_reach_the_reporter_during_a_rip(
        tmp_path, monkeypatch):
    """The read errors that produce a truncated file ("Error while reading, gave
    up") appear here and nowhere else. Losing them leaves a job whose only
    record is a short file and no reason why. A rip killed mid-line leaves a
    partial record behind it, which must not raise out of the stdout pump."""
    dest = tmp_path / "rip"
    _install(monkeypatch, FakeProcess(_rip_responder(dest, lines=[
        "PRGV:1,1,4",
        'TINFO:0,9,0,"1:52:03"',          # attribute dumps are not log lines
        'MSG:2003,0,3,"Error while reading data, gave up","%1","x"',
        'MSG:5005,0,0,"Copy complete. 1 titles saved.","%1","x"',
        "MSG:5005,0",                     # truncated: killed mid-line
    ])))
    heard: list[str] = []

    await _mk(tmp_path).rip_title("disc:0", 0, dest, on_message=heard.append)

    assert heard == ["Error while reading data, gave up",
                     "Copy complete. 1 titles saved."]


async def test_a_rip_message_containing_commas_arrives_whole(
        tmp_path, monkeypatch):
    """MakeMKV's messages are comma-separated fields whose text field is quoted
    and routinely contains commas. Splitting naively truncates the reason a rip
    failed at its first comma — usually right before the useful part."""
    dest = tmp_path / "rip"
    _install(monkeypatch, FakeProcess(_rip_responder(dest, lines=[
        'MSG:2003,0,3,"Error 5 occurred while reading \'BD\', sector 12,345","%1"',
    ])))
    heard: list[str] = []

    await _mk(tmp_path).rip_title("disc:0", 0, dest, on_message=heard.append)

    assert heard == ["Error 5 occurred while reading 'BD', sector 12,345"]


async def test_a_rip_without_a_configured_makemkvcon_names_the_missing_tool(
        tmp_path, monkeypatch):
    """A worker box that never had MakeMKV installed should say which setting is
    empty, not fail deep inside the spawn."""
    _install(monkeypatch, FakeProcess())
    tools = MakeMKV(_settings(tmp_path))

    with pytest.raises(ToolNotFoundError, match="makemkvcon"):
        await tools.rip_title("disc:0", 0, tmp_path / "rip")
    with pytest.raises(ToolNotFoundError, match="makemkvcon"):
        await tools.scan("disc:0")


# --------------------------------------------------------------------------
# makemkv: list_drives
# --------------------------------------------------------------------------

async def test_a_disc_name_the_quick_probe_missed_is_recovered_from_the_operating_system(
        tmp_path, monkeypatch):
    """makemkvcon's --cache=1 enumeration regularly reports an empty disc name
    for a disc the OS has mounted and File Explorer shows. The drives panel then
    offers a drive with nothing to add, and auto-open never fires — so the
    volume label is read back from the OS and the drive is marked as
    OS-detected. It stays a normal, rippable drive (index 0), unlike the blind
    drives makemkvcon could not enumerate at all.

    The second drive is the control: genuinely empty, so the OS has no label for
    it either, and it must stay nameless rather than being marked as holding a
    disc that isn't there."""
    _install(monkeypatch, FakeProcess(lambda cmd: [
        'DRV:0,2,999,12,"ASUS BD","","E:"',
        'DRV:1,2,999,12,"LG BD","","F:"',
    ]))
    monkeypatch.setattr(makemkv_mod, "os_disc_label",
                        lambda dev: "THE_DISC" if dev == "E:" else None)
    monkeypatch.setattr(makemkv_mod, "os_optical_drives", lambda: [("E:", "THE_DISC")])

    drives = await _mk(tmp_path).list_drives()

    assert len(drives) == 2                        # E: not also listed as blind
    loaded, empty = drives
    assert (loaded.index, loaded.disc_name) == (0, "THE_DISC")
    assert loaded.os_detected is True
    assert loaded.makemkv_blind is False
    assert empty.disc_name is None and empty.os_detected is False


async def test_the_operating_system_never_overrides_a_name_makemkv_reported(
        tmp_path, monkeypatch):
    """The Windows volume label is the coarser of the two names (BD_ROM,
    LOGICAL_VOLUME_ID); makemkvcon's is the one the disc actually carries, and
    it becomes the folder name. Consulting the OS unconditionally would rename
    every disc to its label — the same run, varying only whether makemkvcon
    filled the field in."""
    _install(monkeypatch, FakeProcess(
        lambda cmd: ['DRV:0,2,999,12,"ASUS BD","MINORITY_REPORT","E:"']))
    monkeypatch.setattr(makemkv_mod, "os_disc_label", lambda dev: "BD_ROM")
    monkeypatch.setattr(makemkv_mod, "os_optical_drives", lambda: [("E:", "BD_ROM")])

    drives = await _mk(tmp_path).list_drives()

    assert [d.disc_name for d in drives] == ["MINORITY_REPORT"]
    assert drives[0].os_detected is False


async def test_a_wedged_enumeration_still_lists_whatever_it_managed_to_report(
        tmp_path, monkeypatch):
    """makemkvcon exits non-zero when a drive is half-offline, having already
    printed the DRV lines for the drives that do work. Treating that exit code
    as fatal would empty the drives panel — including the drive holding the disc
    someone is waiting on — every time one drive misbehaves."""
    fake = _install(monkeypatch, FakeProcess(
        lambda cmd: (['DRV:0,2,999,12,"ASUS BD","THE_DISC","E:"'], 1)))
    monkeypatch.setattr(makemkv_mod, "os_optical_drives", lambda: [])

    drives = await _mk(tmp_path).list_drives()

    assert [d.disc_name for d in drives] == ["THE_DISC"]
    assert fake.kwargs[0]["check"] is False


# --------------------------------------------------------------------------
# makemkv: two robot attributes the wrappers pass straight through
# --------------------------------------------------------------------------

def test_a_titles_own_name_is_kept_from_the_robot_output():
    """TINFO attribute 2 is the title's own label ("Main Feature", an episode
    name on a TV disc). It is what the confirm screen shows beside each title,
    and with it dropped every title on the disc reads as an anonymous number."""
    disc = parse_info(['TINFO:0,2,0,"Main Feature"'], "disc:0")

    assert disc.titles[0].name == "Main Feature"


def test_an_unrecognised_stream_type_is_recorded_as_unknown_not_guessed():
    """SINFO's type strings are open-ended (button/overlay tracks on some
    discs). Falling through to video or audio would put a menu overlay into the
    audio-language list and let it decide which languages are kept."""
    disc = parse_info(['SINFO:0,3,1,6201,"Button"'], "disc:0")

    assert disc.titles[0].streams[0].stream_type is StreamType.UNKNOWN
