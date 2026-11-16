@echo off
rem Sign in ONCE: opens a normal Brave window with the VR browser's own profile.
rem Sign in to YouTube (and other sites), then close the window. Do not run the server at the same time.
title VR Browser sign-in
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Python314\python.exe"
"%PY%" "%~dp0server.py" --login
pause
