@echo off
rem Run the RobloxAutoPromo service in a console window (Ctrl+C to stop).
rem Double-click this file, or use scripts\install_windows_task.ps1 to run it hidden at logon.
setlocal
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
"%PY%" -m app service %*
if errorlevel 1 pause
endlocal
