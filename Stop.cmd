@echo off
cd /d "%~dp0"
python "%~dp0app.py" --stop %*
if errorlevel 1 pause
