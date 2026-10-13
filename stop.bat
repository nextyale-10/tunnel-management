@echo off
cd /d "%~dp0"
python "%~dp0ssh_tunnels.py" --stop
if errorlevel 1 pause
exit /b
