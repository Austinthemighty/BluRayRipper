<#
.SYNOPSIS
    Run the BluRayRipper encode worker automatically at logon, and keep it up --
    the closest thing to a service that still works with a mapped Z: drive.

.DESCRIPTION
    Registers a Scheduled Task that launches scripts\run-worker-windows.bat when
    you sign in, and restarts it if it ever dies. That gives service-like
    behaviour (starts on boot, survives crashes, controllable from the command
    line) while running INSIDE your interactive session.

    WHY NOT A REAL SERVICE: the worker reads staging and writes output on the
    shared NAS via a MAPPED drive (Z:). Mapped drives are per-session, so a
    Windows Service -- or a task set to "run whether user is logged on or not" --
    lives in session 0 where Z: does not exist, and every job fails with
    "FileNotFoundError: Z:\". Same reason scripts\run-serve-windows.bat exists.

    For the same reason this task deliberately runs UNELEVATED: with UAC, an
    elevated process gets a different token that does NOT inherit drives you
    mapped normally, so "Run with highest privileges" would break Z: too.

    (Switch the worker's paths to a UNC path -- \\nas\share\... -- and a real
    service becomes possible; install it to run as your user account, not
    LocalSystem, so it carries NAS credentials.)

    TO SURVIVE A POWER-OFF you also need the machine to reach a logged-in
    desktop by itself:
      1. Proxmox (if this is a VM): VM -> Options -> Start at boot = Yes.
      2. Windows: enable auto sign-in -- run `netplwiz`, untick
         "Users must enter a user name and password".

.PARAMETER WorkerName
    Name this box shows as in the web UI's Workers panel. Defaults to the hostname.

.PARAMETER TaskName
    Scheduled Task name. Default "BluRayRipper Worker".

.PARAMETER Uninstall
    Remove the task instead of creating it.

.PARAMETER Status
    Show the task's state and last result, then exit.

.EXAMPLE
    # From the repo root, in a NORMAL (non-admin) PowerShell:
    powershell -ExecutionPolicy Bypass -File scripts\install-worker-task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-worker-task.ps1 -Status
    powershell -ExecutionPolicy Bypass -File scripts\install-worker-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$WorkerName = "",
    [string]$TaskName = "BluRayRipper Worker",
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"

function Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  + $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }

$repoRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot "run-worker-windows.bat"

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
    Warn "A worker already running in a console keeps going until you Ctrl+C it."
    exit 0
}

if (-not (Test-Path $launcher)) { throw "Can't find the launcher at $launcher" }

# `blurayripper` must resolve for the user this task runs as -- the task inherits
# that user's PATH, so catch a not-installed CLI now rather than at 3am.
$cli = Get-Command blurayripper -ErrorAction SilentlyContinue
if (-not $cli) {
    Warn "'blurayripper' isn't on PATH in this shell."
    Warn "Install it first (pip install -e .) -- the task would fail the same way."
}

$argument = "`"$launcher`""
if ($WorkerName) { $argument += " `"$WorkerName`"" }

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c $argument" -WorkingDirectory $repoRoot

# AtLogOn (not AtStartup): the worker needs the interactive session that maps Z:.
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

Info "Registering scheduled task '$TaskName'"
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Ok "Installed. It starts automatically the next time you sign in."

Write-Host ""
Info "Control it like a service"
Write-Host "  Start now : schtasks /run /tn `"$TaskName`""
Write-Host "  Stop      : schtasks /end /tn `"$TaskName`""
Write-Host "  Status    : powershell -File scripts\install-worker-task.ps1 -Status"
Write-Host "  Remove    : powershell -File scripts\install-worker-task.ps1 -Uninstall"
Write-Host ""
Warn "'schtasks /end' kills the worker outright, losing an in-progress encode"
Warn "(the job is reclaimed and re-encoded, so nothing is corrupted -- just time)."
Warn "To stop gracefully, press Ctrl+C in the worker's window: it finishes and"
Warn "places the current encode first. 'Update all workers' in the web UI also"
Warn "drains cleanly, then pulls and restarts."
Write-Host ""
Info "Don't forget, so it comes back after a power-off:"
Write-Host "  1. Proxmox (if a VM): VM -> Options -> Start at boot = Yes"
Write-Host "  2. Auto sign-in: run netplwiz, untick 'Users must enter a user name and password'"
