@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv311\Scripts\python.exe" (
    ".venv311\Scripts\python.exe" main.py %*
) else (
    py -3.11 main.py %*
)
if errorlevel 1 pause
