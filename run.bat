@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "ClassIn Exporter" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run.py"
    exit /b 0
)
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0run.py"
    exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 "%~dp0run.py"
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0run.py"
    exit /b
)
echo Python 3.11 or newer was not found. Install Python and project dependencies.
pause
exit /b 1
