@echo off
rem Phase 0: plug in the phone (USB debugging on), run this, follow the phone screen.
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
"%PY%" bridge\hmd_bridge.py --adb --sensors --open /check %*
pause
