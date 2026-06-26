"""Drive-device resolution (for auto-eject), rip-speed formatting, and ETA."""

from blurayripper.pipeline import _RipSpeed, _fmt_eta
from blurayripper.tools.makemkv import parse_info

DRV_OUTPUT = [
    'DRV:0,0,999,0,"BD-RE ASUS BW-16D1HT","WarGames","F:"',
    'DRV:1,256,999,0,"","",""',
    "CINFO:2,0,\"WarGames\"",
    "TINFO:0,9,0,\"1:52:42\"",
]


def test_drive_device_resolved_for_disc_source():
    disc = parse_info(DRV_OUTPUT, "disc:0")
    assert disc.drive_device == "F:"


def test_drive_device_from_dev_source():
    disc = parse_info(DRV_OUTPUT, "dev:/dev/sr0")
    assert disc.drive_device == "/dev/sr0"


def test_file_source_has_no_drive_device():
    disc = parse_info(DRV_OUTPUT, "file:/mnt/rip")
    assert disc.drive_device is None


def test_rip_speed_blank_until_enough_data():
    # No total bytes -> no speed string.
    assert _RipSpeed(total_bytes=0).update(0.5) == ""
    # With total but first sample, EMA not warmed -> blank.
    assert _RipSpeed(total_bytes=10_000_000_000).update(0.0) == ""


def test_fmt_eta():
    assert _fmt_eta(45) == "45s"
    assert _fmt_eta(125) == "2m05s"
    assert _fmt_eta(3725) == "1h02m"
