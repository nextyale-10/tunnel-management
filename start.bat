@echo off
cd /d "%~dp0"
where pythonw >nul 2>&1
if errorlevel 1 (
    echo Python with tkinter is required. Install Python and add it to PATH.
    pause
    exit /b 1
)
start "" pythonw "%~dp0ssh_tunnels.py" --gui %*
exit /b 0
