@echo off
REM ============================================================================
REM Auto-restarting launcher for BluRayRipper `serve` on Windows.
REM
REM WHY A BATCH LOOP: the rip box's staging/output live on a MAPPED drive (Z:),
REM which only exists inside an interactive login session. A Windows Service or a
REM Task Scheduler task set to "run whether user is logged on or not" runs in
REM session 0, where Z: does NOT exist -> serve would crash with "FileNotFoundError
REM Z:\". So serve must run in your logged-in session; this script does that and
REM restarts serve if it ever exits.
REM
REM AUTO-START ON BOOT (survive the Proxmox blade / VM powering off & back on):
REM   1. Proxmox: set this VM to "Start at boot = Yes" (VM -> Options).
REM   2. Windows: enable auto sign-in for this user
REM        run `netplwiz`, untick "Users must enter a user name and password".
REM   3. Put a shortcut to THIS file in the Startup folder:
REM        press Win+R, type  shell:startup , press Enter, drop a shortcut there.
REM   Now: blade powers on -> VM boots -> user auto-signs-in (mapping Z:) ->
REM        this launcher starts serve. And if serve dies, it relaunches.
REM
REM To stop: close this window, or press Ctrl+C twice.
REM ============================================================================

REM Run from the repo root (this file lives in scripts\).
cd /d "%~dp0.."

:loop
echo(
echo [%date% %time%] Starting: blurayripper serve
blurayripper serve
echo [%date% %time%] serve exited (code %errorlevel%). Restarting in 5s... (Ctrl+C to stop)
timeout /t 5 /nobreak >nul
goto loop
