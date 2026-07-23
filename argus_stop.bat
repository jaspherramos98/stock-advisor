@echo off
REM Argus — stop the background instance.
REM
REM When Argus is started via argus_silent.vbs there is no console window to close,
REM so this kills whatever is listening on the Streamlit port (8501). Also clears the
REM chatbot proxy on 8502 if it's still holding the port.

setlocal enabledelayedexpansion
set FOUND=0

for %%P in (8501 8502) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%P" ^| findstr "LISTENING"') do (
        echo Stopping process %%a on port %%P ...
        taskkill /PID %%a /F >nul 2>&1
        set FOUND=1
    )
)

if "!FOUND!"=="0" (
    echo Argus is not running ^(nothing listening on 8501/8502^).
) else (
    echo Argus stopped.
)

endlocal
pause
