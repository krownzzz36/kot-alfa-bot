@echo off
chcp 65001 >nul
title Пульт Холопа
cd /d "%~dp0"

set "LOG=%~dp0startup_log.txt"
echo === Запуск %date% %time% === > "%LOG%"

echo(
echo   ============================
echo        ПУЛЬТ ХОЛОПА
echo   ============================
echo(

rem ---------- 1. ищем РАБОЧИЙ Python ----------
rem (проверяем реальным запуском -c, чтобы отсеять фейковый ярлык из Microsoft Store)
set "PYCMD="
for %%P in ("py -3" "py" "python" "python3") do (
  if not defined PYCMD (
    %%~P -c "import sys" >>"%LOG%" 2>&1
    if not errorlevel 1 set "PYCMD=%%~P"
  )
)

if not defined PYCMD (
  echo [X] Рабочий Python не найден.
  echo     ^(Возможно, стоит "заглушка" из Microsoft Store — она не годится.^)
  echo(
  echo   Как починить:
  echo     1. Открой  https://www.python.org/downloads/
  echo     2. Скачай и запусти установщик.
  echo     3. ВНИЗУ первого окна поставь галочку "Add python.exe to PATH".
  echo     4. Нажми Install Now, дождись конца.
  echo     5. Запусти START.bat снова.
  echo(
  echo PYTHON NOT FOUND >> "%LOG%"
  pause
  exit /b 1
)
echo [ok] Python: %PYCMD% >> "%LOG%"
echo   Python найден: %PYCMD%

rem ---------- 2. библиотека Telegram (telethon) ----------
%PYCMD% -c "import telethon" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo   Первый запуск: ставлю библиотеку Telegram, подожди ~минуту...
  %PYCMD% -m pip install --user telethon >>"%LOG%" 2>&1
  if errorlevel 1 %PYCMD% -m pip install telethon >>"%LOG%" 2>&1
  %PYCMD% -c "import telethon" >>"%LOG%" 2>&1
  if errorlevel 1 (
    echo(
    echo [X] Не смог установить библиотеку telethon.
    echo     Подробности в файле startup_log.txt рядом.
    echo     Проверь интернет и запусти START.bat снова.
    pause
    exit /b 1
  )
)
echo [ok] telethon >> "%LOG%"
echo   Библиотека Telegram на месте.

rem ---------- 3. запуск пульта ----------
echo(
echo   --------------------------------------------
echo    Запускаю пульт. Браузер откроется сам.
echo    Если нет — открой:  http://127.0.0.1:8777/
echo(
echo    ЭТО ОКНО НЕ ЗАКРЫВАЙ (свернуть можно).
echo    Закрыть окно = остановить пульт и ботов.
echo   --------------------------------------------
echo(
echo START HUB >> "%LOG%"

%PYCMD% holop_hub.py 2>>"%LOG%"

echo(
echo   Пульт остановлен.
echo   Если что-то пошло не так — пришли файлы  startup_log.txt  и  hub_error.log
pause
