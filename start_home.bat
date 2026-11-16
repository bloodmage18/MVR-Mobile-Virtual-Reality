@echo off
rem Home Space: starts the home app and streams it to the phone in the headset.
rem   start_home.bat                    (home app + bridge, ~50 fps window capture)
rem   start_home.bat --encoder x264     (extra options go to the bridge)
rem Also starts the headless media service (video, browser) in its own minimised window.
cd /d "%~dp0"
set GODOT=E:\Godot\Godot_v4.7-stable_win64.exe\Godot_v4.7-stable_win64.exe
if not exist "%GODOT%" (
  echo Godot not found at %GODOT% - edit start_home.bat to point at your Godot 4.7 exe
  pause
  exit /b 1
)
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

start "media service" /min "%PY%" home_space\service\media_service.py
start "" "%GODOT%" --path home_space\godot
rem Give the window a moment to appear so the bridge can find it
timeout /t 4 /nobreak >nul
"%PY%" bridge\hmd_bridge.py --adb --open / --video game --title "Cross Reality Home" %*
pause
