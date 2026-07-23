@echo off
REM Argus — run exit + entry alert checks once.
REM
REM Called by the "Argus Alert Checks" scheduled task (via run_checks_silent.vbs so
REM no window flashes). Safe to run manually to test.
REM
REM run_checks.py gates itself on US market hours (Eastern, DST-aware) and exits
REM immediately outside them, so this can be scheduled around the clock.

cd /d "%~dp0"

REM UTF-8 so alert prints with symbols never crash on a cp1252 console.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

"%~dp0venv\Scripts\python.exe" "%~dp0alerts\run_checks.py"
