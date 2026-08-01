<#
.SYNOPSIS
    Start BluRayRipper automatically at logon and keep it up -- the rip box
    (`serve`) or an encode `worker`. The closest thing to a service that still
    works with a mapped Z: drive.

.DESCRIPTION
    Registers a Scheduled Task that launches the matching launcher script when you
    sign in, and restarts it if it ever dies. That gives service-like behaviour
    (starts on boot, survives crashes, controllable from the command line) while
    running INSIDE your interactive session.

    WHY NOT A REAL SERVICE: staging and output live on the shared NAS via a MAPPED
    drive (Z:). Mapped drives are per-session, so a Windows Service -- or a task
    set to "run whether user is logged on or not" -- lives in session 0 where Z:
    does not exist, and every job fails with "FileNotFoundError: Z:\".

    For the same reason the task runs UNELEVATED: with UAC, an elevated process
    gets a different token that does NOT inherit drives you mapped normally, so
    "Run with highest privileges" would break Z: too.

    (Switch the paths to UNC -- \\nas\share\... -- and a real service becomes
    possible; install it to run as your user account, not LocalSystem, so it
    carries NAS credentials. On Linux none of this applies: use
    scripts/install-service-linux.sh for a real systemd service.)

    TO SURVIVE A POWER-OFF the machine also has to reach a logged-in desktop by
    itself:
      1. Proxmox (if this is a VM): VM -> Options -> Start at boot = Yes.
      2. Windows: enable auto sign-in -- run `netplwiz`, untick
         "Users must enter a user name and password".

.PARAMETER Role
    'serve' (the rip box + web UI) or 'worker' (an encode worker).

.PARAMETER WorkerName
    worker only: the name this box shows as in the web UI. Defaults to the hostname.

.PARAMETER TaskName
    Scheduled Task name. Defaults to "BluRayRipper Server"/"BluRayRipper Worker".

.PARAMETER Uninstall
    Remove the task instead of creating it.

.PARAMETER Status
    Show the task's state and last result, then exit.

.EXAMPLE
    # From the repo root, in a NORMAL (non-admin) PowerShell:
    powershell -ExecutionPolicy Bypass -File scripts\install-task-windows.ps1 -Role serve

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-task-windows.ps1 -Role worker
    powershell -ExecutionPolicy Bypass -File scripts\install-task-windows.ps1 -Role worker -Status
    powershell -ExecutionPolicy Bypass -File scripts\install-task-windows.ps1 -Role worker -Uninstall
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("serve", "worker")]
    [string]$Role,
    [string]$WorkerName = "",
    [string]$TaskName = "",
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"

function Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  + $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }

$repoRoot = Split-Path -Parent $PSScriptRoot
if ($Role -eq "serve") {
    $launcher = Join-Path $PSScriptRoot "run-serve-windows.bat"
    if (-not $TaskName) { $TaskName = "BluRayRipper Server" }
} else {
    $launcher = Join-Path $PSScriptRoot "run-worker-windows.bat"
    if (-not $TaskName) { $TaskName = "BluRayRipper Worker" }
}

if ($Status) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Warn "Task '$TaskName' is not installed."; exit 1 }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Info "Task '$TaskName'"
    Write-Host "  State        : $($t.State)"
    Write-Host "  Last run     : $($info.LastRunTime)"
    Write-Host "  Last result  : $($info.LastTaskResult)  (0 = ok, 267009 = currently running)"
    Write-Host "  Next run     : $($info.NextRunTime)"
    Write-Host ""
    Write-Host "  Start now : schtasks /run /tn `"$TaskName`"" -ForegroundColor DarkGray
    Write-Host "  Stop now  : schtasks /end /tn `"$TaskName`"" -ForegroundColor DarkGray
    exit 0
}

if ($Uninstall) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Warn "Task '$TaskName' isn't installed -- nothing to do."; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Ok "Removed the scheduled task '$TaskName'."
    Warn "An instance already running in a console keeps going until you Ctrl+C it."
    exit 0
}

if (-not (Test-Path $launcher)) { throw "Can't find the launcher at $launcher" }

# Check the CLI the same way the launcher does. PATH alone is the WRONG test: the
# documented install is `python -m venv .venv`, so blurayripper.exe sits in
# .venv\Scripts and is only on PATH while that venv is activated -- which a
# scheduled task never does. The launcher therefore prefers the venv, and so does
# this check, or it would cry wolf on a perfectly good install.
$cliPath = $null
foreach ($rel in @(".venv\Scripts\blurayripper.exe", "venv\Scripts\blurayripper.exe",
                   ".venv\Scripts\python.exe", "venv\Scripts\python.exe")) {
    $candidate = Join-Path $repoRoot $rel
    if (Test-Path $candidate) { $cliPath = $candidate; break }
}
if ($cliPath) {
    Ok "Found the CLI at $cliPath"
} elseif (Get-Command blurayripper -ErrorAction SilentlyContinue) {
    Ok "Found 'blurayripper' on PATH ($((Get-Command blurayripper).Source))"
    Warn "It is not in a .venv beside the repo, so the task depends on it staying"
    Warn "on the PATH of the account that runs it."
} else {
    Warn "Can't find blurayripper in $repoRoot\.venv\Scripts\, venv\Scripts\, or on PATH."
    Warn "The task would fail at logon. Create it first:"
    Warn "  python -m venv .venv"
    Warn "  .venv\Scripts\pip install -e `".[web]`""
}

$argument = "`"$launcher`""
if ($Role -eq "worker" -and $WorkerName) { $argument += " `"$WorkerName`"" }

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c $argument" -WorkingDirectory $repoRoot

# AtLogOn (not AtStartup): it needs the interactive session that maps Z:.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Interactive token = the session's mapped drives. NOT -RunLevel Highest: an
# elevated token doesn't inherit normally-mapped drives.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # 0 = run as long as it wants

Info "Registering scheduled task '$TaskName' ($Role)"
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Ok "Installed. It starts automatically the next time you sign in."

Write-Host ""
Info "Control it like a service"
Write-Host "  Start now : schtasks /run /tn `"$TaskName`""
Write-Host "  Stop      : schtasks /end /tn `"$TaskName`""
Write-Host "  Status    : powershell -File scripts\install-task-windows.ps1 -Role $Role -Status"
Write-Host "  Remove    : powershell -File scripts\install-task-windows.ps1 -Role $Role -Uninstall"
Write-Host ""
if ($Role -eq "worker") {
    Warn "'schtasks /end' kills the worker outright, losing an in-progress encode"
    Warn "(the job is reclaimed and re-encoded, so nothing is corrupted -- just time)."
    Warn "To stop gracefully, press Ctrl+C in its window: it finishes and places the"
    Warn "current encode first. 'Update all workers' in the web UI drains too, then"
    Warn "pulls and restarts."
} else {
    Warn "An in-flight rip/OCR is checkpointed, so stopping and starting resumes it"
    Warn "rather than starting over. Press 'u' in its window to pull + restart."
}
Write-Host ""
Info "Don't forget, so it comes back after a power-off:"
Write-Host "  1. Proxmox (if a VM): VM -> Options -> Start at boot = Yes"
Write-Host "  2. Auto sign-in: run netplwiz, untick 'Users must enter a user name and password'"
