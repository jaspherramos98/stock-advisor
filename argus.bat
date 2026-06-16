@echo off
cd /d D:\CS\Projects\stock-advisor
REM Force Python UTF-8 mode so pipeline print()s with symbols (->, stars, etc.)
REM never crash on a cp1252 console (was causing "0 recommendations").
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
call venv\Scripts\activate
start http://localhost:8501
streamlit run dashboard/app.py