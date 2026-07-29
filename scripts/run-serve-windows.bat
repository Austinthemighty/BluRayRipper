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
set "RC=%errorlevel%"

REM Exit 2 = a config problem (missing tool, bad settings) that a restart can't
REM fix -- stop and show it instead of spamming the same error every 5s. Every
REM other code restarts: serve runs under uvicorn, which handles Ctrl+C itself and
REM exits 0, and the 'u'/web restart re-execs, so 0 does NOT mean "stop serving".
REM To stop the loop: Ctrl+C, then answer Y to "Terminate batch job?".
if "%RC%"=="2" (
  echo [%date% %time%] serve can't start ^(exit 2^) -- fix the error above, then rerun.
  echo Press any key to close.
  pause >nul
  goto :eof
)

echo [%date% %time%] serve exited (code %RC%). Restarting in 5s... (Ctrl+C to stop)
timeout /t 5 /nobreak >nul
goto loop
