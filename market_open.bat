@echo off
REM Argus market-open routine (run weekdays at 6:30 AM PT by the "Argus Market Open" task).
REM 1. Launch Argus (dashboard) in the background   2. Run the pipeline for fresh signals
REM 3. Arm the agent (the every-20-min "Argus Options Agent" task trades LIVE on its next cycle).
REM
REM The pipeline runs BEFORE arming so the agent trades on today's fresh read. It uses
REM scripts\run_pipeline.py (headless; writes pipeline_cache.json) - NOT main.py, which prompts
REM for a budget and would hang forever in this hidden window. Holidays: the pipeline skips
REM itself and the agent's own market gate skips, so arming is harmless. Output: market_open.log

cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set LOG=%~dp0market_open.log

echo ==== %DATE% %TIME% market-open routine ==== >> "%LOG%"

REM 1. Launch Argus with no terminal window (background). Reuses a running instance.
start "" wscript.exe "%~dp0argus_silent.vbs"

REM 2. Fresh pipeline signals for today (writes pipeline_cache.json the agent reads).
"%~dp0venv\Scripts\python.exe" "%~dp0scripts\run_pipeline.py" >> "%LOG%" 2>&1
if errorlevel 1 echo PIPELINE FAILED - agent still armed; it only trades today's signals, so it stays idle >> "%LOG%"

REM 3. Arm the agent -> LIVE on the next agent cycle.
echo armed > "%~dp0agent_live.arm"
echo armed agent >> "%LOG%"

echo Argus market-open routine complete.
