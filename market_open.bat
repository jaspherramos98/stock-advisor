@echo off
REM Argus market-open routine (run daily at 6:30 AM PT by the "Argus Market Open" task).
REM 1. Launch Argus (dashboard) in the background   2. Run the pipeline for fresh signals
REM 3. Arm the options agent (the every-20-min agent task flips to LIVE on its next cycle).
REM
REM The pipeline runs BEFORE arming so the agent trades on today's fresh read. On a
REM non-trading day the agent's own market gate skips, so arming is harmless.

cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM 1. Launch Argus with no terminal window (background). Reuses a running instance.
start "" wscript.exe "%~dp0argus_silent.vbs"

REM 2. Fresh pipeline signals for today (writes pipeline_cache.json the agent reads).
"%~dp0venv\Scripts\python.exe" "%~dp0main.py"

REM 3. Arm the agent -> LIVE on the next agent cycle.
echo armed > "%~dp0agent_live.arm"

echo Argus market-open routine complete.
