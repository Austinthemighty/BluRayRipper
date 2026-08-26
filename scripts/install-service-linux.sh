#!/usr/bin/env bash
#
# Run BluRayRipper as a systemd service — the rip box (`serve`) or an encode
# `worker` — so it starts on boot and comes back after a crash.
#
# Linux has no mapped-drive problem (that's why the Windows side uses a scheduled
# task instead, see scripts/install-task-windows.ps1), so this is a real service.
#
# USER vs SYSTEM
#   Default is a USER service (systemctl --user) plus `loginctl enable-linger`, so
#   it starts at boot without anyone logging in, needs no root, and can see a NAS
#   you mount as your user. That also suits immutable/atomic distros (SteamOS).
#   Pass --system for a system-wide unit (needs sudo); use it when the NAS is
#   mounted system-wide via /etc/fstab.
#
# NAS: pass --requires-mount for each mount the job needs (repeatable). systemd
#   then refuses to start until it's actually mounted, instead of failing every
#   job with "no such file or directory" after a reboot race.
#
# Usage (from the repo root):
#   ./scripts/install-service-linux.sh --role worker
#   ./scripts/install-service-linux.sh --role serve --requires-mount /mnt/nas
#   ./scripts/install-service-linux.sh --role worker --system --user-name austin
#   ./scripts/install-service-linux.sh --role worker --status
#   ./scripts/install-service-linux.sh --role worker --uninstall
set -euo pipefail

ROLE=""
SCOPE="user"
WORKER_NAME=""
RUN_AS="$(id -un)"
STOP_TIMEOUT="1h"
MOUNTS=()
ACTION="install"
while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --system) SCOPE="system"; shift ;;
    --user-name) RUN_AS="$2"; shift 2 ;;
    --worker-name) WORKER_NAME="$2"; shift 2 ;;
    --requires-mount) MOUNTS+=("$2"); shift 2 ;;
    --stop-timeout) STOP_TIMEOUT="$2"; shift 2 ;;
    --uninstall) ACTION="uninstall"; shift ;;
    --status) ACTION="status"; shift ;;
    --print) ACTION="print"; shift ;;   # show the unit file, install nothing
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$ROLE" in
  serve|worker) ;;
  *) echo "Pass --role serve  or  --role worker" >&2; exit 2 ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT="blurayripper-${ROLE}.service"
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  + %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }

if [ "$SCOPE" = "user" ]; then
  UNIT_DIR="$HOME/.config/systemd/user"
  SC=(systemctl --user)
else
  UNIT_DIR="/etc/systemd/system"
  SC=(sudo systemctl)
fi

if [ "$ACTION" = "status" ]; then
  "${SC[@]}" status "$UNIT" --no-pager || true
  echo
  info "Live log:  ${SC[*]} ... ;  journalctl $([ "$SCOPE" = user ] && echo --user) -u $UNIT -f"
  exit 0
fi

if [ "$ACTION" = "uninstall" ]; then
  "${SC[@]}" disable --now "$UNIT" 2>/dev/null || true
  if [ "$SCOPE" = "user" ]; then rm -f "$UNIT_DIR/$UNIT"; else sudo rm -f "$UNIT_DIR/$UNIT"; fi
  "${SC[@]}" daemon-reload
  ok "Removed $UNIT."
  exit 0
fi

# Resolve the CLI to an absolute path: systemd has a minimal PATH and will not
# find a venv/pipx shim the way an interactive shell does.
BRR="$(command -v blurayripper || true)"
if [ -z "$BRR" ]; then
  if [ "$ACTION" = "print" ]; then
    BRR="/path/to/blurayripper"   # placeholder so --print still shows the shape
  else
    echo "Can't find 'blurayripper' on PATH. Run ./scripts/setup-linux.sh, which" >&2
    echo "installs it into a virtualenv and links it onto PATH (a plain" >&2
    echo "'pip install -e .' is refused outright on Ubuntu 24.04 and other" >&2
    echo "PEP 668 distros, so it is not a workaround)," >&2
    echo "then re-run this from the same environment." >&2
    exit 2
  fi
fi

EXEC="$BRR $ROLE"
if [ "$ROLE" = "worker" ] && [ -n "$WORKER_NAME" ]; then
  EXEC="$EXEC --name $WORKER_NAME"
