@echo off
REM Argus market-open routine (run weekdays at 6:30 AM PT by the "Argus Market Open" task).
REM 1. Launch Argus (dashboard) in the background   2. Run the pipeline for fresh signals
REM
REM It does NOT change the agent mode (off/paper/live) - that is set only from the Agent tab, so the
REM morning task can never switch real-money trading on. The every-20-min "Argus Options Agent" task
REM trades on today's fresh read in whatever mode is set. Uses scripts\run_pipeline.py (headless;
REM writes pipeline_cache.json) - NOT main.py, which prompts for a budget and would hang forever in
REM this hidden window. Holidays: the pipeline skips itself. Output: market_open.log

cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set LOG=%~dp0market_open.log

echo ==== %DATE% %TIME% market-open routine ==== >> "%LOG%"

REM 1. Launch Argus with no terminal window (background). Reuses a running instance.
start "" wscript.exe "%~dp0argus_silent.vbs"

REM 2. Fresh pipeline signals for today (writes pipeline_cache.json the agent reads). --crypto adds 5 crypto
REM    stories on top of the 15 stock ones, so the agent's crypto leg has signals to act on.
"%~dp0venv\Scripts\python.exe" "%~dp0scripts\run_pipeline.py" --crypto >> "%LOG%" 2>&1
if errorlevel 1 echo PIPELINE FAILED - the agent only trades today's signals, so it stays idle >> "%LOG%"

echo Argus market-open routine complete.
