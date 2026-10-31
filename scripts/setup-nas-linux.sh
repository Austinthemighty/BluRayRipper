#!/usr/bin/env bash
#
# Mount the NAS share on a Linux box the way BluRayRipper needs it, and keep it
# mounted across reboots — docs/SETUP.md "1. Mount the NAS", done for you:
#
#   * installs cifs-utils if it is missing;
#   * asks for the NAS username and password ON THIS TERMINAL and keeps them in
#     a root-only credentials file (mode 600), never in /etc/fstab, which every
#     account on the box can read;
#   * writes one /etc/fstab line with the options that matter (hard, uid/gid,
#     nofail, _netdev, automount — SETUP.md says why each one), backing fstab up
#     first and checking the line before it is used;
#   * mounts it and proves the service account can write to it.
#
# Safe to re-run: it replaces its own fstab line, reuses saved credentials if you
# say so, and asks before replacing a line it did not write.
#
# Usage (from the repo root):
#   ./scripts/setup-nas-linux.sh --share //192.168.1.10/Share --mount /mnt/nas
#   ./scripts/setup-nas-linux.sh --share //nas/Share --mount /mnt/nas --owner austin
#   ./scripts/setup-nas-linux.sh --share //nas/Share --mount /mnt/nas --print
#
#   --owner NAME        the account the service runs as; it owns the files on the
#                       mount (default: you)
#   --nas-user NAME     the NAS account (default: asked, suggesting --owner)
#   --cred-file PATH    where to keep the credentials (default /etc/cifs-HOST.cred)
#   --smb-version V     pin the SMB dialect (e.g. 3.0) for an old NAS
#   --print             show what /etc/fstab would become; change nothing
#
# There is deliberately no --password: on the command line it would land in your
# shell history and be visible to every user in `ps` while this runs.
set -euo pipefail

SHARE=""
MOUNT_POINT=""
OWNER="$(id -un)"
NAS_USER=""
CRED_FILE=""
SMB_VERSION=""
ACTION="setup"
while [ $# -gt 0 ]; do
  case "$1" in
    --share) SHARE="$2"; shift 2 ;;
    --mount) MOUNT_POINT="$2"; shift 2 ;;
    --owner) OWNER="$2"; shift 2 ;;
    --nas-user) NAS_USER="$2"; shift 2 ;;
    --cred-file) CRED_FILE="$2"; shift 2 ;;
    --smb-version) SMB_VERSION="$2"; shift 2 ;;
    --print) ACTION="print"; shift ;;
    --password|--password=*)
      echo "No --password, on purpose: it would end up in your shell history. The" >&2
      echo "script asks for it instead, without echoing it." >&2
      exit 2 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  + %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  x %s\033[0m\n' "$*" >&2; exit 1; }

# --- what to mount, and where ------------------------------------------------

# \\host\share (Windows) and smb://host/share (a file manager) mean //host/share.
case "$SHARE" in
  smb://*) SHARE="//${SHARE#smb://}" ;;
  \\\\*) SHARE="${SHARE//\\//}" ;;
esac
SHARE="${SHARE%/}"
case "$SHARE" in
  //?*/?*) ;;
  *) echo "Pass the share as --share //SERVER/SHARE (e.g. //192.168.1.10/Share)." >&2; exit 2 ;;
esac
case "$MOUNT_POINT" in
  /?*) MOUNT_POINT="${MOUNT_POINT%/}" ;;
  *) echo "Pass where to mount it as an absolute path: --mount /mnt/nas" >&2; exit 2 ;;
esac

HOST="${SHARE#//}"; HOST="${HOST%%/*}"
if [ -z "$CRED_FILE" ]; then
  CRED_FILE="/etc/cifs-$(printf '%s' "$HOST" | tr -c 'A-Za-z0-9._-' '_').cred"
fi
case "$CRED_FILE" in
  *[[:space:],]*) die "The credentials path can't contain spaces or commas: $CRED_FILE" ;;
esac

if ! OWNER_UID="$(id -u "$OWNER" 2>/dev/null)" || ! OWNER_GID="$(id -g "$OWNER" 2>/dev/null)"; then
  echo "No such user '$OWNER' — pass --owner with the account the service runs as." >&2
  exit 2
fi

# fstab separates fields with whitespace, so a space inside a path is \040.
fstab_escape() { local s="$1"; printf '%s' "${s// /\\040}"; }

