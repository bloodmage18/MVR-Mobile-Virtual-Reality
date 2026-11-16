@echo off
rem VR browser test: headless Brave on this laptop, streamed to the phone headset.
rem Open the https://...:8443/?k=... address it prints on the phone (same Wi-Fi).
rem Sign in to YouTube once first with vr_browser_test\login.bat. See vr_browser_test\README.md
title VR Browser test server
cd /d "%~dp0"
rem The web server needs fastapi + uvicorn + websockets + cryptography (the project .venv has them).
rem yt-dlp is run as a separate program from whichever Python has it (C:\Python314 here).
set "PY="
for %%P in ("%~dp0.venv\Scripts\python.exe" "C:\Python314\python.exe") do (
  if not defined PY if exist %%P (
    %%P -c "import fastapi, uvicorn, websockets, cryptography" >nul 2>&1 && set "PY=%%~P"
  )
)
if not defined PY (
  echo No Python with fastapi, uvicorn, websockets and cryptography was found.
  echo Install them with:  C:\Python314\python.exe -m pip install fastapi uvicorn websockets cryptography psutil
  pause
  exit /b 1
)
echo Using %PY%
"%PY%" "%~dp0vr_browser_test\server.py" %*
if errorlevel 1 (
  echo.
  echo The VR browser server stopped with an error ^(exit code %errorlevel%^).
  pause
)
