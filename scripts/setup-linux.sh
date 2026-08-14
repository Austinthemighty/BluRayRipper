#!/usr/bin/env bash
#
# Install the external tools BluRayRipper needs on a Linux box — a rip box, a
# worker, or an all-in-one — then run `blurayripper check`. Works for a fully
# Linux setup (nothing here needs Windows).
#
# Everything is installed onto PATH, and BluRayRipper auto-detects tools by name
# (makemkvcon, HandBrakeCLI, mkvmerge, mkvextract, ffmpeg, dovi_tool, seconv), so
# no tool paths need to be written to config. For clustering, also set the DB URL
# (BLURAYRIPPER_DATABASE_URL in config/secrets.env) on every box.
#
# MakeMKV is built from source (the only way it ships on Linux — no apt package).
# Skip it with --no-makemkv on an encode-only worker that never rips.
#
# Debian/Ubuntu (apt). Other distros: install the equivalents by hand.
#
# Usage (from the repo root):
#   ./scripts/setup-linux.sh [--langs pol,deu] [--seconv /path/to/seconv]
#                            [--no-makemkv] [--tools-dir /opt/brr-tools]
set -euo pipefail

# MakeMKV has no apt package; it's compiled from versioned tarballs. Bump this to
# the current release (see makemkv.com/download or the forum) if a download 404s.
MAKEMKV_VER="1.18.4"

LANGS=""
SECONV=""
WITH_MAKEMKV=1
TOOLS_DIR="/opt/brr-tools"
while [ $# -gt 0 ]; do
  case "$1" in
    --langs) LANGS="$2"; shift 2 ;;
    --seconv) SECONV="$2"; shift 2 ;;
    --no-makemkv) WITH_MAKEMKV=0; shift ;;
    --makemkv-version) MAKEMKV_VER="$2"; shift 2 ;;
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
# default-jre is for MakeMKV, not for us: it shells out to a JRE for BD-Java
# discs (menu-driven Blu-rays and most BD+ titles). Without one it does not fail
# outright — it rips a subset of the titles and logs "This disc requires Java
# runtime (JRE)" partway down, so the disc looks done and is quietly incomplete.
$SUDO apt-get install -y handbrake-cli mkvtoolnix ffmpeg tesseract-ocr $TESS_LANGS curl tar git default-jre
ok "HandBrakeCLI, mkvmerge/mkvextract, ffmpeg, tesseract, JRE installed (on PATH)."

# Default TOOLS_DIR is /opt/brr-tools, which needs root to create — but the
# unpack/build steps below run as the invoking user, so hand it over afterwards
# rather than building as root.
$SUDO mkdir -p "$TOOLS_DIR"
$SUDO chown "$(id -u):$(id -g)" "$TOOLS_DIR"

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
    # Don't hardcode a version: the SDK package name tracks the release (24.04
    # has dotnet-sdk-8.0, later ones don't), so ask apt what it actually has and
    # take the newest. Hardcoding 8.0 silently skipped seconv on Ubuntu 26.04.
    local sdk
    # Match the SDK package EXACTLY (dotnet-sdk-10.0). The search also returns
    # dotnet-sdk-10.0-source-built-artifacts, an internal build package which
    # sorts last and would otherwise be picked — installing it leaves no `dotnet`
    # on PATH, so seconv was skipped with no useful error.
    sdk="$(apt-cache --names-only search '^dotnet-sdk-[0-9]' 2>/dev/null \
           | awk '{print $1}' | grep -E '^dotnet-sdk-[0-9]+\.[0-9]+$' \
           | sort -V | tail -1)"
    if [ -n "$sdk" ]; then
      info "Installing the .NET SDK ($sdk)"
      $SUDO apt-get install -y "$sdk" || sdk=""
    fi
    if [ -z "$sdk" ] || ! command -v dotnet >/dev/null 2>&1; then
      warn "No .NET SDK available via apt on this release. Either install it"
      warn "(https://dotnet.microsoft.com/download), or copy a built seconv from"
      warn "another box and re-run with --seconv /path/to/seconv."
      warn "Skipping seconv — everything except subtitle OCR still works."
      return
    fi
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

# --- 4. MakeMKV (rip) — built from source; the only way it ships on Linux -----
build_makemkv() {
  # Build deps for the OSS component (libs + optional Qt GUI). makemkvcon itself
  # comes precompiled in the -bin tarball.
  $SUDO apt-get install -y build-essential pkg-config libc6-dev libssl-dev \
    libexpat1-dev libavcodec-dev libgl1-mesa-dev qtbase5-dev zlib1g-dev || return 1
  local d="$TOOLS_DIR/makemkv"
  mkdir -p "$d"
  ( set -e
    cd "$d"
    for part in oss bin; do
      tgz="makemkv-$part-$MAKEMKV_VER.tar.gz"
      [ -f "$tgz" ] || curl -fsSL "https://www.makemkv.com/download/$tgz" -o "$tgz"
      tar -xzf "$tgz"
    done
    ( cd "makemkv-oss-$MAKEMKV_VER" && ./configure && make && $SUDO make install )
    # The -bin component carries its own EULA; `make` accepts it (we feed "yes").
    ( cd "makemkv-bin-$MAKEMKV_VER" && echo yes | make && $SUDO make install )
  )
}

if [ "$WITH_MAKEMKV" -eq 1 ]; then
  if command -v makemkvcon >/dev/null 2>&1; then
    ok "makemkvcon already on PATH: $(command -v makemkvcon)"
  else
    info "Building MakeMKV $MAKEMKV_VER from source (oss + bin) — accepts the MakeMKV EULA"
    if build_makemkv && command -v makemkvcon >/dev/null 2>&1; then
      ok "makemkvcon -> $(command -v makemkvcon)"
    else
      warn "MakeMKV build failed. A curl 5xx (502/525) is makemkv.com being down —"
      warn "that is not your version, so just re-run this script later. A 404 means"
      warn "the version is stale: pass --makemkv-version <current>."
      warn "Manual guide: https://forum.makemkv.com/forum/viewtopic.php?t=224"
    fi
  fi
else
  info "Skipping MakeMKV (--no-makemkv) — encode-only worker."
fi

# --- verify -------------------------------------------------------------------
info "Verifying with 'blurayripper check'"
if command -v blurayripper >/dev/null 2>&1; then
  ( cd "$REPO_ROOT" && blurayripper check ) || true
else
  warn "'blurayripper' not on PATH — run 'pip install -e .' in $REPO_ROOT, then 'blurayripper check'."
fi
info "For a clustered setup, set BLURAYRIPPER_DATABASE_URL (same Postgres) in"
info "$REPO_ROOT/config/secrets.env on every box. A single all-in-one box can skip it."
