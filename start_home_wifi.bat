@echo off
rem Home Space over Wi-Fi (no cable): the laptop runs the library and streams it to the phone.
rem   1. The phone joins the same Wi-Fi as the laptop.
rem   2. Run this, then open the https://...:8444/?k=... address it prints, on the phone (the key is required).
rem   3. First time only: accept the certificate warning. Tap the start button, put the phone in the headset.
rem   Extra options go to the bridge, e.g.  start_home_wifi.bat --bitrate 5M   (if the picture stutters)
cd /d "%~dp0"
set GODOT=E:\Godot\Godot_v4.7-stable_win64.exe\Godot_v4.7-stable_win64.exe
if not exist "%GODOT%" (
  echo Godot not found at %GODOT% - edit start_home_wifi.bat to point at your Godot 4.7 exe
  pause
  exit /b 1
)
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

start "media service" /min "%PY%" home_space\service\media_service.py
start "" "%GODOT%" --path home_space\godot
timeout /t 4 /nobreak >nul
"%PY%" bridge\hmd_bridge.py --wifi --video game --title "Cross Reality Home" %*
pause
