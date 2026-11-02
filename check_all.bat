@echo off
rem Read-only pre-start check (see check_all.py)
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
"%~dp0venv\Scripts\python.exe" "%~dp0check_all.py"
pause
