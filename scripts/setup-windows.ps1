<#
.SYNOPSIS
    Download + install the external tools BluRayRipper needs on a Windows box
    (serve box or worker), then wire their paths into config\secrets.env and run
    `blurayripper check` to verify.

.DESCRIPTION
    Automates the open-source, portable tools fully (ffmpeg, dovi_tool,
    MKVToolNix, HandBrakeCLI) and the installer-based ones silently (VC++
    runtime, Tesseract). Two tools need a manual touch and are downloaded but not
    silently installed:
      * MakeMKV  — proprietary; run its installer and enter the (free) beta key.
      * seconv   — the SubtitleEdit CLI; built from source with the .NET SDK, or
                   pass -SeconvExe to point at one you already have (e.g. copied
                   from another worker).

    Re-runnable: it skips a tool whose files are already present. Existing keys in
    secrets.env (DATABASE_URL, TMDB, etc.) are preserved — only the tool-path keys
    are updated.

.PARAMETER ToolsDir
    Where the portable tools are unpacked. Default C:\brr-tools.

.PARAMETER SeconvExe
    Path to an existing seconv.exe to use instead of building it.

.PARAMETER Langs
    Extra Tesseract language codes to install beyond 'eng' (e.g. pol,deu).

.EXAMPLE
    # From the repo root, in an ELEVATED (admin) PowerShell:
    powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1 -Langs pol
