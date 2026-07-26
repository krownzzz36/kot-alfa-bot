#!/bin/bash
# Пульт Холопа для macOS. Двойной клик.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
PY="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11"
[ -x "$PY" ] || PY="$(command -v python3.11 || command -v python3)"
if [ -z "$PY" ]; then
  echo "Python не найден. Установи с https://www.python.org/downloads/"
  read -p "Enter для выхода… " _; exit 1
fi
"$PY" -c "import telethon" 2>/dev/null || "$PY" -m pip install --user telethon
# автообновление из GitHub (вход и списки не трогает)
"$PY" "$DIR/update.py"
# ПОРТ этого аккаунта. Мультиоконность (несколько аккаунтов): в каждой папке —
# свой файл port.txt с числом (напр. 8778). Нет файла → 8777.
PORT=8777
[ -f "$DIR/port.txt" ] && PORT="$(tr -cd 0-9 < "$DIR/port.txt")"
[ -z "$PORT" ] && PORT=8777
# ВАЖНО: работающая панель держит СТАРЫЙ код в памяти. Гасим её перед стартом — но
# ТОЛЬКО хаб из ЭТОЙ папки (по полному пути), чтобы НЕ убить панель другого аккаунта.
# Боты (набеги и пр.) НЕ трогаются.
pkill -f "$DIR/holop_hub.py" 2>/dev/null
for _ in $(seq 1 20); do pgrep -f "$DIR/holop_hub.py" >/dev/null || break; sleep 0.3; done
HOLOP_NO_BROWSER=1 HOLOP_PORT="$PORT" nohup "$PY" "$DIR/holop_hub.py" >/dev/null 2>&1 &
sleep 1.5
open "http://127.0.0.1:$PORT/"
