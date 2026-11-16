@echo off
rem Exports the school horror game (godot_project) as a web build into phone_host\game_build\
rem so phones can run it themselves: phone_host_test.bat, then https://<laptop-ip>:8443/game/
cd /d "%~dp0\.."
set GODOT=E:\Godot\Godot_v4.7-stable_win64.exe\Godot_v4.7-stable_win64.exe
if not exist phone_host\game_build mkdir phone_host\game_build
"%GODOT%" --headless --path godot_project --export-release "Web" ..\phone_host\game_build\index.html
copy /y phone_host\game_web\hmd_web.js phone_host\game_build\hmd_web.js >nul
echo Built phone_host\game_build\  (open https://<laptop-ip>:8443/game/ on the phone)