fi

REQ=""
for m in ${MOUNTS+"${MOUNTS[@]}"}; do
  REQ+="RequiresMountsFor=$m"$'\n'
done

IDENTITY=""
if [ "$SCOPE" = "system" ]; then
  # A system unit names the account to run as. Catch a typo here rather than
  # letting systemd fail the service at boot with a bare 217/USER.
  if ! RUN_GROUP="$(id -gn "$RUN_AS" 2>/dev/null)"; then
    echo "No such user '$RUN_AS' — pass --user-name with the account that owns" >&2
    echo "the repo and can reach the NAS (and the 'cdrom' group for serve)." >&2
    exit 2
  fi
  IDENTITY="User=$RUN_AS"$'\n'"Group=$RUN_GROUP"$'\n'
  WANTED="multi-user.target"
else
  WANTED="default.target"
fi

DESC="BluRayRipper $([ "$ROLE" = serve ] && echo 'rip box (web UI)' || echo 'encode worker')"
if [ "$ROLE" = "worker" ]; then
  STOP_NOTE="# SIGTERM makes the worker DRAIN: it finishes and places the current encode,
# then exits. That can take a long time, hence the generous stop timeout — systemd
# would otherwise SIGKILL after 90s and throw the finished-but-unplaced encode away."
else
  STOP_NOTE="# SIGTERM shuts uvicorn down gracefully. An in-flight rip/OCR is interrupted, but
# it's checkpointed, so it resumes on the next start rather than restarting."
fi

UNIT_TEXT="[Unit]
Description=$DESC
# Don't start before the network/NAS is there, or every job fails on a reboot race.
After=network-online.target
Wants=network-online.target
${REQ}
[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
${IDENTITY}ExecStart=$EXEC
Restart=always
RestartSec=10
# Unbuffered, so journalctl shows progress live instead of in 4KB bursts.
Environment=PYTHONUNBUFFERED=1
$STOP_NOTE
KillSignal=SIGTERM
TimeoutStopSec=$STOP_TIMEOUT

[Install]
WantedBy=$WANTED
"

if [ "$ACTION" = "print" ]; then
  printf '%s' "$UNIT_TEXT"
  exit 0
fi

info "Installing $UNIT ($SCOPE scope)"
mkdir -p "$UNIT_DIR" 2>/dev/null || sudo mkdir -p "$UNIT_DIR"
if [ "$SCOPE" = "user" ]; then
  printf '%s' "$UNIT_TEXT" > "$UNIT_DIR/$UNIT"
else
  printf '%s' "$UNIT_TEXT" | sudo tee "$UNIT_DIR/$UNIT" >/dev/null
fi
ok "Wrote $UNIT_DIR/$UNIT"

"${SC[@]}" daemon-reload
"${SC[@]}" enable --now "$UNIT"
ok "Enabled and started."

if [ "$SCOPE" = "user" ]; then
  # Without linger a user service stops at logout and never starts at boot.
  if loginctl enable-linger "$(id -un)" 2>/dev/null; then
    ok "Enabled linger — it starts at boot with nobody logged in."
  else
    warn "Couldn't enable linger; run: sudo loginctl enable-linger $(id -un)"
    warn "Without it the service stops when you log out."
  fi
fi

echo
info "Manage it"
echo "  Status : ${SC[*]} status $UNIT"
echo "  Logs   : journalctl $([ "$SCOPE" = user ] && echo --user) -u $UNIT -f"
echo "  Stop   : ${SC[*]} stop $UNIT      # drains first; up to $STOP_TIMEOUT"
echo "  Update : git pull && ${SC[*]} restart $UNIT"
echo
if [ "$ROLE" = "serve" ]; then
  warn "serve needs the optical drives: make sure $RUN_AS is in the 'cdrom' group"
  warn "  sudo usermod -aG cdrom $RUN_AS   (log out and back in to take effect)"
fi
if [ ${#MOUNTS[@]} -eq 0 ]; then
  warn "No --requires-mount given. If staging/output live on a NAS, re-run with"
  warn "  --requires-mount /path/to/mount   so it waits for the mount on boot."
fi
warn "Running headless means no 'u' key to update — use 'Update all workers' in"
warn "the web UI, or 'git pull && ${SC[*]} restart $UNIT'."