#>
[CmdletBinding()]
param(
    [string]$ToolsDir = "C:\brr-tools",
    [string]$SeconvExe = "",
    [string]$Langs = "",
    [switch]$SkipMakeMkv
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # much faster Invoke-WebRequest downloads

# --- pinned versions for tools whose URL embeds the version (bump as needed) ---
$TesseractVer  = "5.5.0.20241111"
$MkvToolNixVer = "88.0"
$MakeMkvVer    = "1.18.1"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DownloadDir = Join-Path $ToolsDir "_downloads"
New-Item -ItemType Directory -Force -Path $ToolsDir, $DownloadDir | Out-Null
$Resolved = [ordered]@{}   # env key -> resolved path, written to secrets.env at the end

function Info($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  + $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  ! $m" -ForegroundColor Yellow }

function Get-File($url, $outFile) {
    if (Test-Path $outFile) { return $outFile }
    Info "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $outFile -UseBasicParsing
    return $outFile
}

# Newest release asset from a GitHub repo whose name matches a wildcard pattern.
function Get-GhAssetUrl($repo, $pattern) {
    $rel = Invoke-RestMethod -UseBasicParsing -Headers @{ "User-Agent" = "brr-setup" } `
        -Uri "https://api.github.com/repos/$repo/releases/latest"
    $asset = $rel.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1
    if (-not $asset) { throw "No asset matching '$pattern' in $repo latest release." }
    return $asset.browser_download_url
}

function Expand-To($zip, $dest) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Expand-Archive -Path $zip -DestinationPath $dest -Force
}

# Find the first matching .exe under a folder (portable tools nest under a versioned dir).
function Find-Exe($root, $name) {
    Get-ChildItem -Path $root -Recurse -Filter $name -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}

# ---------------------------------------------------------------------------
# 1. Microsoft Visual C++ runtime (dovi_tool / MKVToolNix are MSVC-linked).
# ---------------------------------------------------------------------------
function Install-VcRedist {
    Info "Visual C++ x64 runtime"
    $exe = Get-File "https://aka.ms/vs/17/release/vc_redist.x64.exe" (Join-Path $DownloadDir "vc_redist.x64.exe")
    Start-Process -Wait -FilePath $exe -ArgumentList "/install", "/quiet", "/norestart"
    Ok "VC++ runtime installed."
}

# ---------------------------------------------------------------------------
# 2. ffmpeg (UHD/HDR encode + grain preview) — BtbN nightly, stable 'latest' URL.
# ---------------------------------------------------------------------------
function Install-Ffmpeg {
    Info "ffmpeg"
    $dir = Join-Path $ToolsDir "ffmpeg"
    $exe = Find-Exe $dir "ffmpeg.exe"
    if (-not $exe) {
        $zip = Get-File "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip" (Join-Path $DownloadDir "ffmpeg.zip")
        Expand-To $zip $dir
        $exe = Find-Exe $dir "ffmpeg.exe"
    }
    if ($exe) { $Resolved["BLURAYRIPPER_FFMPEG"] = $exe; Ok $exe } else { Warn "ffmpeg.exe not found after extract." }
}

# ---------------------------------------------------------------------------
# 3. dovi_tool (Dolby Vision preservation) — GitHub release.
# ---------------------------------------------------------------------------
function Install-DoviTool {
    Info "dovi_tool"
    $dir = Join-Path $ToolsDir "dovi_tool"
    $exe = Find-Exe $dir "dovi_tool.exe"
    if (-not $exe) {
        $url = Get-GhAssetUrl "quietvoid/dovi_tool" "*x86_64-pc-windows-msvc.zip"
        $zip = Get-File $url (Join-Path $DownloadDir "dovi_tool.zip")
        Expand-To $zip $dir
        $exe = Find-Exe $dir "dovi_tool.exe"
    }
    if ($exe) { $Resolved["BLURAYRIPPER_DOVITOOL"] = $exe; Ok $exe } else { Warn "dovi_tool.exe not found." }
}

# ---------------------------------------------------------------------------
# 4. HandBrakeCLI (<=1080p encodes) — GitHub release.
# ---------------------------------------------------------------------------
function Install-HandBrake {
    Info "HandBrakeCLI"
    $dir = Join-Path $ToolsDir "handbrake"
    $exe = Find-Exe $dir "HandBrakeCLI.exe"
    if (-not $exe) {
        $url = Get-GhAssetUrl "HandBrake/HandBrake" "HandBrakeCLI-*-win-x86_64.zip"
        $zip = Get-File $url (Join-Path $DownloadDir "handbrakecli.zip")
        Expand-To $zip $dir
        $exe = Find-Exe $dir "HandBrakeCLI.exe"
    }
    if ($exe) { $Resolved["BLURAYRIPPER_HANDBRAKECLI"] = $exe; Ok $exe } else { Warn "HandBrakeCLI.exe not found." }
}

# ---------------------------------------------------------------------------
# 5. MKVToolNix (mkvmerge + mkvextract) — portable 7z isn't zip, so use the
#    silent NSIS installer into Program Files.
# ---------------------------------------------------------------------------
function Install-MkvToolNix {
    Info "MKVToolNix $MkvToolNixVer"
    $merge = "C:\Program Files\MKVToolNix\mkvmerge.exe"
    if (-not (Test-Path $merge)) {
        $url = "https://mkvtoolnix.download/windows/releases/$MkvToolNixVer/mkvtoolnix-64-bit-$MkvToolNixVer-setup.exe"
        $exe = Get-File $url (Join-Path $DownloadDir "mkvtoolnix-setup.exe")
        Start-Process -Wait -FilePath $exe -ArgumentList "/S"   # NSIS silent
    }
    if (Test-Path $merge) {
        $Resolved["BLURAYRIPPER_MKVMERGE"]   = $merge
        $Resolved["BLURAYRIPPER_MKVEXTRACT"] = "C:\Program Files\MKVToolNix\mkvextract.exe"
        Ok "MKVToolNix installed."
    } else { Warn "mkvmerge not found — the version URL may be stale; bump `$MkvToolNixVer." }
}

# ---------------------------------------------------------------------------
# 6. Tesseract (seconv OCR engine) — silent installer + PATH + language data.
# ---------------------------------------------------------------------------
function Install-Tesseract {
    Info "Tesseract $TesseractVer"
    $tessDir = "C:\Program Files\Tesseract-OCR"
    $exe = Join-Path $tessDir "tesseract.exe"
    if (-not (Test-Path $exe)) {
        $url = "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-$TesseractVer.exe"
        $inst = Get-File $url (Join-Path $DownloadDir "tesseract-setup.exe")
        Start-Process -Wait -FilePath $inst -ArgumentList "/S"   # NSIS silent (installs 'eng')
    }
    if (-not (Test-Path $exe)) { Warn "tesseract.exe not found — bump `$TesseractVer or install manually."; return }

    # seconv shells out to a SYSTEM tesseract — make sure it's on the machine PATH.
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($machinePath -notlike "*Tesseract-OCR*") {
        [Environment]::SetEnvironmentVariable("Path", "$machinePath;$tessDir", "Machine")
        Ok "Added Tesseract to the machine PATH (restart serve/worker to pick it up)."
    }

    # Extra language packs beyond eng (traineddata straight into tessdata).
    foreach ($lang in ($Langs -split "[, ]" | Where-Object { $_ })) {
        $td = Join-Path $tessDir "tessdata\$lang.traineddata"
        if (-not (Test-Path $td)) {
            try {
                Get-File "https://github.com/tesseract-ocr/tessdata_best/raw/main/$lang.traineddata" $td | Out-Null
                Ok "Tesseract language '$lang' installed."
            } catch { Warn "Could not fetch Tesseract language '$lang'." }
        }
    }
    Ok "$exe"
}

# ---------------------------------------------------------------------------
# 7. seconv (SubtitleEdit CLI) — use -SeconvExe if given, else build with dotnet.
# ---------------------------------------------------------------------------
function Install-Seconv {
    Info "seconv (SubtitleEdit CLI)"
    if ($SeconvExe -and (Test-Path $SeconvExe)) {
        $Resolved["BLURAYRIPPER_SUBTITLEEDIT"] = $SeconvExe; Ok "$SeconvExe (provided)"; return
    }
    $dir = Join-Path $ToolsDir "subtitleedit-cli"
    $existing = Find-Exe $dir "seconv.exe"
    if ($existing) { $Resolved["BLURAYRIPPER_SUBTITLEEDIT"] = $existing; Ok "$existing"; return }

    if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
        Warn "seconv needs the .NET SDK to build (https://dotnet.microsoft.com/download)."
        Warn "Install it, or copy seconv.exe from another worker and re-run with -SeconvExe <path>."
        return
    }
    if (-not (Test-Path $dir)) {
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Warn "git not on PATH — can't clone subtitleedit-cli."; return }
        git clone --depth 1 https://github.com/SubtitleEdit/subtitleedit-cli.git $dir
    }
    $csproj = Get-ChildItem -Path $dir -Recurse -Filter "seconv.csproj" | Select-Object -First 1
    if (-not $csproj) { Warn "seconv.csproj not found in the clone."; return }
    dotnet build $csproj.FullName -c Release
    $built = Find-Exe $dir "seconv.exe"
    if ($built) { $Resolved["BLURAYRIPPER_SUBTITLEEDIT"] = $built; Ok "$built" }
    else { Warn "seconv build produced no seconv.exe." }
}

