@echo off
rem Runs the game without the Godot editor (lighter on the laptop than F5 in the editor).
rem Then run start_hmd.bat to stream it to the phone.
cd /d "%~dp0"
set GODOT=E:\Godot\Godot_v4.7-stable_win64.exe\Godot_v4.7-stable_win64.exe
if not exist "%GODOT%" (
  echo Godot not found at %GODOT% - edit play_game.bat to point at your Godot 4.7 exe
  pause
  exit /b 1
)
start "" "%GODOT%" --path godot_project
