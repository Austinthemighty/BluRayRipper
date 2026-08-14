import json

from blurayripper.tools.handbrake import HandBrakeJsonParser, _progress_from_block


def _feed_all(parser: HandBrakeJsonParser, text: str):
    blocks = []
    for line in text.splitlines():
        out = parser.feed_line(line)
        if out is not None:
            blocks.append(out)
    return blocks


def test_parses_multiline_progress_block():
    stream = (
        'Version: {\n'
        ' "Name": "HandBrake"\n'
        '}\n'
        'Progress: {\n'
        ' "State": "WORKING",\n'
        ' "Working": { "Progress": 0.42, "ETASeconds": 90, "RateAvg": 24.5 }\n'
        '}\n'
    )
    parser = HandBrakeJsonParser()
    blocks = _feed_all(parser, stream)
    labels = [b[0] for b in blocks]
    assert "Version" in labels and "Progress" in labels

    progress_text = next(t for label, t in blocks if label == "Progress")
    obj = json.loads(progress_text)
    prog = _progress_from_block(obj)
    assert prog.state == "WORKING"
    assert abs(prog.fraction - 0.42) < 1e-9
    assert prog.eta_seconds == 90
    assert abs(prog.fps - 24.5) < 1e-9


def test_workdone_state():
    parser = HandBrakeJsonParser()
    blocks = _feed_all(parser, 'Progress: {\n "State": "WORKDONE",\n "WorkDone": { "Error": 0 }\n}\n')
    obj = json.loads(blocks[0][1])
    assert _progress_from_block(obj).state == "WORKDONE"


def test_brace_in_string_does_not_break_parsing():
    parser = HandBrakeJsonParser()
    stream = 'Progress: {\n "Note": "a } brace in a string",\n "State": "WORKING"\n}\n'
    blocks = _feed_all(parser, stream)
    assert len(blocks) == 1
    obj = json.loads(blocks[0][1])
    assert obj["State"] == "WORKING"


def test_the_log_names_the_preset_that_is_actually_running(tmp_path):
    """Speed and quality live in the preset JSON, not our settings, so the job
    log has to say which preset ran — otherwise a preset that quietly changed
    (medium vs slower is several times the wall-clock) is invisible."""
    from blurayripper.config import load_settings
    from blurayripper.tools.handbrake import HandBrake

    pf = tmp_path / "presets.json"
    pf.write_text(json.dumps({"PresetList": [
        {"PresetName": "Mine", "VideoEncoder": "x265_10bit",
         "VideoPreset": "slower", "VideoQualitySlider": 18}]}))
    s = load_settings()
    s.encode.preset_file, s.encode.preset_name = pf, "Mine"

    out = HandBrake(s).speed_summary()
    assert "Mine" in out and "slower" in out and "x265_10bit" in out and "CQ 18" in out


def test_a_preset_name_that_is_not_in_the_file_is_called_out(tmp_path):
    """Silent fallback to HandBrake's built-in default is the worst case: every
    encode runs at settings nobody chose. Say so rather than reporting a name."""
    from blurayripper.config import load_settings
    from blurayripper.tools.handbrake import HandBrake

    pf = tmp_path / "presets.json"
    pf.write_text(json.dumps({"PresetList": [{"PresetName": "Other"}]}))
    s = load_settings()
    s.encode.preset_file, s.encode.preset_name = pf, "Missing"

    assert "NOT FOUND" in HandBrake(s).speed_summary()
