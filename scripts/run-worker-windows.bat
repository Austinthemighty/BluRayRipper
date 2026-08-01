@echo off
REM ============================================================================
REM Auto-restarting launcher for BluRayRipper `worker` on Windows.
REM
REM WHY NOT A REAL WINDOWS SERVICE: the encode worker reads the ripped .mkv from
REM staging and writes the output to the shared NAS on a MAPPED drive (Z:), which
REM only exists inside an interactive login session. A Windows Service (or a Task
REM Scheduler task set to "run whether user is logged on or not") runs in session
REM 0, where Z: does NOT exist -> the worker fails every job with "FileNotFoundError
REM Z:\". So the worker must run in your logged-in session; this script does that
REM and restarts it if it ever exits.
REM
REM   (If you ever switch the worker's paths from Z:\... to a UNC path such as
REM    \\nas\share\..., a real service DOES become possible -- install it to run
REM    as your user account, not LocalSystem, so it carries NAS credentials.)
REM
REM AUTO-START ON BOOT: run scripts\install-worker-task.ps1 once. It registers a
REM scheduled task that launches this file at logon and restarts it on failure --
REM service-like behaviour, but inside the session where Z: exists. See that
REM script's header for the auto-sign-in step.
REM
REM Usage:  run-worker-windows.bat [worker-name]
REM   The optional name is how this box shows up in the web UI's Workers panel;
REM   it defaults to the machine's hostname.
REM
REM To drain and stop: press Ctrl+C once (finishes and PLACES the current encode,
REM   then exits), or Ctrl+C twice to abort immediately. Closing the window kills
REM   an in-progress encode -- the job is reclaimed and re-encoded, but the work
REM   so far is lost.
REM ============================================================================

REM Run from the repo root (this file lives in scripts\).
cd /d "%~dp0.."

REM Resolve the CLI WITHOUT relying on PATH. A scheduled task (and a double-click)
REM gets a plain environment with no venv activated, and the documented install is
REM `python -m venv .venv` -- so blurayripper.exe lives in .venv\Scripts and is
REM invisible unless you activated it. Prefer the venv, then fall back to PATH.
set "BRR="
if exist ".venv\Scripts\blurayripper.exe" set BRR=".venv\Scripts\blurayripper.exe"
if not defined BRR if exist "venv\Scripts\blurayripper.exe" set BRR="venv\Scripts\blurayripper.exe"
if not defined BRR if exist ".venv\Scripts\python.exe" set BRR=".venv\Scripts\python.exe" -m blurayripper
if not defined BRR if exist "venv\Scripts\python.exe" set BRR="venv\Scripts\python.exe" -m blurayripper
if not defined BRR (
  REM Parenthesised so && binds to `where`. Left bare, cmd treats && as a
  REM line-level separator and the set runs even when BRR is already the
  REM venv path -- silently clobbering it back to a bare PATH lookup.
  where blurayripper >nul 2>&1 && set BRR=blurayripper
)
if not defined BRR (
  echo(
  echo [ERROR] Can't find blurayripper.
  echo   Looked in .venv\Scripts\, venv\Scripts\, and on PATH, from:
  echo   %CD%
  echo   Create it with:  python -m venv .venv ^&^& .venv\Scripts\pip install -e ".[web]"
  echo Press any key to close.
  pause >nul
  exit /b 2
)

set "WORKER_NAME=%~1"
if defined WORKER_NAME (
  set "NAME_ARG=--name %WORKER_NAME%"
) else (
  set "NAME_ARG="
)

:loop
echo(
echo [%date% %time%] Starting: %BRR% worker %NAME_ARG%
%BRR% worker %NAME_ARG%
set "RC=%errorlevel%"

REM Exit codes that mean "you asked for this" -- don't fight the user by
REM restarting. 0 = drained cleanly, 130 = aborted with a second Ctrl+C,
REM 2 = missing tool / no database (a config problem a restart can't fix).
REM (An 'u'/web update re-execs the worker rather than returning here. On Windows
REM  that re-exec starts a fresh process and this one exits, so the launcher may
REM  stop while the updated worker keeps running -- it is still up, just no longer
REM  supervised by this window until the next logon restarts the task.)
if "%RC%"=="0"   ( echo [%date% %time%] Worker drained and exited cleanly. Stopping. & goto :eof )
if "%RC%"=="130" ( echo [%date% %time%] Worker aborted by Ctrl+C. Stopping. & goto :eof )
if "%RC%"=="2"   (
  echo [%date% %time%] Worker can't start ^(exit 2^) -- fix the error above, then rerun.
  echo Press any key to close.
  pause >nul
  goto :eof
)

echo [%date% %time%] worker exited (code %RC%). Restarting in 10s... (Ctrl+C to stop)
timeout /t 10 /nobreak >nul
goto loop
