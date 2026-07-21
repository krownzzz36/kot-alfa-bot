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
HOLOP_NO_BROWSER=1 nohup "$PY" "$DIR/holop_hub.py" >/dev/null 2>&1 &
sleep 1.5
open "http://127.0.0.1:8777/"