# ---------------------------------------------------------------------------
# 8. MakeMKV (rip) — proprietary; download the installer, guide the manual step.
# ---------------------------------------------------------------------------
function Install-MakeMkv {
    if ($SkipMakeMkv) { return }
    Info "MakeMKV $MakeMkvVer"
    $con = "C:\Program Files (x86)\MakeMKV\makemkvcon64.exe"
    if (Test-Path $con) { $Resolved["BLURAYRIPPER_MAKEMKVCON"] = $con; Ok "$con"; return }
    $url = "https://www.makemkv.com/download/makemkv_setup_$MakeMkvVer.exe"
    try {
        $inst = Get-File $url (Join-Path $DownloadDir "makemkv-setup.exe")
        Warn "MakeMKV is proprietary — launching its installer; accept the licence, then finish setup."
        Start-Process -Wait -FilePath $inst
        if (Test-Path $con) { $Resolved["BLURAYRIPPER_MAKEMKVCON"] = $con; Ok "$con" }
        else { Warn "makemkvcon64.exe not found after install — check the install path." }
        Warn "Enter the free beta key in MakeMKV (Help -> Register), from makemkv.com/forum."
    } catch {
        Warn "Couldn't download MakeMKV (version URL may be stale; bump `$MakeMkvVer). Get it from makemkv.com/download."
    }
}

# --- write resolved tool paths into config\secrets.env (preserving other keys) ---
function Save-Env {
    $file = Join-Path $RepoRoot "config\secrets.env"
    $lines = if (Test-Path $file) { @(Get-Content $file) } else { @() }
    foreach ($key in $Resolved.Keys) {
        $val = $Resolved[$key]
        $newLines = @()
        $found = $false
        foreach ($line in $lines) {
            if ($line -match "^\s*$([regex]::Escape($key))\s*=") { $newLines += "$key=$val"; $found = $true }
            else { $newLines += $line }
        }
        if (-not $found) { $newLines += "$key=$val" }
        $lines = $newLines
    }
    Set-Content -Path $file -Value $lines -Encoding UTF8
    Info "Wrote tool paths to $file"
}

# ------------------------------------ run ------------------------------------
Info "BluRayRipper Windows setup — tools go under $ToolsDir"
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Warn "Not elevated — installers (VC++, MKVToolNix, Tesseract, MakeMKV) will prompt or fail. Re-run as Administrator for a hands-off install."
}

Install-VcRedist
Install-Ffmpeg
Install-DoviTool
Install-HandBrake
Install-MkvToolNix
Install-Tesseract
Install-Seconv
Install-MakeMkv
Save-Env

Info "Done. Verifying with 'blurayripper check' (paths take effect in a NEW shell — PATH just changed)…"
try {
    & blurayripper check
} catch {
    Warn "Couldn't run 'blurayripper check' here (open a new shell so the updated PATH applies, then run it)."
}
Info "If anything shows red, install that one tool by hand and re-run this script."
