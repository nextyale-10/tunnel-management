@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python "%~dp0ssh_tunnels.py" --cli %*
pause
