"""scripts/setup-nas-linux.sh: the NAS mount a new box needs, done for you.

Only its --print path runs here (the real one needs Linux, sudo and a NAS),
but that path is the part that has to be exactly right: the /etc/fstab line
it writes, and how it replaces an earlier one. A wrong option there is
silent until it costs a file. Without `hard`, a stall on the NAS truncates a
rip that MakeMKV still reports as a success.
"""

import getpass
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SETUP = REPO / "scripts" / "setup-nas-linux.sh"
INSTALL = REPO / "scripts" / "install-service-linux.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

ME = getpass.getuser()
UID, GID = os.getuid(), os.getgid()


def _run(script, *args, fstab=None):
    env = dict(os.environ)
    if fstab is not None:
        env["BLURAYRIPPER_FSTAB"] = str(fstab)
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                          env=env, timeout=30)


def _print(*args, fstab=None):
    out = _run(SETUP, "--print", "--owner", ME, *args, fstab=fstab)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_the_fstab_line_has_every_option_the_workload_needs(tmp_path):
    out = _print("--share", "//192.168.1.10/Share", "--mount", "/mnt/nas",
                 fstab=tmp_path / "none")
    line = next(ln for ln in out.splitlines() if ln.startswith("//"))
    share, mount, fstype, options, dump, passno = line.split()
    assert (share, mount, fstype, dump, passno) == ("//192.168.1.10/Share", "/mnt/nas",
                                                   "cifs", "0", "0")
    opts = options.split(",")
    assert "credentials=/etc/cifs-192.168.1.10.cred" in opts
    assert f"uid={UID}" in opts and f"gid={GID}" in opts
    for needed in ("hard", "nofail", "_netdev", "x-systemd.automount", "nobrl"):
        assert needed in opts
    assert not any(o.startswith(("password=", "username=")) for o in opts)
    assert "soft" not in opts


def test_a_share_typed_the_windows_or_file_manager_way_is_understood(tmp_path):
    for typed in (r"\\truenas\Share", "smb://truenas/Share", "//truenas/Share/"):
        out = _print("--share", typed, "--mount", "/mnt/nas", fstab=tmp_path / "none")
        assert "\n//truenas/Share  /mnt/nas  cifs  credentials=/etc/cifs-truenas.cred," in out, typed


def test_spaces_are_escaped_the_way_fstab_reads_them(tmp_path):
    out = _print("--share", "//nas/My Share", "--mount", "/mnt/my nas", "--smb-version", "3.0",
                 fstab=tmp_path / "none")
    assert "//nas/My\\040Share  /mnt/my\\040nas  cifs  " in out
    assert out.split("\n")[1].split()[3].endswith(",vers=3.0")


def test_re_running_replaces_its_own_line_and_leaves_the_rest_alone(tmp_path):
    fstab = tmp_path / "fstab"
    fstab.write_text(
        "UUID=abcd / ext4 defaults 0 1\n"
        "# BluRayRipper NAS: /mnt/my nas (scripts/setup-nas-linux.sh)\n"
        "//old/Share  /mnt/my\\040nas  cifs  credentials=/etc/x,uid=1  0 0\n"
        "//other/Media /mnt/media cifs credentials=/etc/y 0 0\n"
        "#//commented/out /mnt/my\\040nas cifs defaults 0 0\n")
    out = _print("--share", "//new/Share", "--mount", "/mnt/my nas", fstab=fstab)
    lines = out.splitlines()
    assert "UUID=abcd / ext4 defaults 0 1" in lines
    assert "//other/Media /mnt/media cifs credentials=/etc/y 0 0" in lines
    assert "#//commented/out /mnt/my\\040nas cifs defaults 0 0" in lines   # comments stay
    assert not any(ln.startswith("//old/") for ln in lines)
    assert lines.count("# BluRayRipper NAS: /mnt/my nas (scripts/setup-nas-linux.sh)") == 1
    assert sum(ln.startswith("//new/Share  /mnt/my\\040nas  cifs  ") for ln in lines) == 1


@pytest.mark.parametrize("args, says", [
    (["--share", "nas/Share", "--mount", "/mnt/nas"], "//SERVER/SHARE"),
    (["--share", "//nas", "--mount", "/mnt/nas"], "//SERVER/SHARE"),
    (["--share", "//nas/Share", "--mount", "mnt/nas"], "absolute path"),
    (["--share", "//nas/Share", "--mount", "/mnt/nas", "--password", "hunter2"], "shell history"),
    (["--share", "//nas/Share", "--mount", "/mnt/nas", "--owner", "no-such-user-xyz"], "No such user"),
    (["--share", "//nas/Share", "--mount", "/mnt/nas", "--cred-file", "/etc/my creds"], "spaces or commas"),
    (["--share", "//nas/Share", "--mount", "/mnt/nas", "--cred-file", "/etc/a,b.cred"], "spaces or commas"),
])
def test_what_it_refuses(args, says):
    out = _run(SETUP, "--print", *args)
    assert out.returncode != 0
    assert says in out.stderr


def test_the_installer_wants_one_mount_point_to_put_a_named_share_at():
    out = _run(INSTALL, "--role", "worker", "--nas", "//nas/Share", "--print")
    assert out.returncode == 2 and "exactly one --requires-mount" in out.stderr
    out = _run(INSTALL, "--role", "worker", "--nas", "//nas/Share", "--print",
               "--requires-mount", "/mnt/a", "--requires-mount", "/mnt/b")
    assert out.returncode == 2


def test_printing_the_unit_sets_up_nothing():
    out = _run(INSTALL, "--role", "worker", "--requires-mount", "/mnt/nas",
               "--nas", "//nas/Share", "--print")
    assert out.returncode == 0, out.stderr
    assert "RequiresMountsFor=/mnt/nas" in out.stdout
    assert "Setting up the NAS" not in out.stdout + out.stderr