OPTIONS="credentials=$CRED_FILE,uid=$OWNER_UID,gid=$OWNER_GID,file_mode=0664,dir_mode=0775"
OPTIONS+=",iocharset=utf8,nobrl,_netdev,nofail,hard,x-systemd.automount,x-systemd.mount-timeout=30"
[ -n "$SMB_VERSION" ] && OPTIONS+=",vers=$SMB_VERSION"
MARKER="# BluRayRipper NAS: $MOUNT_POINT (scripts/setup-nas-linux.sh)"
FSTAB_LINE="$(fstab_escape "$SHARE")  $(fstab_escape "$MOUNT_POINT")  cifs  $OPTIONS  0 0"
ESCAPED_MP="$(fstab_escape "$MOUNT_POINT")"
FSTAB="${BLURAYRIPPER_FSTAB:-/etc/fstab}"   # overridable for the tests only

# Values go to awk through the environment: -v would decode the \040 escapes,
# and a mount point with a space in it would never match its own line.
fstab_line_for_mount() {   # the line that mounts something at our mount point now
  [ -f "$FSTAB" ] || return 0
  MP="$ESCAPED_MP" awk '$1 !~ /^#/ && $2 == ENVIRON["MP"]' "$FSTAB"
}
rewritten_fstab() {   # fstab with any line for our mount point replaced by ours
  if [ -f "$FSTAB" ]; then
    MP="$ESCAPED_MP" MARKER="$MARKER" awk '
      $0 == ENVIRON["MARKER"] { next }
      $1 !~ /^#/ && $2 == ENVIRON["MP"] { next }
      { print }' "$FSTAB"
  fi
  printf '%s\n%s\n' "$MARKER" "$FSTAB_LINE"
}

if [ "$ACTION" = "print" ]; then
  rewritten_fstab
  echo "# (credentials file, root only, mode 600: $CRED_FILE)"
  exit 0
fi

# --- the real thing ----------------------------------------------------------

[ "$(uname -s)" = "Linux" ] || die "This sets up a Linux mount; on this OS mount the share by hand."
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
INTERACTIVE=0; [ -t 0 ] && INTERACTIVE=1

