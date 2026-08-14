@echo off
REM Argus - run one options-agent scheduler cycle.
REM Called by the "Argus Options Agent" task via run_agent_silent.vbs (hidden).
REM scripts\run_agent.py self-gates on US market hours and DRY vs LIVE (agent_live.arm).

cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

"%~dp0venv\Scripts\python.exe" "%~dp0scripts\run_agent.py"
