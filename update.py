#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автообновление пульта из GitHub.
Тянет свежий код (репозиторий kot-alfa-bot), ставит поверх — но НЕ трогает
твоё личное: вход (config.json), списки целей, настройки, логи.
Запускается автоматически из START.bat / START-mac.command перед стартом.
Только стандартная библиотека Python (без сторонних пакетов).
"""
import io
import os
import ssl
import sys
import zipfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ZIP = "https://github.com/krownzzz36/kot-alfa-bot/archive/refs/heads/main.zip"


def _log(msg):
    """Печать в консоль + строка в update_log.txt (чтобы друг мог прислать, если обнова не доехала)."""
    print(msg)
    try:
        import time
        with open(os.path.join(HERE, "update_log.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg + "\n")
    except OSError:
        pass

# обновляем только код и документацию — НЕ трогаем личные файлы и запускалки
UPDATE_EXT = (".py", ".md")
UPDATE_EXACT = {"requirements.txt", ".gitattributes"}
# на всякий случай никогда не перезаписываем это (личное/данные/запуск)
NEVER = {"config.json", "smash_settings.json", "smash_targets.txt", "smash_control.txt",
         "smash_bench.txt", "smash_donate.txt", "raid_targets.txt", "scout_targets.txt",
         "nicks.txt", "START.bat", "START-mac.command"}


def _should_update(rel):
    if rel in NEVER or "/" in rel:      # только файлы в корне, не в подпапках
        return False
    return rel in UPDATE_EXACT or rel.endswith(UPDATE_EXT)


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "kot-alfa-updater"})
    try:
        return urllib.request.urlopen(req, timeout=25).read()
    except urllib.error.URLError:
        # python.org на Mac часто без корневых сертификатов → пробуем без проверки.
        # Код публичный, качается с github.com — риск минимальный.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=25, context=ctx).read()


def main():
    try:
        _log("🔄 Проверяю обновления на GitHub…")
        data = _fetch(REPO_ZIP)
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        _log(f"   ⚠️ обновление ПРОПУЩЕНО (нет связи/битый архив): {type(e).__name__}: {e}")
        return

    names = zf.namelist()
    if not names:
        return
    root = names[0].split("/")[0] + "/"     # напр. kot-alfa-bot-main/
    updated = 0
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith(root):
            continue
        rel = info.filename[len(root):]
        if not _should_update(rel):
            continue
        content = zf.read(info)
        dest = os.path.join(HERE, rel)
        old = None
        if os.path.exists(dest):
            try:
                with open(dest, "rb") as f:
                    old = f.read()
            except OSError:
                pass
        if old != content:
            try:
                with open(dest, "wb") as f:
                    f.write(content)
                updated += 1
            except OSError as e:
                _log(f"   ⚠️ не смог обновить {rel}: {e}")

    if updated:
        _log(f"✅ Обновлено файлов: {updated}. Запускаю свежую версию.")
    else:
        _log("✅ У тебя уже последняя версия.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log(f"   ⚠️ апдейтер упал целиком: {type(e).__name__}: {e}")
