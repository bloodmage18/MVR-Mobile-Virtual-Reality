@echo off
rem Phone-hosted VR test: the PHONE renders the scene, this laptop only serves the files.
rem Open the https address it prints on the phone (same Wi-Fi). See phone_host\README.md
title Phone VR test server
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo Using %PY%
"%PY%" "%~dp0phone_host\server.py" %*
echo.
echo Server stopped (exit code %errorlevel%).
pause
