@echo off
rem ASCII-only launcher: no codepage/encoding surprises on any Windows.
title Holop Panel
cd /d "%~dp0"

set "LOG=%~dp0startup_log.txt"
echo === start %date% %time% === > "%LOG%"

echo(
echo   ============================
echo         HOLOP PANEL
echo   ============================
echo(

rem ---------- 1. find a WORKING python ----------
rem (real run of -c filters out the fake Microsoft Store stub)
set "PYCMD="
for %%P in ("py -3" "py" "python" "python3") do (
  if not defined PYCMD (
    %%~P -c "import sys" >>"%LOG%" 2>&1
    if not errorlevel 1 set "PYCMD=%%~P"
  )
)

if not defined PYCMD (
  echo [X] Working Python not found.
  echo     ^(A Microsoft Store "stub" does not count.^)
  echo(
  echo   How to fix:
  echo     1. Open  https://www.python.org/downloads/
  echo     2. Run the installer.
  echo     3. TICK the box "Add python.exe to PATH" at the bottom.
  echo     4. Click Install Now, wait, then run START.bat again.
  echo(
  echo PYTHON NOT FOUND >> "%LOG%"
  pause
  exit /b 1
)
echo [ok] Python: %PYCMD% >> "%LOG%"
echo   Python found: %PYCMD%

rem ---------- 2. Telegram library (telethon) ----------
%PYCMD% -c "import telethon" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo   First run: installing Telegram library, please wait ~1 min...
  %PYCMD% -m pip install --user telethon >>"%LOG%" 2>&1
  if errorlevel 1 %PYCMD% -m pip install telethon >>"%LOG%" 2>&1
  %PYCMD% -c "import telethon" >>"%LOG%" 2>&1
  if errorlevel 1 (
    echo(
    echo [X] Could not install telethon. See startup_log.txt next to this file.
    echo     Check the internet connection and run START.bat again.
    pause
    exit /b 1
  )
)
echo [ok] telethon >> "%LOG%"
echo   Telegram library is ready.

rem ---------- 3. run the panel ----------
echo(
echo   --------------------------------------------
echo    Starting. The browser will open by itself.
echo    If not, open:  http://127.0.0.1:8777/
echo(
echo    DO NOT CLOSE THIS WINDOW (you can minimize it).
echo    Closing this window = stopping the panel and bots.
echo   --------------------------------------------
echo(
echo START HUB >> "%LOG%"

%PYCMD% holop_hub.py 2>>"%LOG%"

echo(
echo   Panel stopped.
echo   If something went wrong, send me:  startup_log.txt  and  hub_error.log
pause
