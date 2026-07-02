@echo off
cd /d D:\CS\Projects\stock-advisor
REM Force Python UTF-8 mode so pipeline print()s with symbols (->, stars, etc.)
REM never crash on a cp1252 console (was causing "0 recommendations").
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM If Argus is ALREADY running on 8501, reuse it: just open the existing instance
REM and exit. Prevents spawning a second server (orphan processes) and a blank tab.
netstat -ano | findstr ":8501" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Argus already running - opening the existing session.
    start "" http://localhost:8501
    exit /b
)

call venv\Scripts\activate
REM Let Streamlit open the browser ITSELF, once the server is actually ready — that's
REM ONE tab, and never the premature blank one. --server.headless=false forces the
REM auto-open even if a global streamlit config turned it off. (No manual `start`.)
streamlit run dashboard/app.py --server.headless=false