ask_yes() {   # ask_yes "question" default(y|n)
  local reply
  if [ "$INTERACTIVE" -ne 1 ]; then [ "$2" = "y" ]; return; fi
  read -r -p "$1 $([ "$2" = y ] && echo '[Y/n]' || echo '[y/N]') " reply
  reply="${reply:-$2}"
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

info "NAS share $SHARE -> $MOUNT_POINT (files owned by $OWNER)"

# 1. cifs-utils — the mount.cifs helper.
if ! command -v mount.cifs >/dev/null 2>&1 && [ ! -x /sbin/mount.cifs ] && [ ! -x /usr/sbin/mount.cifs ]; then
  info "Installing cifs-utils"
  if command -v apt-get >/dev/null 2>&1; then $SUDO apt-get install -y -qq cifs-utils
  elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y cifs-utils
  elif command -v pacman >/dev/null 2>&1; then $SUDO pacman -S --needed --noconfirm cifs-utils
  elif command -v zypper >/dev/null 2>&1; then $SUDO zypper --non-interactive install cifs-utils
  else die "No package manager found — install cifs-utils by hand, then re-run."
  fi
  ok "cifs-utils installed"
fi

# 2. Credentials, root-only. Read from the terminal; never on a command line.
write_creds=1
if $SUDO test -f "$CRED_FILE"; then
  if ask_yes "Credentials for $HOST are already saved in $CRED_FILE. Use them?" y; then
    write_creds=0
    ok "Using the saved credentials in $CRED_FILE"
  fi
fi
if [ "$write_creds" -eq 1 ]; then
  [ "$INTERACTIVE" -eq 1 ] || die "Run this from a terminal: it asks for the NAS password."
  if [ -z "$NAS_USER" ]; then
    read -r -p "NAS username [$OWNER]: " NAS_USER
    NAS_USER="${NAS_USER:-$OWNER}"
  fi
  # IFS= so a password that starts or ends with a space keeps it.
  NAS_PASSWORD=""
  IFS= read -r -s -p "NAS password for $NAS_USER (not shown): " NAS_PASSWORD; echo
  [ -n "$NAS_PASSWORD" ] || die "No password given."
  # Created empty at 600 first, so the password is never in a readable file.
  # printf is a shell builtin, so the password is not in any process's argv.
  $SUDO install -m 600 -o root -g root /dev/null "$CRED_FILE"
  printf 'username=%s\npassword=%s\n' "$NAS_USER" "$NAS_PASSWORD" | $SUDO tee "$CRED_FILE" >/dev/null
  NAS_PASSWORD=""
  ok "Saved to $CRED_FILE (root only)"
fi

# 3. The mount point.
$SUDO mkdir -p "$MOUNT_POINT"

# 4. /etc/fstab: one line for this mount point, ours.
changed=0
existing="$(fstab_line_for_mount)"
if [ "$existing" = "$FSTAB_LINE" ]; then
  ok "/etc/fstab already has this line"
else
  if [ -n "$existing" ] && ! grep -qxF "$MARKER" "$FSTAB"; then
    warn "/etc/fstab already mounts something at $MOUNT_POINT:"
    printf '      %s\n' "$existing"
    ask_yes "Replace it with the BluRayRipper line?" n || die "Left /etc/fstab alone. Pick another --mount, or edit it by hand."
  fi
  # Check the new line on its own before it goes anywhere near the real fstab.
  # Only errors ([E]) stop it: a warning such as "cifs not in /proc/filesystems"
  # just means the module isn't loaded until the first mount.
  if findmnt --help 2>/dev/null | grep -q -- '--verify'; then
    check="$(mktemp)"
    printf '%s\n' "$FSTAB_LINE" > "$check"
    verdict="$(findmnt --verify --tab-file "$check" 2>&1 || true)"
    rm -f "$check"
    if printf '%s' "$verdict" | grep -q '\[E\]'; then
      printf '%s\n' "$verdict" >&2
      die "That fstab line didn't pass findmnt --verify; /etc/fstab is unchanged."
    fi
  fi
  backup="$FSTAB.bak-blurayripper-$(date +%Y%m%d-%H%M%S)"
  $SUDO cp -p "$FSTAB" "$backup"
  new="$(mktemp)"
  rewritten_fstab > "$new"
  $SUDO tee "$FSTAB" < "$new" >/dev/null
  rm -f "$new"
  changed=1
  ok "Wrote /etc/fstab (the old one is $backup)"
fi
$SUDO systemctl daemon-reload 2>/dev/null || true

# 5. Mount it now, and say why if it won't.
if findmnt -n -t cifs -M "$MOUNT_POINT" >/dev/null 2>&1; then
  ok "Already mounted"
  if [ "$changed" -eq 1 ]; then
    warn "It is still mounted the old way. The new line takes effect after a reboot, or"
    warn "  sudo umount $MOUNT_POINT && sudo mount $MOUNT_POINT   (with nothing using it)"
  fi
else
  info "Mounting $MOUNT_POINT"
  if ! out="$($SUDO mount "$MOUNT_POINT" 2>&1)"; then
    printf '%s\n' "$out" >&2
    case "$out" in
      *"error(13)"*) warn "Permission denied: the NAS refused that username or password. Re-run and answer n to type them again." ;;
      *"error(2)"*)  warn "No such share on $HOST. Check the name after the server (it's case-sensitive on some NAS)." ;;
      *"error(112)"*|*"error(113)"*|*"error(115)"*|*"error(111)"*)
                     warn "Can't reach $HOST. Is the address right, and is SMB enabled on the NAS?" ;;
      *"error(95)"*) warn "The NAS doesn't speak the default SMB dialect. Re-run with --smb-version 3.0 (or 2.1)." ;;
    esac
    die "Not mounted. /etc/fstab and the credentials are in place, so re-running picks up where this stopped."
  fi
  findmnt -n -t cifs -M "$MOUNT_POINT" >/dev/null 2>&1 || die "mount returned, but $MOUNT_POINT is not a CIFS mount."
  ok "Mounted"
fi

# 6. The service account must be able to write there, or every job fails later
#    with "exists but not writable".
probe="$MOUNT_POINT/.blurayripper-write-test.$$"
as_owner() {
  if [ "$OWNER" = "$(id -un)" ]; then "$@"
  elif [ "$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1; then runuser -u "$OWNER" -- "$@"
  else sudo -u "$OWNER" -- "$@"
  fi
}
# shellcheck disable=SC2016  # $1 is meant for the inner sh, not this one
if as_owner sh -c 'touch "$1" && rm -f "$1"' _ "$probe" 2>/dev/null; then
  ok "$OWNER can write to $MOUNT_POINT"
else
  die "$OWNER can't write to $MOUNT_POINT. The NAS account needs write access to the share."
fi

echo
info "Done. It mounts by itself at boot (on first use), and the box still boots if the NAS is down."
warn "Every box must see the staging and output folders at the same path (or a"
warn "Path map entry in Settings → Storage). Check this box with: blurayripper check"
