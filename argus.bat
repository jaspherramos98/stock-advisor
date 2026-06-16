@echo off
cd /d D:\CS\Projects\stock-advisor
call venv\Scripts\activate
start http://localhost:8501
streamlit run dashboard/app.py