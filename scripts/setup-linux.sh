#!/usr/bin/env bash
#
# Install the external tools BluRayRipper needs on a Linux box, then run
# `blurayripper check`. Aimed at a Linux WORKER (encode + subtitle OCR) — those
# don't rip, so MakeMKV isn't installed by default (ripping stays on the box with
# the optical drives). Pass --with-makemkv only for a Linux rip box.
#
# Everything is installed onto PATH, and BluRayRipper auto-detects tools by name
# (HandBrakeCLI, mkvmerge, mkvextract, ffmpeg, dovi_tool, seconv), so no tool
# paths need to be written to config — a worker only needs its DB URL set
# (BLURAYRIPPER_DATABASE_URL in config/secrets.env).
#
# Debian/Ubuntu (apt). Other distros: install the equivalents by hand.
#
# Usage (from the repo root):
#   ./scripts/setup-linux.sh [--langs pol,deu] [--seconv /path/to/seconv]
#                            [--with-makemkv] [--tools-dir /opt/brr-tools]
set -euo pipefail

LANGS=""
SECONV=""
WITH_MAKEMKV=0
TOOLS_DIR="/opt/brr-tools"
while [ $# -gt 0 ]; do
  case "$1" in
    --langs) LANGS="$2"; shift 2 ;;
    --seconv) SECONV="$2"; shift 2 ;;
    --with-makemkv) WITH_MAKEMKV=1; shift ;;
    --tools-dir) TOOLS_DIR="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  + %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }

if ! command -v apt-get >/dev/null 2>&1; then
  warn "This script targets Debian/Ubuntu (apt). Install these with your package"
  warn "manager instead: handbrake-cli, mkvtoolnix, ffmpeg, tesseract-ocr (+langs),"
  warn "dotnet-sdk-8.0; plus dovi_tool + seconv on PATH."
  exit 1
fi

# --- 1. apt packages: HandBrake, MKVToolNix, ffmpeg, Tesseract + language data ---
info "Installing apt packages (HandBrake CLI, MKVToolNix, ffmpeg, Tesseract)"
TESS_LANGS="tesseract-ocr-eng"
IFS=', ' read -r -a _langs <<< "$LANGS"
for l in "${_langs[@]:-}"; do [ -n "$l" ] && TESS_LANGS="$TESS_LANGS tesseract-ocr-$l"; done
$SUDO apt-get update -qq
# shellcheck disable=SC2086
$SUDO apt-get install -y handbrake-cli mkvtoolnix ffmpeg tesseract-ocr $TESS_LANGS curl tar
ok "HandBrakeCLI, mkvmerge/mkvextract, ffmpeg, tesseract installed (on PATH)."

mkdir -p "$TOOLS_DIR"

# --- 2. dovi_tool (Dolby Vision) — GitHub release, linked onto PATH ------------
if command -v dovi_tool >/dev/null 2>&1; then
  ok "dovi_tool already on PATH: $(command -v dovi_tool)"
else
  info "Installing dovi_tool"
  DT_URL="$(curl -fsSL https://api.github.com/repos/quietvoid/dovi_tool/releases/latest \
    | grep -oE '"browser_download_url": *"[^"]*x86_64-unknown-linux-musl\.tar\.gz"' \
    | head -1 | sed -E 's/.*"(https[^"]*)"$/\1/')"
  if [ -n "$DT_URL" ]; then
    curl -fsSL "$DT_URL" -o "$TOOLS_DIR/dovi_tool.tar.gz"
    tar -xzf "$TOOLS_DIR/dovi_tool.tar.gz" -C "$TOOLS_DIR"
    DT_BIN="$(find "$TOOLS_DIR" -name dovi_tool -type f | head -1)"
    if [ -n "$DT_BIN" ]; then
      chmod +x "$DT_BIN"; $SUDO ln -sf "$DT_BIN" /usr/local/bin/dovi_tool
      ok "dovi_tool -> /usr/local/bin/dovi_tool"
    else warn "dovi_tool binary not found after extract."; fi
  else warn "Couldn't resolve the dovi_tool linux release URL."; fi
fi

# --- 3. seconv (SubtitleEdit CLI) — provided, or built with the .NET SDK -------
install_seconv() {
  if command -v seconv >/dev/null 2>&1; then ok "seconv already on PATH: $(command -v seconv)"; return; fi
  if [ -n "$SECONV" ] && [ -x "$SECONV" ]; then
    $SUDO ln -sf "$SECONV" /usr/local/bin/seconv; ok "seconv -> $SECONV (provided)"; return
  fi
  info "Building seconv (SubtitleEdit CLI)"
  if ! command -v dotnet >/dev/null 2>&1; then
    info "Installing the .NET SDK"
    $SUDO apt-get install -y dotnet-sdk-8.0 || {
      warn "dotnet-sdk-8.0 not available via apt on this release. Install the .NET SDK"
      warn "(https://dotnet.microsoft.com/download), or copy a built seconv from a"
      warn "worker and re-run with --seconv /path/to/seconv. Skipping seconv."
      return; }
  fi
  local src="$TOOLS_DIR/subtitleedit-cli"
  [ -d "$src" ] || git clone --depth 1 https://github.com/SubtitleEdit/subtitleedit-cli.git "$src"
  local csproj; csproj="$(find "$src" -name seconv.csproj | head -1)"
  if [ -z "$csproj" ]; then warn "seconv.csproj not found in the clone."; return; fi
  dotnet publish "$csproj" -c Release -o "$TOOLS_DIR/seconv-bin"
  local dll="$TOOLS_DIR/seconv-bin/seconv.dll"
  if [ -f "$dll" ]; then
    # A tiny wrapper so it's a plain `seconv` on PATH, run via the .NET runtime.
    printf '#!/usr/bin/env bash\nexec dotnet "%s" "$@"\n' "$dll" | $SUDO tee /usr/local/bin/seconv >/dev/null
    $SUDO chmod +x /usr/local/bin/seconv
    ok "seconv -> /usr/local/bin/seconv (dotnet $dll)"
  else warn "seconv build produced no seconv.dll."; fi
}
install_seconv

# --- 4. MakeMKV (only for a Linux RIP box; workers don't need it) -------------
if [ "$WITH_MAKEMKV" -eq 1 ]; then
  warn "MakeMKV on Linux has no apt package — it's compiled from the makemkv-oss +"
  warn "makemkv-bin tarballs on the MakeMKV forum (needs build-essential, libssl-dev,"
  warn "libexpat1-dev, qtbase5-dev, zlib1g-dev, and ffmpeg dev libs). Follow:"
  warn "  https://forum.makemkv.com/forum/viewtopic.php?t=224  — then it's on PATH as makemkvcon."
else
  info "Skipping MakeMKV (workers don't rip). Use --with-makemkv for a Linux rip box."
fi

# --- verify -------------------------------------------------------------------
info "Verifying with 'blurayripper check'"
if command -v blurayripper >/dev/null 2>&1; then
  ( cd "$REPO_ROOT" && blurayripper check ) || true
else
  warn "'blurayripper' not on PATH — run 'pip install -e .' in $REPO_ROOT, then 'blurayripper check'."
fi
info "For a worker, also set BLURAYRIPPER_DATABASE_URL in $REPO_ROOT/config/secrets.env."
