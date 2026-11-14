@echo off
rem Starts the phone link + bridge and streams the running Godot game window to the phone.
rem   1. Start the game first (F5 in Godot, or play_game.bat)
rem   2. start_hmd.bat                       (captures the game window, ~50 fps)
rem   Other options (added after, they override the defaults):
rem   start_hmd.bat --video test             (ffmpeg test pattern, no game needed)
rem   start_hmd.bat --video off              (head tracking only)
rem   start_hmd.bat --encoder x264           (if Intel Quick Sync fails)
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
"%PY%" bridge\hmd_bridge.py --adb --open / --video game %*
pause
