#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🏰 ХОЛОП — ЕДИНЫЙ ПУЛЬТ (веб-дашборд со вкладками)
Собирает все инструменты в одном окне браузера:
  ⚔️ Набеги · 🎭 Роли холопов · ⏰ Будильники · 🕳️ Пещеры · 📊 Статус (КД/щиты)
Кнопки и копипаст работают нормально (это веб-страница, а не Tkinter).
Запуск: двойной клик по «Холоп-Панель.command». Только стандартная библиотека Python.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
from concurrent.futures import TimeoutError as FuturesTimeout
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

# Windows-консоль (cp1251) рушит эмодзи в выводе — переводим в UTF-8 с заменой.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

VERSION = "2026.07.24-6"   # видно в консоли и в шапке панели — чтобы понимать, свежая ли версия
PY = sys.executable or "python3"
PORT = int(os.environ.get("HOLOP_PORT", "8777"))

IS_WIN = os.name == "nt"
# Как запускать дочерние скрипты: на Windows — без всплывающего чёрного окна;
# на macOS/Linux — в своей сессии, чтобы переживали закрытие пульта.
if IS_WIN:
    CREATE_NO_WINDOW = 0x08000000
    POPEN_KW = {"creationflags": CREATE_NO_WINDOW}
else:
    POPEN_KW = {"start_new_session": True}
# stdin обязательно валидный (DEVNULL): без окна консоли Python иначе падает с
# "Fatal Python error: init_sys_streams: can't initialize sys standard streams".
POPEN_KW["stdin"] = subprocess.DEVNULL
# ФОРСИРУЕМ UTF-8 у дочерних скриптов: на русской Windows (cp1251) Python иначе
# падает/мусорит на эмодзи в логах. PYTHONUTF8=1 включает UTF-8-режим на любом Python.
POPEN_KW["env"] = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def _pid_cmd(pid):
    """Командная строка процесса pid ('' если нет) — *nix. Чтобы отличить НАШ процесс
    от чужого, которому ОС переиспользовала тот же pid (иначе os.kill(pid,0) врёт «жив»)."""
    if not pid:
        return ""
    try:
        r = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                           capture_output=True, text=True, timeout=3)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _pid_alive(pid, needle=None):
    """Жив ли процесс — БЕЗ его убийства (на Windows os.kill(pid,0) убивает процесс!).
    needle (*nix) — проверить, что это ИМЕННО наш скрипт (защита от переиспользования pid)."""
    if not pid:
        return False
    if IS_WIN:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE   # (имя на Windows не проверяем — нет дешёвого способа)
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    if needle is None:
        return True
    return needle in _pid_cmd(pid)   # pid жив, но НАШ ли это процесс?


def _terminate(pid):
    """Мягко/жёстко погасить процесс (и его детей) кросс-платформенно."""
    if not pid:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        pass
NIGHT_SH = os.path.join(HERE, "night_smash.sh")   # ночной режим: caffeinate + сторож смашера
NIGHT_PID = os.path.join(HERE, "night.pid")

# ─────────────── конфиг + вход (сессия Telegram) ───────────────
# Общий «ключ приложения» по умолчанию. Друзьям НЕ нужен my.telegram.org —
# они входят по телефону + коду. При желании каждый может вписать свой api_id/api_hash
# в config.json (my.telegram.org → API development tools).
DEFAULT_API_ID = 35604443
DEFAULT_API_HASH = "a5ab4ff8c7dd0bc6e02bbbf15183168e"
CONFIG_PATH = os.path.join(HERE, "config.json")

_CFG_DEFAULTS = {
    "api_id": DEFAULT_API_ID, "api_hash": DEFAULT_API_HASH,
    "session_name": "holop_session", "bot_username": "holop",
    "target_profession": "Воин", "guard_when_done": True,
    "max_iterations": 40, "max_captures_per_session": 0,
    "min_delay": 0.8, "max_delay": 1.8,
    "fast_min_delay": 0.15, "fast_max_delay": 0.4,
    "dry_run": False, "allow_star_spend": False,
}


def load_cfg():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def ensure_config():
    """Первый запуск: создаёт config.json с настройками по умолчанию (без сессии)."""
    cfg = load_cfg()
    changed = False
    for k, v in _CFG_DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        save_cfg(cfg)
    return cfg


def is_authorized():
    return bool(load_cfg().get("session_string"))


class Auth:
    """Хранит один Telethon-клиент между запросами «отправить код» → «ввести код»."""
    def __init__(self):
        self.loop = None
        self.client = None
        self.phone = None
        self.phone_code_hash = None
        self.lock = threading.Lock()

    def _run(self, coro, timeout=60):
        """С ТАЙМАУТОМ: иначе подвисший Telethon держит HTTP-запрос вечно —
        у пользователя «жму получить код и ничего не происходит» (жалоба Karina)."""
        import asyncio
        if self.loop is None or self.loop.is_closed():
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=self.loop.run_forever, daemon=True).start()
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except FuturesTimeout:
            fut.cancel()
            raise RuntimeError(
                "Telegram не ответил за 60 секунд. Проверь интернет/VPN и попробуй ещё раз.")

    def send_code(self, phone, force_sms=False):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        cfg = load_cfg()
        api_id = int(cfg.get("api_id") or DEFAULT_API_ID)
        api_hash = cfg.get("api_hash") or DEFAULT_API_HASH
        phone = phone.strip().replace(" ", "")

        async def _go():
            # при повторной отправке (SMS) переиспользуем тот же клиент — иначе
            # Telegram не даст сменить способ доставки
            if not (force_sms and self.client and self.phone == phone):
                if self.client:
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
                self.client = TelegramClient(StringSession(), api_id, api_hash)
                await self.client.connect()
            sent = await self.client.send_code_request(phone, force_sms=force_sms)
            self.phone = phone
            self.phone_code_hash = sent.phone_code_hash
            return _where_code_went(sent)

        with self.lock:
            return self._run(_go())

    def sign_in(self, code=None, password=None):
        """Возвращает 'ready' (вошли, сессия сохранена) или 'password' (нужен 2FA)."""
        from telethon.errors import SessionPasswordNeededError
        if self.client is None:
            raise RuntimeError("Сначала запроси код (номер телефона).")

        async def _go():
            if password is not None:
                await self.client.sign_in(password=password)
            else:
                await self.client.sign_in(self.phone, code,
                                          phone_code_hash=self.phone_code_hash)

        with self.lock:
            try:
                self._run(_go())
            except SessionPasswordNeededError:
                return "password"
            ss = self.client.session.save()
        cfg = load_cfg()
        cfg["session_string"] = ss
        save_cfg(cfg)
        return "ready"


AUTH = Auth()


def _where_code_went(sent):
    """Понятным языком: КУДА Telegram отправил код. Главная причина жалоб
    «код не приходит» — его шлют СООБЩЕНИЕМ В САМ TELEGRAM, а люди ждут SMS."""
    name = type(getattr(sent, "type", None)).__name__
    if "App" in name:
        return ("app", "Код отправлен СООБЩЕНИЕМ В TELEGRAM — открой Telegram на телефоне "
                       "и найди чат «Telegram» (обычно самый верхний). SMS не будет!")
    if "Sms" in name:
        return ("sms", "Код отправлен по SMS на твой номер.")
    if "Call" in name:
        return ("call", "Telegram позвонит и продиктует код голосом.")
    if "Email" in name:
        return ("email", "Код отправлен на привязанную почту.")
    return ("app", "Код отправлен. Проверь чат «Telegram» в приложении и SMS.")


def _log_auth_error(e):
    """Пишем сбои входа в auth_log.txt: у друзей «код не приходит» без деталей —
    по этому файлу видно настоящую причину (FloodWait, сеть, api_id и т.д.)."""
    import traceback
    try:
        with open(path("auth_log.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " +
                    f"{type(e).__name__}: {e}\n" + traceback.format_exc() + "\n")
    except OSError:
        pass


def _auth_err(e):
    name = type(e).__name__
    s = str(e)
    if "PhoneCodeInvalid" in name:
        return "Неверный код. Проверь и введи заново."
    if "PhoneCodeExpired" in name:
        return "Код устарел — запроси новый."
    if "PhoneNumberInvalid" in name:
        return "Неверный номер. Формат: +79991234567."
    if "PhoneNumberBanned" in name:
        return "Этот номер заблокирован в Telegram."
    if "PasswordHashInvalid" in name or "Password" in name and "Invalid" in name:
        return "Неверный пароль двухфакторной защиты."
    if "FloodWait" in name:
        return f"Слишком много попыток. Подожди перед повтором ({s})."
    if "ApiId" in name or "api_id" in s.lower():
        return "Ключ приложения (api_id/api_hash) не принят Telegram."
    return s or name

# ─────────────── описание модулей (и для сервера, и для UI) ───────────────
PROFS = ["Воин", "Ополченец", "Пахарь", "Волхв", "Ремесленник", "Зодчий", "Лазутчик"]

MODULES = [
    {
        "id": "raids", "title": "Набеги", "emoji": "⚔️", "kind": "loop",
        "script": "holop_smash.py", "log": "smash.log",
        "control": "smash_control.txt", "targets": "smash_targets.txt",
        "desc": "Авто-бой по списку + защита от бочки. Список правится на лету.",
    },
    {
        "id": "roles", "title": "Роли холопов", "emoji": "🎭", "kind": "oneshot",
        "script": "holop_reroll.py", "log": "hub_roles.out",
        "desc": "Перегон холопа в нужную профессию (крутит выгнать→захватить).",
        "fields": [{"id": "nick", "label": "Ники холопов (по одному в строке)",
                    "kind": "textarea", "rows": 6, "placeholder": "Яр\nЖёлудь"}],
        "selects": [{"id": "prof", "label": "Профессия", "options": PROFS, "default": "Воин"},
                    {"id": "auto_defrog", "label": "Авто-разжаб (снять охрану зельем жаб из запаса)",
                     "options": ["Нет", "Да"], "default": "Нет"}],
        "actions": [{"id": "run", "label": "▶ Перегнать"},
                    {"id": "check", "label": "🔍 Проверить"},
                    {"id": "dry", "label": "Холостой (dry-run)"}],
    },
    {
        "id": "alarms", "title": "Будильники", "emoji": "⏰", "kind": "oneshot",
        "script": "holop_alarms.py", "log": "hub_alarms.out",
        "desc": "Отложенные мигалки в чат к моменту спадения охраны холопов.",
        "fields": [{"id": "lead", "label": "За сколько минут до спадения щита",
                    "kind": "number", "default": 5}],
        "actions": [{"id": "run", "label": "⏰ Поставить будильники"},
                    {"id": "clear", "label": "🧹 Снять все"},
                    {"id": "dry", "label": "Холостой (dry-run)"}],
    },
    {
        "id": "caves", "title": "Пещеры", "emoji": "🕳️", "kind": "oneshot",
        "script": "holop_caves.py", "log": "hub_caves.out",
        "desc": "Автопроход трёх пещер по очереди до стоп-уровня.",
        "fields": [{"id": "min_win", "label": "Мин. шанс победы, %", "kind": "number", "default": 85},
                   {"id": "min_hp", "label": "Мин. HP", "kind": "number", "default": 20},
                   {"id": "max_level", "label": "Стоп-уровень", "kind": "number", "default": 10},
                   {"id": "only", "label": "Только пещера (подстрока, необязательно)",
                    "kind": "text", "default": ""}],
        "actions": [{"id": "run", "label": "🕳️ Пройти пещеры"},
                    {"id": "dry", "label": "Холостой (dry-run)"}],
    },
    {
        "id": "find", "title": "Найти цели", "emoji": "🔎", "kind": "oneshot",
        "script": "holop_raid.py", "log": "hub_find.out",
        "result_file": "raid_targets.txt",   # найденные ники (столбиком) — для поля результата
        "result_send_to": "raids",           # кнопка «В Набеги» добавляет их в список набегов
        "desc": "Сканирует богатых бьющихся соперников (сорт. по серебру) и выдаёт список ников. "
                "Справа в поле «Найденные ники» — кнопки «Копировать» и «В Набеги».",
        "fields": [{"id": "want", "label": "Сколько целей найти", "kind": "number", "default": 10},
                   {"id": "pages", "label": "Сколько страниц сканировать", "kind": "number", "default": 6},
                   {"id": "max_def", "label": "Макс. защита цели (0 = любая; напр. 500 для боя за 1 HP)",
                    "kind": "number", "default": 0}],
        "selects": [{"id": "skip_def", "label": "Пропускать цели с ров/частокол/защитой",
                     "options": ["Да", "Нет"], "default": "Да"}],
        "actions": [{"id": "run", "label": "🔎 Найти цели"},
                    {"id": "dry", "label": "Холостой (dry-run)"}],
    },
    {
        "id": "scout", "title": "Разведка (КД/щиты)", "emoji": "📊", "kind": "oneshot",
        "script": "holop_scout.py", "log": "hub_scout.out",
        "desc": "Вставь ников — покажет, когда у каждого спадёт щит/КД и восстановится HP "
                "(время по МСК). Можно поставить напоминалки «за N минут: готовься к атаке на X».",
        "fields": [{"id": "nicks", "label": "Ники для проверки (по одному в строке)",
                    "kind": "textarea", "rows": 8, "placeholder": "Миру мир\nЗима"},
                   {"id": "remind", "label": "Напомнить за N минут до (0 = без напоминаний)",
                    "kind": "number", "default": 1}],
        "actions": [{"id": "run", "label": "🔎 Проверить"},
                    {"id": "remind_run", "label": "⏰ Проверить + напоминалки"},
                    {"id": "clear", "label": "🧹 Снять напоминалки"}],
    },
]
MOD = {m["id"]: m for m in MODULES}


# ─────────────── общие утилиты процессов/логов ───────────────
def path(name):
    return os.path.join(HERE, name)


def pidfile(mid):
    return path(f"hub_{mid}.pid" if mid != "raids" else "smash.pid")


def read_pid(mid):
    try:
        with open(pidfile(mid)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _pgrep(pat):
    if IS_WIN:
        return False   # на Windows pgrep нет — статус определяем по pid-файлам
    try:
        return subprocess.run(["pgrep", "-f", pat],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def is_running(mid):
    # проверяем pid С УЧЁТОМ имени скрипта (*nix) — защита от переиспользования pid другим процессом
    script = MOD.get(mid, {}).get("script") if mid != "raids" else "holop_smash.py"
    alive = _pid_alive(read_pid(mid), script)
    if mid == "raids":
        # набеги мог поднять и «Запустить» (smash.pid), и «Ночной режим» (night_smash.sh)
        return alive or _pgrep("holop_smash.py") or night_running()
    return alive


# ─────────────── ночной режим (caffeinate + сторож) ───────────────
def read_night_pid():
    try:
        with open(NIGHT_PID) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def night_running():
    return _pid_alive(read_night_pid(), "night_smash.sh") or _pgrep("night_smash.sh")


def start_night():
    write_control("run")
    if not os.path.exists(NIGHT_SH):
        # Ночной режим (caffeinate + авто-перезапуск) — дополнительный файл, в раздаче его нет.
        # Просто держим набеги включёнными, без «будильника» Mac.
        return raids_start()
    if night_running():
        return
    out = open(path("smash_console.out"), "a", encoding="utf-8")
    p = subprocess.Popen(["/bin/bash", NIGHT_SH], cwd=HERE, stdout=out, stderr=out,
                         start_new_session=True)
    with open(NIGHT_PID, "w") as f:
        f.write(str(p.pid))


def stop_night():
    write_control("stop")

    def _backstop():
        for _ in range(25):           # даём смашеру доиграть и напечатать отчёт
            if not night_running():
                return
            time.sleep(1)
        pid = read_night_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for pat in ("night_smash.sh", "holop_smash.py"):
            subprocess.run(["pkill", "-f", pat],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=_backstop, daemon=True).start()


def tail(logname, n=250):
    try:
        with open(path(logname), encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return "(лога ещё нет)"
    clean = [ln for ln in lines
             if "TypeNotFoundError" not in ln and "Could not find a matching" not in ln]
    out = []
    for ln in clean[-n:]:
        parts = ln.split("  ", 1)
        if len(parts) == 2 and len(parts[0]) >= 19:
            out.append(parts[0][11:19] + "  " + parts[1])
        else:
            out.append(ln)
    return "".join(out) or "(лог пуст)"


# ─────────────── модуль набегов (цикл) ───────────────
def write_control(v):
    try:
        with open(path("smash_control.txt"), "w", encoding="utf-8") as f:
            f.write(v)
    except OSError:
        pass


def raids_start():
    write_control("run")
    if is_running("raids"):
        return
    out = open(path("smash_console.out"), "a", encoding="utf-8")
    p = subprocess.Popen([PY, "holop_smash.py"], cwd=HERE, stdout=out, stderr=out,
                         **POPEN_KW)
    with open(pidfile("raids"), "w") as f:
        f.write(str(p.pid))


def raids_stop():
    write_control("stop")
    if night_running():
        stop_night()          # мягко гасим и ночной сторож, и смашер (вернёт сон Mac)
        return
    _terminate(read_pid("raids"))
    if not IS_WIN:
        try:
            subprocess.run(["pkill", "-f", "holop_smash.py"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def control_value():
    try:
        with open(path("smash_control.txt")) as f:
            return f.read().strip().lower()
    except OSError:
        return ""


def raids_state():
    if is_running("raids") and control_value().startswith("pause"):
        return "pause"
    return "run" if is_running("raids") else "stopped"


def load_targets():
    try:
        with open(path("smash_targets.txt"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "# по нику в строке\n"


def save_targets(text):
    try:
        with open(path("smash_targets.txt"), "w", encoding="utf-8") as f:
            f.write(text.rstrip("\n") + "\n")
        return True
    except OSError:
        return False


def load_donate():
    """Список «щитников» (донат Купол/Стена) — смашер их не бьёт, экономит требушеты."""
    try:
        with open(path("smash_donate.txt"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "# ники под донат-щитом (Купол/Стена) — бот их пропустит, требушеты не тратит\n"


def save_donate(text):
    try:
        with open(path("smash_donate.txt"), "w", encoding="utf-8") as f:
            f.write(text.rstrip("\n") + "\n")
        return True
    except OSError:
        return False


# ─────────────── настройки боя (smash_settings.json) ───────────────
SMASH_SETTINGS_DEFAULTS = {"my_min_hp": 25, "my_recover_to": 50, "sec_per_hp": 60,
                           "regen_auto": False, "auto_kazna": False, "auto_defense": False,
                           "pierce_defenses": True, "hit_shields": True, "bank_gold": False,
                           "auto_oboz": False, "war_mode": False}


def load_smash_settings():
    """Текущие настройки боя (файл поверх дефолтов)."""
    cur = dict(SMASH_SETTINGS_DEFAULTS)
    try:
        with open(path("smash_settings.json"), encoding="utf-8") as f:
            data = json.load(f)
        for k in SMASH_SETTINGS_DEFAULTS:
            if k in data:
                cur[k] = data[k]
    except (OSError, ValueError, TypeError):
        pass
    return cur


def save_smash_settings(body):
    """Записать настройки боя из панели с разумными ограничениями."""
    cur = load_smash_settings()
    out = dict(cur)
    try:
        out["my_min_hp"] = max(20, min(int(body.get("my_min_hp", cur["my_min_hp"])), 100))
        out["my_recover_to"] = max(out["my_min_hp"] + 1,
                                   min(int(body.get("my_recover_to", cur["my_recover_to"])), 100))
        out["sec_per_hp"] = max(5, min(int(body.get("sec_per_hp", cur["sec_per_hp"])), 600))
        out["regen_auto"] = bool(body.get("regen_auto", cur["regen_auto"]))
        out["auto_kazna"] = bool(body.get("auto_kazna", cur["auto_kazna"]))
        out["auto_defense"] = bool(body.get("auto_defense", cur["auto_defense"]))
        out["pierce_defenses"] = bool(body.get("pierce_defenses", cur["pierce_defenses"]))
        out["hit_shields"] = bool(body.get("hit_shields", cur["hit_shields"]))
        out["bank_gold"] = bool(body.get("bank_gold", cur["bank_gold"]))
        out["auto_oboz"] = bool(body.get("auto_oboz", cur["auto_oboz"]))
        out["war_mode"] = bool(body.get("war_mode", cur["war_mode"]))
    except (TypeError, ValueError):
        return False
    try:
        with open(path("smash_settings.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


# ─────────────── разовые модули (oneshot) ───────────────
def build_args(mid, action, fields):
    f = fields or {}
    if mid == "roles":
        prof = (f.get("prof") or "Воин").strip()
        nicks = [x.strip() for x in (f.get("nick") or "").splitlines() if x.strip()]
        tail_args = ["--dry-run"] if action == "dry" else []
        if (f.get("auto_defrog") or "Нет").strip() == "Да":
            tail_args = tail_args + ["--auto-defrog"]   # снять охрану зельем из запаса
        if action == "check":
            nick = nicks[0] if nicks else ""
            return [nick, "--check"]
        if len(nicks) > 1:
            lp = path("hub_roles_nicks.txt")
            with open(lp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(nicks) + "\n")
            return ["--list", lp, "--prof", prof] + tail_args
        nick = nicks[0] if nicks else ""
        return [nick, "--prof", prof] + tail_args
    if mid == "alarms":
        if action == "clear":
            return ["--clear"]
        args = ["--lead", str(int(f.get("lead") or 5))]
        return args + (["--dry-run"] if action == "dry" else [])
    if mid == "caves":
        args = ["--min-win", str(int(f.get("min_win") or 85)),
                "--min-hp", str(int(f.get("min_hp") or 20)),
                "--max-level", str(int(f.get("max_level") or 10))]
        only = (f.get("only") or "").strip()
        if only:
            args += ["--only", only]
        return args + (["--dry-run"] if action == "dry" else [])
    if mid == "find":
        # holop_raid.py без --attack = ТОЛЬКО собрать список ников (не бьёт)
        args = ["--want", str(int(f.get("want") or 10)),
                "--pages", str(int(f.get("pages") or 6))]
        if (f.get("skip_def") or "Да").strip().lower().startswith("да"):
            args.append("--skip-defended")
        md = int(f.get("max_def") or 0)
        if md > 0:
            args += ["--max-def", str(md)]
        return args + (["--dry-run"] if action == "dry" else [])
    if mid == "scout":
        if action == "clear":
            return ["--clear"]
        nicks = [x.strip() for x in (f.get("nicks") or "").splitlines() if x.strip()]
        lp = path("scout_targets.txt")
        with open(lp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(nicks) + "\n")
        args = ["--list", lp]
        if action == "remind_run":
            args += ["--remind", str(int(f.get("remind") or 1))]
        return args
    return []


def oneshot_run(mid, action, fields):
    mod = MOD.get(mid)
    if not mod or mod["kind"] != "oneshot":
        return {"ok": False, "err": "неизвестный модуль"}
    if is_running(mid):
        return {"ok": False, "err": "уже выполняется — дождись конца или останови"}
    args = build_args(mid, action, fields)
    logp = path(mod["log"])
    with open(logp, "a", encoding="utf-8") as f:
        f.write(f"\n═════ {mod['title']}: {action} {' '.join(args)} ═════\n")
    out = open(logp, "a", encoding="utf-8")
    p = subprocess.Popen([PY, mod["script"], *args], cwd=HERE, stdout=out, stderr=out,
                         **POPEN_KW)
    with open(pidfile(mid), "w") as f:
        f.write(str(p.pid))
    return {"ok": True}


def oneshot_stop(mid):
    _terminate(read_pid(mid))
    if not IS_WIN:
        try:
            subprocess.run(["pkill", "-f", MOD[mid]["script"]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    return {"ok": True}


BOT_SCRIPTS = sorted({m["script"] for m in MODULES if m.get("script")})


def _win_sweep_orphans():
    """Windows: убить осиротевшие боты (python-процессы наших скриптов),
    что пережили закрытие cmd. Бьём ТОЛЬКО по командной строке — чужое не заденем."""
    if not IS_WIN:
        return
    likes = " -or ".join(f"$_.CommandLine -like '*{s}*'" for s in BOT_SCRIPTS)
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'py%'\" | "
          f"Where-Object {{ {likes} }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=0x08000000, timeout=15)
    except Exception:
        pass


def stop_all():
    """СТОП-КРАН: погасить ВСЁ — набеги, разовые модули, ночной режим, осиротевшие боты."""
    write_control("stop")
    if night_running():
        stop_night()
    for m in MODULES:
        _terminate(read_pid(m["id"]))
    if IS_WIN:
        _win_sweep_orphans()
    else:
        for s in BOT_SCRIPTS:
            try:
                subprocess.run(["pkill", "-f", s],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    return {"ok": True}


# ─────────────── статус-доска (КД/щиты из лога набегов) ───────────────
STATE_PATTERNS = [
    (re.compile(r"ПОБЕДА"), "🏆 победа"),
    (re.compile(r"на КД ещё\s*([^\-—]+)"), "⌛ КД {0}"),
    (re.compile(r"под щитом ещё\s*~?([^\-—]+)"), "🛡️ щит {0}"),
    (re.compile(r"жду реген\s*~?([^\-—]+)"), "💤 реген {0}"),
    (re.compile(r"частокол/ров"), "🧱 пробиваю"),
    (re.compile(r"ПОРАЖЕНИЕ"), "❌ снят (скамейка)"),
    (re.compile(r"свой клан|ниже уровня"), "🚫 недоступен"),
    (re.compile(r"слаб"), "💤 слаб"),
]


def status_board():
    targets = [x.split("#", 1)[0].strip()
               for x in load_targets().splitlines() if x.split("#", 1)[0].strip()]
    try:
        with open(path("smash.log"), encoding="utf-8", errors="replace") as f:
            lines = [ln for ln in f if "\\x" not in ln]
    except OSError:
        lines = []
    rows = []
    for t in targets:
        state, when = "—", ""
        for ln in reversed(lines):
            if (t + ":") in ln:
                for rx, tmpl in STATE_PATTERNS:
                    m = rx.search(ln)
                    if m:
                        state = tmpl.format(*[g.strip() for g in m.groups()]) if m.groups() else tmpl
                        break
                tm = ln.split("  ", 1)[0]
                when = tm[11:19] if len(tm) >= 19 else ""
                break
        rows.append({"name": t, "state": state, "when": when})
    # сводка
    summary = ""
    for ln in reversed(lines):
        if "Сводка:" in ln:
            summary = ln.split("  ", 1)[-1].strip()
            break
    return {"rows": rows, "summary": summary, "raids": raids_state()}


# ─────────────── HTTP ───────────────
def ui_config():
    return [{k: m.get(k) for k in
             ("id", "title", "emoji", "kind", "desc", "fields", "selects", "actions",
              "result_file", "result_send_to")}
            for m in MODULES]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/" or p.startswith("/index"):
            page = PAGE if is_authorized() else LOGIN_PAGE
            page = page.replace("__VERSION__", VERSION)
            self._send(200, page, "text/html; charset=utf-8")
        elif p == "/api/auth/status":
            self._json({"authorized": is_authorized()})
        elif p == "/api/config":
            self._json(ui_config())
        elif p == "/api/targets":
            self._send(200, load_targets(), "text/plain; charset=utf-8")
        elif p == "/api/donate":
            self._send(200, load_donate(), "text/plain; charset=utf-8")
        elif p == "/api/raids/settings":
            self._json(load_smash_settings())
        elif p == "/api/status_board":
            self._json(status_board())
        elif p.startswith("/api/") and p.endswith("/status"):
            mid = p.split("/")[2]
            mod = MOD.get(mid, {})
            self._json({"running": is_running(mid),
                        "state": raids_state() if mid == "raids" else ("run" if is_running(mid) else "idle"),
                        "night": night_running() if mid == "raids" else False,
                        "log": tail(mod.get("log", "")) if mod.get("log") else ""})
        elif p.startswith("/api/") and p.endswith("/results"):
            mid = p.split("/")[2]
            rf = MOD.get(mid, {}).get("result_file")
            txt = ""
            if rf:
                try:
                    with open(path(rf), encoding="utf-8", errors="replace") as f:
                        txt = f.read()
                except OSError:
                    txt = ""
            self._send(200, txt, "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        try:
            body = json.loads(raw) if raw and raw.lstrip().startswith("{") else {}
        except Exception:
            body = {}
        parts = p.strip("/").split("/")   # ['api', mid, action]
        # ── стоп-кран: погасить всё ──
        if p == "/api/stop_all":
            return self._json(stop_all())
        # ── вход в аккаунт ──
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "auth":
            try:
                if parts[2] == "send_code":
                    phone = (body.get("phone") or "").strip()
                    if not phone:
                        return self._json({"ok": False, "err": "Введи номер телефона."})
                    kind, where = AUTH.send_code(phone, force_sms=bool(body.get("sms")))
                    return self._json({"ok": True, "kind": kind, "where": where})
                if parts[2] == "sign_in":
                    r = AUTH.sign_in(code=(body.get("code") or "").strip())
                    return self._json({"ok": True, "need": r})
                if parts[2] == "password":
                    r = AUTH.sign_in(password=(body.get("password") or ""))
                    return self._json({"ok": True, "need": r})
                if parts[2] == "logout":
                    cfg = load_cfg()
                    cfg.pop("session_string", None)   # стираем вход → откроется экран входа
                    save_cfg(cfg)
                    return self._json({"ok": True})
            except Exception as e:
                _log_auth_error(e)      # в auth_log.txt — чтобы прислать при проблемах входа
                return self._json({"ok": False, "err": _auth_err(e)})
        if len(parts) == 3 and parts[0] == "api":
            mid, action = parts[1], parts[2]
            if mid == "raids" and action == "start":
                raids_start(); return self._json({"ok": True})
            if mid == "raids" and action == "stop":
                raids_stop(); return self._json({"ok": True})
            if mid == "raids" and action == "night_on":
                start_night(); return self._json({"ok": True})
            if mid == "raids" and action == "night_off":
                stop_night(); return self._json({"ok": True})
            if mid == "raids" and action == "save":
                return self._json({"ok": save_targets(raw if not body else body.get("text", ""))})
            if mid == "raids" and action == "save_donate":
                return self._json({"ok": save_donate(raw if not body else body.get("text", ""))})
            if mid == "raids" and action == "settings":
                return self._json({"ok": save_smash_settings(body)})
            if action == "run":
                return self._json(oneshot_run(mid, body.get("action", "run"), body.get("fields", {})))
            if action == "stop":
                return self._json(oneshot_stop(mid))
        self._send(404, "not found", "text/plain; charset=utf-8")


# ─────────────── страница ───────────────
PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🏰 Холоп — Пульт</title>
<style>
 :root{color-scheme:light dark;
   --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,Helvetica,Arial,sans-serif;
   --mono:ui-monospace,"SF Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace;
   --bg:#1f1830;--panel:#292140;--panel2:#2e2649;--elev:#372d55;
   --ink:#f0eafa;--mut:#b2a6ce;--faint:#7b7098;--line:rgba(185,160,255,.10);--line2:rgba(185,160,255,.055);
   --accent:#e6873a;--blue:#e6873a;--green:#3fbe86;--red:#f05a6b;--grey:#372d55;--purple:#9b7be6;--amber:#e6873a;
   --shadow:0 1px 2px rgba(0,0,0,.32),0 20px 46px -16px rgba(0,0,0,.6);}
 @media (prefers-color-scheme:light){:root{
   --bg:#efe9f9;--panel:#ffffff;--panel2:#f7f3fd;--elev:#ffffff;--ink:#2a2340;--mut:#6f6690;--faint:#a79fc4;
   --line:rgba(110,80,190,.10);--line2:rgba(110,80,190,.05);
   --accent:#d9772a;--blue:#d9772a;--green:#1fa877;--red:#e24a5c;--grey:#efe9fb;--purple:#7c5cf5;--amber:#c07020;
   --shadow:0 1px 2px rgba(60,40,110,.06),0 18px 40px -14px rgba(80,55,150,.16);}}
 *{box-sizing:border-box}
 html{-webkit-text-size-adjust:100%}
 body{margin:0;min-height:100vh;color:var(--ink);font:14.5px/1.5 var(--font);
   -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
   background:radial-gradient(1200px 700px at 88% -10%,color-mix(in srgb,var(--accent) 13%,transparent),transparent 58%),
     radial-gradient(1000px 640px at 2% 112%,color-mix(in srgb,var(--purple) 13%,transparent),transparent 60%),var(--bg);
   background-attachment:fixed}
 .app{display:flex;min-height:100vh}
 .rail{width:236px;flex-shrink:0;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:16px;
   padding:18px 13px;background:color-mix(in srgb,var(--panel) 82%,transparent);
   backdrop-filter:blur(22px) saturate(180%);-webkit-backdrop-filter:blur(22px) saturate(180%);
   border-right:1px solid var(--line);z-index:40}
 .brand{display:flex;align-items:center;gap:12px;padding:4px 6px}
 .brand-tile{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;font-size:21px;flex-shrink:0;
   background:linear-gradient(150deg,color-mix(in srgb,var(--accent) 42%,var(--panel)),var(--panel));border:1px solid var(--line);
   box-shadow:0 5px 16px color-mix(in srgb,var(--accent) 24%,transparent),inset 0 1px 0 rgba(255,255,255,.10)}
 .brand-tx{display:flex;flex-direction:column;line-height:1.02;font-weight:770;font-size:17px;letter-spacing:-.01em}
 .brand-tx small{font:600 9px var(--mono);letter-spacing:.36em;color:var(--accent);margin-top:4px}
 #tabs{display:flex;flex-direction:column;gap:3px;flex:1;overflow-y:auto;min-height:0}
 .tab{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:11px;cursor:pointer;color:var(--mut);
   font-weight:560;font-size:13.5px;white-space:nowrap;user-select:none;transition:color .2s,background .2s,transform .12s,box-shadow .25s}
 .tab:hover{color:var(--ink);background:color-mix(in srgb,var(--accent) 11%,transparent)}
 .tab:active{transform:scale(.98)}
 .tab.on{color:#fff;background:var(--accent);box-shadow:0 8px 18px -8px color-mix(in srgb,var(--accent) 65%,transparent)}
 .rail-foot{display:flex;flex-direction:column;gap:8px;border-top:1px solid var(--line);padding-top:14px}
 .rail-foot .b-red{box-shadow:0 10px 22px -12px color-mix(in srgb,var(--red) 70%,transparent)}
 .ver{font:11px var(--mono);color:var(--faint);text-align:center;letter-spacing:.02em;margin-top:2px}
 .pet{display:flex;flex-direction:column;align-items:center;gap:4px;padding:4px 0 2px;position:relative}
 .pet-bubble{min-height:20px;font-size:15px;line-height:20px;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:1px 8px;opacity:0;transition:opacity .2s;box-shadow:var(--shadow)}
 #petCat{width:92px;height:92px;image-rendering:pixelated;image-rendering:crisp-edges;cursor:pointer}
 .pet-say{font:10.5px var(--mono);color:var(--faint);letter-spacing:.02em}
 #petRun{position:fixed;bottom:12px;left:0;width:104px;height:104px;image-rendering:pixelated;pointer-events:none;z-index:60;display:none;filter:drop-shadow(0 6px 10px rgba(0,0,0,.4))}
 main{flex:1;min-width:0;max-width:1240px;margin:0 auto;padding:26px 30px 38px}
 .head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px;animation:rise .42s both}
 .head-l{display:flex;align-items:center;gap:14px;min-width:0}
 .mod-emoji{width:52px;height:52px;border-radius:16px;display:grid;place-items:center;font-size:26px;flex-shrink:0;
   background:var(--panel2);border:1px solid var(--line);box-shadow:var(--shadow)}
 .mod-title{font-size:22px;font-weight:750;letter-spacing:-.02em}
 .desc{color:var(--mut);font-size:13px;margin-top:2px;max-width:72ch}
 .head-r{flex-shrink:0}
 .controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(258px,1fr));gap:16px;margin-bottom:20px;
   align-items:start;animation:rise .42s .05s both}
 .acc{background:var(--panel);border:1px solid var(--line);border-radius:22px;overflow:hidden;box-shadow:var(--shadow);
   transition:box-shadow .25s,border-color .25s}
 .acc:hover{border-color:color-mix(in srgb,var(--accent) 38%,var(--line))}
 .acc[open]{grid-column:1/-1}
 .acc summary{list-style:none;display:flex;align-items:center;gap:12px;cursor:pointer;padding:17px 19px;
   font-weight:600;font-size:14.5px;user-select:none;transition:background .2s}
 .acc summary:hover{background:color-mix(in srgb,var(--accent) 7%,transparent)}
 .acc summary::-webkit-details-marker{display:none}
 .acc-ic{font-size:17px;line-height:1}
 .acc-hint{font:11px var(--mono);color:var(--faint);font-weight:400}
 .chev{position:relative;width:18px;height:18px;flex-shrink:0;margin-left:auto}
 .chev::before,.chev::after{content:"";position:absolute;background:var(--mut);border-radius:2px;transition:transform .28s cubic-bezier(.2,.7,.2,1),background .2s}
 .chev::before{left:3px;right:3px;top:8px;height:2px}
 .chev::after{top:3px;bottom:3px;left:8px;width:2px}
 details[open] .chev::after{transform:scaleY(0)}
 .acc:hover .chev::before,.acc:hover .chev::after{background:var(--accent)}
 .acc-body{padding:2px 19px 19px;animation:accIn .3s cubic-bezier(.2,.7,.2,1)}
 .grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(232px,1fr));gap:12px 26px}
 .grid2 .ctl-l{margin-top:0}
 .grid2 .sw{margin-top:0}
 label{display:block}
 .ctl-l{color:var(--mut);font-size:12px;font-weight:520;margin:11px 0 5px}
 input,select,textarea{width:100%;background:var(--panel2);color:var(--ink);border:1px solid var(--line);
   border-radius:12px;padding:11px 12px;font:14px var(--font);outline:none;transition:border-color .16s,box-shadow .16s;-webkit-appearance:none;appearance:none}
 input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 26%,transparent)}
 input:disabled{opacity:.45;cursor:not-allowed}
 textarea{font:13px/1.55 var(--mono);resize:vertical}
 .sw{display:flex;align-items:center;gap:11px;margin-top:9px;cursor:pointer;font-size:13px;color:var(--ink);line-height:1.35}
 .sw input{position:absolute;opacity:0;width:0;height:0;pointer-events:none}
 .sw .track{flex-shrink:0;width:40px;height:23px;border-radius:99px;background:var(--elev);border:1px solid var(--line);position:relative;transition:background .22s,border-color .22s}
 .sw .track::after{content:"";position:absolute;top:2px;left:2px;width:17px;height:17px;border-radius:50%;background:var(--mut);transition:transform .22s cubic-bezier(.2,.9,.3,1.2),background .22s}
 .sw input:checked + .track{background:var(--green);border-color:transparent}
 .sw input:checked + .track::after{transform:translateX(17px);background:#fff}
 .sw input:focus-visible + .track{box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 32%,transparent)}
 .sw .lbl{flex:1}
 .console{animation:rise .42s .1s both;background:var(--panel);border:1px solid var(--line);border-radius:22px;overflow:hidden;box-shadow:var(--shadow)}
 .console-bar{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line2)}
 .live{display:inline-flex;align-items:center;gap:7px;font:700 10.5px var(--mono);letter-spacing:.2em;color:var(--green);
   padding:4px 10px;border-radius:99px;background:color-mix(in srgb,var(--green) 13%,transparent)}
 .live::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--green);animation:blip 1.6s ease-in-out infinite}
 .console-name{font:12.5px var(--mono);color:var(--mut)}
 .console-meta{margin-left:auto;font:11px var(--mono);color:var(--faint)}
 pre.log{min-height:440px;max-height:calc(100vh - 340px);overflow:auto;background:transparent;
   border:0;border-radius:0;padding:18px 20px;margin:0;white-space:pre-wrap;word-break:break-word;
   font:13px/1.75 var(--mono);color:var(--ink)}
 button{font:600 13.5px var(--font);border:0;border-radius:12px;padding:11px 15px;cursor:pointer;color:#fff;
   box-shadow:0 1px 2px rgba(20,15,45,.08);transition:transform .09s,filter .16s,box-shadow .22s}
 button:hover{filter:brightness(1.06)} button:active{transform:scale(.97)}
 .b-green{background:var(--green)}
 .b-red{background:var(--red)}
 .b-blue{background:var(--accent);width:100%}
 .b-grey{background:var(--elev);color:var(--ink);box-shadow:inset 0 0 0 1px var(--line)}
 .b-night{background:var(--elev);color:var(--ink);width:100%;box-shadow:inset 0 0 0 1px var(--line)}
 .b-night.on{background:var(--purple);color:#fff}
 .btns{display:flex;flex-wrap:wrap;gap:8px}
 .btns button{flex:1;min-width:120px}
 .pill{padding:6px 13px;border-radius:99px;font-weight:650;font-size:12.5px;display:inline-flex;align-items:center;gap:7px}
 .pill::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor;box-shadow:0 0 9px currentColor}
 .pill.run{background:color-mix(in srgb,var(--green) 16%,transparent);color:var(--green)}
 .pill.pause{background:color-mix(in srgb,var(--mut) 18%,transparent);color:var(--mut)}
 .pill.stopped,.pill.idle{background:color-mix(in srgb,var(--red) 15%,transparent);color:var(--red)}
 .note{color:var(--mut);font-size:12px;min-height:15px;margin-top:9px;line-height:1.45}
 .summ{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:14px 16px;margin-bottom:16px;
   font-size:13px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;box-shadow:var(--shadow);animation:rise .42s .05s both}
 .board-wrap{background:var(--panel);border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:var(--shadow);animation:rise .42s .1s both}
 table{width:100%;border-collapse:collapse;font-size:14px}
 td,th{text-align:left;padding:12px 15px;border-bottom:1px solid var(--line2)}
 tr:last-child td{border-bottom:0}
 th{color:var(--mut);font-weight:590;font-size:12px;font-family:var(--mono);letter-spacing:.03em}
 ::-webkit-scrollbar{width:11px;height:11px}
 ::-webkit-scrollbar-thumb{background:color-mix(in srgb,var(--mut) 32%,transparent);border-radius:99px;border:3px solid transparent;background-clip:padding-box}
 ::-webkit-scrollbar-thumb:hover{background:color-mix(in srgb,var(--mut) 50%,transparent);background-clip:padding-box}
 ::-webkit-scrollbar-track{background:transparent}
 @keyframes blip{0%,100%{opacity:1}50%{opacity:.32}}
 @keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
 @keyframes accIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
 @media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
 @media (max-width:860px){
   .app{flex-direction:column}
   .rail{width:auto;height:auto;flex-direction:row;align-items:center;flex-wrap:wrap;gap:10px;padding:10px 14px;
     border-right:0;border-bottom:1px solid var(--line)}
   #tabs{flex-direction:row;order:3;flex-basis:100%;flex-wrap:nowrap;overflow-x:auto;gap:4px}
   .rail-foot{flex-direction:row;align-items:center;border-top:0;padding-top:0;margin-left:auto}
   .ver{display:none}
   main{padding:16px 14px 26px}
   .controls{grid-template-columns:1fr}
   .acc[open]{grid-column:auto}
   pre.log{max-height:56vh;min-height:320px}
 }
 @media (max-width:430px){.brand-tx{font-size:15px}.btns button{min-width:0}}
</style></head><body>
<div class="app">
<aside class="rail">
  <div class="brand"><span class="brand-tile">🏰</span><span class="brand-tx">Холоп<small>ПУЛЬТ</small></span></div>
  <nav id="tabs"></nav>
  <div class="pet"><div class="pet-bubble" id="petBubble"></div><canvas id="petCat" width="26" height="26"></canvas><span class="pet-say" id="petSay">🐾 Рыжик</span></div>
  <div class="rail-foot">
    <button class="b-grey" onclick="logout()" title="Выйти и войти заново (если Telegram отозвал сессию)">👤 Сменить аккаунт</button>
    <button class="b-red" onclick="stopAll()" title="Остановить ВСЕ боты разом">🛑 Стоп-кран</button>
    <div class="ver">v__VERSION__</div>
  </div>
</aside>
<main id="main"></main>
</div>
<canvas id="petRun" width="26" height="26"></canvas>
<script>
const $=s=>document.querySelector(s);
let CFG=[], active=null, timer=null, petState='run', petEvent=null, petLastLine='';
function classifyLog(line){
  var m=[[/побед|✅/i,'🎉','win','Победа!'],[/лечит|ухожу леч|❤/i,'🩹','heal','Лечится'],[/бочк|💣/i,'😾','alarm','Бочка!'],[/щит|требушет|🏹|🛡/i,'🛡️','block','Щит!'],[/казн|депозит|🏦|🪙|серебр/i,'💰','loot','Добыча'],[/атак|🎯/i,'⚔️','attack','В атаку']];
  for(var i=0;i<m.length;i++){ if(m[i][0].test(line)) return {emoji:m[i][1],kind:m[i][2],text:m[i][3],at:performance.now()}; }
  return null;
}

function pill(state){
  const t=state==='run'?'🟢 Работает':state==='pause'?'⏸ Пауза':
          state==='idle'?'⚪ Не запущен':state==='stopped'?'⚪ Остановлен':state;
  return `<span class="pill ${state}">${t}</span>`;
}
function fieldHTML(f){
  if(f.kind==='textarea') return `<label class="ctl-l">${f.label}</label><textarea id="f_${f.id}" rows="${f.rows||5}" placeholder="${f.placeholder||''}"></textarea>`;
  const type=f.kind==='number'?'number':'text';
  return `<label class="ctl-l">${f.label}</label><input id="f_${f.id}" type="${type}" value="${f.default!=null?f.default:''}">`;
}
function selectHTML(s){
  const opts=s.options.map(o=>`<option ${o===s.default?'selected':''}>${o}</option>`).join('');
  return `<label class="ctl-l">${s.label}</label><select id="f_${s.id}">${opts}</select>`;
}
function swHTML(id,label){
  return `<label class="sw"><input id="${id}" type="checkbox"><span class="track"></span><span class="lbl">${label}</span></label>`;
}
function render(mid){
  active=mid; if(timer) clearInterval(timer);
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.id===mid));
  const m=CFG.find(x=>x.id===mid); const main=$('#main');
  const head=`<div class="head">
      <div class="head-l"><div class="mod-emoji">${m.emoji||''}</div>
        <div><div class="mod-title">${m.title}</div><div class="desc">${m.desc||''}</div></div></div>
      <div class="head-r"><span id="mpill"></span></div></div>`;
  if(m.kind==='status'){
    main.innerHTML=head+`<div class="summ" id="summ">—</div>
      <div class="board-wrap"><table><thead><tr><th>Цель</th><th>Состояние</th><th>Обновл.</th></tr></thead><tbody id="board"></tbody></table></div>`;
    pollStatus(); timer=setInterval(pollStatus,2000); return;
  }
  let ctl='';
  if(m.kind==='loop'){
    ctl=`<details class="acc" open>
        <summary><span class="acc-ic">🎮</span><span class="acc-t">Управление</span><span class="chev"></span></summary>
        <div class="acc-body">
          <div class="btns"><button class="b-green" onclick="post('${mid}','start')">▶ Запустить</button>
            <button class="b-red" onclick="post('${mid}','stop')">⏹ Остановить</button></div>
          <button id="nightBtn" class="b-night" style="margin-top:9px" onclick="toggleNight()">🌙 Ночной режим</button>
          <div class="note">🌙 держит Mac бодрым (caffeinate) + сам перезапускает бота, если упал/завис. Для фарма на ночь.</div>
        </div>
      </details>
      <details class="acc">
        <summary><span class="acc-ic">🎯</span><span class="acc-t">Цели</span><span class="acc-hint">ник в строке</span><span class="chev"></span></summary>
        <div class="acc-body">
          <textarea id="targets" rows="8" spellcheck="false"></textarea>
          <button class="b-blue" style="margin-top:10px" onclick="saveTargets()">💾 Сохранить список</button>
          <div class="note" id="note"></div>
        </div>
      </details>
      <details class="acc">
        <summary><span class="acc-ic">🛡️</span><span class="acc-t">Щитники — не бить</span><span class="chev"></span></summary>
        <div class="acc-body">
          <textarea id="donate" rows="4" spellcheck="false" placeholder="ник в строке — бот их пропустит, требушеты не потратит"></textarea>
          <button class="b-blue" style="margin-top:10px" onclick="saveDonate()">💾 Сохранить щитников</button>
          <div class="note" id="dnote">Впиши тех, у кого донат-щит (Купол/Стена) — бот их не тронет. Бот и сам заносит сюда, кого распознал (1 требушет на распознавание).</div>
        </div>
      </details>
      <details class="acc">
        <summary><span class="acc-ic">⚔️</span><span class="acc-t">Настройки боя</span><span class="acc-hint">10 параметров</span><span class="chev"></span></summary>
        <div class="acc-body">
          <div class="grid2">
            <div><label class="ctl-l">Воевать, пока моё HP выше (иначе — лечиться)</label><input id="set_min_hp" type="number" min="20" max="100"></div>
            <div><label class="ctl-l">Лечиться до HP</label><input id="set_recover" type="number" min="21" max="100"></div>
            <div><label class="ctl-l">Реген: секунд на 1 HP (меньше = быстрее)</label><input id="set_sec_hp" type="number" min="5" max="600"></div>
          </div>
          <div class="grid2" style="margin-top:14px">
            ${swHTML('set_regen_auto','Авто-реген (считать по бонусам с главной)')}
            ${swHTML('set_auto_kazna','🏦 Авто-казна (сбор → депозит → реинвест)')}
            ${swHTML('set_bank_gold','🪙 Класть в казну и золото (иначе — только серебро, золото на оборону)')}
            ${swHTML('set_auto_defense','🛡️ Авто-оборона (ров + частокол активны + запас)')}
            ${swHTML('set_pierce','🧱 Пробивать ров/частокол у целей (иначе — пропускать)')}
            ${swHTML('set_hit_shields','🏹 Сносить донат-щит требушетом и фармить (выкл — беречь требушеты)')}
            ${swHTML('set_auto_oboz','🐴 Авто-обоз (+50% серебра с набегов — 400🏅 золота / 50 мин)')}
            ${swHTML('set_war','⚔️ РЕЖИМ ВОЙНЫ — бить по КД без пауз, держать цели прижатыми (палевно)')}
          </div>
          <button class="b-blue" style="margin-top:14px" onclick="saveSettings()">💾 Сохранить настройки</button>
          <div class="note" id="snote">Меняется на лету — бот подхватит в ближайший цикл.</div>
        </div>
      </details>`;
  } else {
    const fields=(m.fields||[]).map(fieldHTML).join('');
    const sels=(m.selects||[]).map(selectHTML).join('');
    const acts=(m.actions||[]).map(a=>{
      const cls=a.id==='run'?'b-green':a.id==='clear'?'b-red':'b-grey';
      return `<button class="${cls}" onclick="runOne('${mid}','${a.id}')">${a.label}</button>`;
    }).join('');
    let resultBox='';
    if(m.result_file){
      resultBox=`<details class="acc" open>
        <summary><span class="acc-ic">🎯</span><span class="acc-t">Найденные ники</span><span class="acc-hint">можно править</span><span class="chev"></span></summary>
        <div class="acc-body">
          <textarea id="results" rows="12" spellcheck="false" placeholder="здесь появятся найденные ники после «Найти цели»"></textarea>
          <div class="btns" style="margin-top:10px">
            <button class="b-blue" onclick="copyResults()">📋 Копировать</button>
            ${m.result_send_to?`<button class="b-green" onclick="sendResults('${m.result_send_to}')">➡️ В Набеги</button>`:''}
          </div>
          <div class="note" id="rnote"></div>
        </div>
      </details>`;
    }
    ctl=`<details class="acc" open>
        <summary><span class="acc-ic">🎛️</span><span class="acc-t">Параметры</span><span class="chev"></span></summary>
        <div class="acc-body">${fields}${sels}
          <div class="btns" style="margin-top:14px">${acts}
            <button class="b-red" onclick="post('${mid}','stop')">⏹ Стоп</button></div>
          <div class="note" id="note"></div>
        </div>
      </details>${resultBox}`;
  }
  main.innerHTML=head+`<div class="controls">${ctl}</div>
    <section class="console"><div class="console-bar"><span class="live">LIVE</span>
      <span class="console-name">Журнал — ${m.title}</span>
      <span class="console-meta">автоскролл</span></div>
      <pre class="log" id="log">…</pre></section>`;
  if(m.kind==='loop'){ loadTargets(); loadDonate(); loadSettings(); }
  pollMod(); timer=setInterval(pollMod,1500);
}
let nightOn=false;
async function pollMod(){
  try{ const r=await fetch('/api/'+active+'/status'); const d=await r.json();
    const mp=$('#mpill'); if(mp) mp.innerHTML=pill(d.state); petState=d.state||'run';
    if(d.log){ var _l=(d.log.trim().split('\n').pop()||''); if(_l!==petLastLine){ petLastLine=_l; var _e=classifyLog(_l); if(_e) petEvent=_e; } }
    const nb=$('#nightBtn'); if(nb){ nightOn=!!d.night;
      nb.className='b-night'+(nightOn?' on':'');
      nb.textContent=nightOn?'🌙 Ночной режим: ВКЛ':'🌙 Ночной режим'; }
    const log=$('#log'); if(log){ const bottom=log.scrollHeight-log.scrollTop-log.clientHeight<26;
      log.textContent=d.log||'(пусто)'; if(bottom) log.scrollTop=log.scrollHeight; }
  }catch(e){}
  loadResults();
}
async function loadResults(){
  const t=$('#results'); if(!t) return;
  // не перетираем поле, пока в нём выделяют/печатают — иначе слетает выделение
  if(document.activeElement===t) return;
  try{ const txt=await (await fetch('/api/'+active+'/results')).text();
    if(t.value!==txt) t.value=txt; }catch(e){}
}
function copyResults(){
  const t=$('#results'); if(!t||!t.value.trim()){ return; }
  t.focus(); t.select();
  const done=()=>{ const n=$('#rnote'); if(n){n.textContent='✅ скопировано в буфер'; setTimeout(()=>n.textContent='',4000);} };
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(t.value).then(done).catch(()=>{ try{document.execCommand('copy');}catch(e){} done(); });
  } else { try{document.execCommand('copy');}catch(e){} done(); }
}
async function sendResults(target){
  const t=$('#results'); if(!t||!t.value.trim()) return;
  const clean=s=>s.split('\n').map(x=>x.split('#')[0].trim()).filter(Boolean);
  const fresh=clean(t.value);
  let existing=[];
  try{ existing=clean(await (await fetch('/api/targets')).text()); }catch(e){}
  const merged=existing.slice();
  fresh.forEach(n=>{ if(!merged.includes(n)) merged.push(n); });
  try{ await fetch('/api/'+target+'/save',{method:'POST',body:merged.join('\n')});
    const n=$('#rnote'); if(n){n.textContent='✅ добавлено в Набеги (всего '+merged.length+') — открой вкладку ⚔️ Набеги'; setTimeout(()=>n.textContent='',7000);} }catch(e){}
}
async function toggleNight(){
  const to=nightOn?'night_off':'night_on';
  if(to==='night_off' && !confirm('Выключить ночной режим? Бот остановится, засыпание Mac разблокируется.')) return;
  try{ await fetch('/api/raids/'+to,{method:'POST'}); }catch(e){}
  pollMod();
}
async function pollStatus(){
  try{ const r=await fetch('/api/status_board'); const d=await r.json();
    $('#summ').innerHTML=(d.summary||'—')+'  '+pill(d.raids);
    $('#board').innerHTML=d.rows.map(x=>`<tr><td>${x.name}</td><td>${x.state}</td><td style="color:#8a8f98">${x.when||''}</td></tr>`).join('');
  }catch(e){}
}
async function post(mid,action){ try{await fetch('/api/'+mid+'/'+action,{method:'POST'});}catch(e){} pollMod(); }
async function logout(){
  if(!confirm('Выйти и войти заново? Списки целей и настройки останутся — попросит только номер и код из Telegram. Делай так, если Telegram отозвал сессию (VPN сменил IP / два запуска).')) return;
  try{ await fetch('/api/auth/logout',{method:'POST'}); }catch(e){}
  location.reload();
}
async function stopAll(){
  if(!confirm('Остановить ВСЕ боты (набеги, пещеры, всё)? Пульт останется открыт.')) return;
  try{ await fetch('/api/stop_all',{method:'POST'}); }catch(e){}
  pollMod();
}
async function runOne(mid,action){
  const m=CFG.find(x=>x.id===mid); const fields={};
  (m.fields||[]).forEach(f=>fields[f.id]=$('#f_'+f.id).value);
  (m.selects||[]).forEach(s=>fields[s.id]=$('#f_'+s.id).value);
  try{ const r=await fetch('/api/'+mid+'/run',{method:'POST',body:JSON.stringify({action,fields})});
    const d=await r.json(); const n=$('#note'); if(n) n.textContent=d.ok?'✅ запущено':(d.err||'ошибка');
  }catch(e){}
  pollMod();
}
async function loadTargets(){ try{const r=await fetch('/api/targets');const t=$('#targets'); if(t) t.value=await r.text();}catch(e){} }
async function saveTargets(){ const t=$('#targets'); if(!t)return;
  try{await fetch('/api/raids/save',{method:'POST',body:t.value});
    const n=$('#note'); if(n){n.textContent='✅ сохранено (применится в ближайшую минуту)'; setTimeout(()=>n.textContent='',6000);} }catch(e){}
}
async function loadDonate(){ try{const r=await fetch('/api/donate');const t=$('#donate'); if(t) t.value=await r.text();}catch(e){} }
async function saveDonate(){ const t=$('#donate'); if(!t)return;
  try{await fetch('/api/raids/save_donate',{method:'POST',body:t.value});
    const n=$('#dnote'); if(n){n.textContent='✅ щитники сохранены (применится в ближайшую минуту)'; setTimeout(()=>n.textContent='',6000);} }catch(e){}
}
async function loadSettings(){
  try{ const d=await (await fetch('/api/raids/settings')).json();
    const a=$('#set_min_hp'), b=$('#set_recover'), c=$('#set_sec_hp'),
          ra=$('#set_regen_auto'), ak=$('#set_auto_kazna');
    if(a && document.activeElement!==a) a.value=d.my_min_hp;
    if(b && document.activeElement!==b) b.value=d.my_recover_to;
    if(c && document.activeElement!==c) c.value=d.sec_per_hp;
    if(ra) ra.checked=!!d.regen_auto;
    if(ak) ak.checked=!!d.auto_kazna;
    const ad=$('#set_auto_defense'); if(ad) ad.checked=!!d.auto_defense;
    const pd=$('#set_pierce'); if(pd) pd.checked=(d.pierce_defenses!==false);
    const hs=$('#set_hit_shields'); if(hs) hs.checked=!!d.hit_shields;
    const bg=$('#set_bank_gold'); if(bg) bg.checked=!!d.bank_gold;
    const ao=$('#set_auto_oboz'); if(ao) ao.checked=!!d.auto_oboz;
    const wm=$('#set_war'); if(wm) wm.checked=!!d.war_mode;
    if(c) c.disabled=!!(ra&&ra.checked);
  }catch(e){}
}
async function saveSettings(){
  const a=$('#set_min_hp'), b=$('#set_recover'); if(!a||!b) return;
  const body={my_min_hp:parseInt(a.value||'25',10), my_recover_to:parseInt(b.value||'50',10),
    sec_per_hp:parseInt(($('#set_sec_hp')||{}).value||'60',10),
    regen_auto:!!($('#set_regen_auto')||{}).checked,
    auto_kazna:!!($('#set_auto_kazna')||{}).checked,
    auto_defense:!!($('#set_auto_defense')||{}).checked,
    pierce_defenses:!!($('#set_pierce')||{}).checked,
    hit_shields:!!($('#set_hit_shields')||{}).checked,
    bank_gold:!!($('#set_bank_gold')||{}).checked,
    auto_oboz:!!($('#set_auto_oboz')||{}).checked,
    war_mode:!!($('#set_war')||{}).checked};
  try{ const r=await fetch('/api/raids/settings',{method:'POST',body:JSON.stringify(body)});
    const d=await r.json(); const n=$('#snote');
    if(n){ n.textContent=d.ok?'✅ настройки сохранены — применятся в ближайший цикл':'ошибка сохранения';
      setTimeout(()=>{n.textContent='Меняется на лету — бот подхватит в ближайший цикл.';},6000);}
    loadSettings();
  }catch(e){}
}
async function init(){
  CFG=await (await fetch('/api/config')).json();
  $('#tabs').innerHTML=CFG.map(m=>`<span class="tab" data-id="${m.id}" onclick="render('${m.id}')">${m.emoji} ${m.title}</span>`).join('');
  render(CFG[0].id);
}
/* ── ПИКСЕЛЬНЫЙ КОТ-КОМПАНЬОН (Рыжик) ─────────────────────────── */
(function setupPet(){
  var cv=document.getElementById('petCat'); if(!cv) return;
  var say=document.getElementById('petSay'), bub=document.getElementById('petBubble');
  var runCv=document.getElementById('petRun'), runCtx=runCv&&runCv.getContext('2d');
  var railCtx=cv.getContext('2d');
  var acc=(getComputedStyle(document.documentElement).getPropertyValue('--accent')||'').trim()||'#e6873a';
  function drk(hex,f){ var n=parseInt(hex.replace('#',''),16); return 'rgb('+Math.round(((n>>16)&255)*f)+','+Math.round(((n>>8)&255)*f)+','+Math.round((n&255)*f)+')'; }
  var P={K:'#241a1e',G:acc,D:drk(acc,.72),C:'#f3e7cf',E:'#3f9fd6',W:'#f6fbff',N:'#cf7280',I:'#e3a6ac',
    M:'#c2c7d4',Md:'#767c8f',WD:'#8a5a30',Gd:'#e8c14e',R:'#e0503a',Pu:'#9b7be6',FL:'#ffce4d',FLo:'#ff8a3a',
    WH:'#f3f0fb',GL:'rgba(120,190,240,.9)',GR:'#4bd08a',HRT:'#ff6b81'};
  var S=[
    "...K............K...",
    "..KIK..........KIK..",
    "..KIIK........KIIK..",
    ".KGGGGGGGGGGGGGGGGK.",
    ".KGGGGGGGGGGGGGGGGK.",
    ".KGGGGGGGGGGGGGGGGK.",
    ".KGGEEGGGGGGGEEGGGK.",
    ".KGGEWGGGGGGGEWGGGK.",
    ".KGGEEGGGGGGGEEGGGK.",
    ".KGGGGGGNNGGGGGGGGK.",
    ".KGGGGCCCCCCCGGGGGK.",
    "..KGGCCCCCCCCCGGGK..",
    "..KKGGGGGGGGGGGKK...",
    ".KGGGGGGGGGGGGGGGK..",
    "KGGGGGGGGGGGGGGGGGK.",
    "KGGGGCCCCCCCCCGGGGK.",
    "KGGGGCCCCCCCCCGGGGK.",
    ".KGGGGGGGGGGGGGGGGK.",
    "..KKGGGGGGGGGGGGKK.."
  ];
  var XO=3, g=null, GW=0;
  function px(x,y,c){ if(x<0||y<0||x>=GW||y>=g.canvas.height) return; g.fillStyle=c; g.fillRect(x,y,1,1); }
  function rct(x,y,w,h,c){ g.fillStyle=c; g.fillRect(x,y,w,h); }
  function paw(x,y){ rct(x,y,3,3,P.K); rct(x,y,3,2,P.G); px(x+1,y+2,P.K); }
  function tail(yo,dx,up){ var cs=up?[[16,13],[17,12],[18,12],[18,11]]:[[16,14],[17,15],[18,15],[18,16]]; cs.forEach(function(p){px(p[0]+XO+dx,p[1]+yo,P.G);}); var e=cs[cs.length-1]; px(e[0]+XO+dx,e[1]+yo,P.D); }
  function body(yo,dx,ph,run,blink,hh){
    tail(yo,dx, run?(ph<2):true);
    for(var r=0;r<S.length;r++){ var row=S[r]; for(var c=0;c<row.length;c++){ var ch=row[c]; if(ch==='.')continue; if(hh && r<=5) continue; px(c+XO+dx, r+yo, P[ch]); } }
    px(6+XO+dx,4+yo,P.D); px(8+XO+dx,4+yo,P.D); px(11+XO+dx,4+yo,P.D);
    if(!blink){ px(5+XO+dx,7+yo,P.K); px(14+XO+dx,7+yo,P.K); }
    else { [[4,7],[5,7],[13,7],[14,7]].forEach(function(p){px(p[0]+XO+dx,p[1]+yo,P.G);}); [[4,7],[5,7],[13,7],[14,7]].forEach(function(p){px(p[0]+XO+dx,p[1]+yo,P.K);}); }
    var a=run?(ph<2?0:-1):0, b=run?(ph<2?-1:0):0;
    paw(5+XO+dx,19+yo+a); paw(12+XO+dx,19+yo+b);
  }
  function helm(yo,dx){ var x=XO+dx; rct(x+4,2+yo,10,4,P.M); rct(x+3,5+yo,12,1,P.Md); px(x+9,1+yo,P.R); px(x+9,0+yo,P.R); rct(x+9,6+yo,1,3,P.Md); }
  function crown(yo,dx){ var x=XO+dx; rct(x+4,2+yo,10,3,P.Gd); px(x+4,1+yo,P.Gd); px(x+9,0+yo,P.Gd); px(x+13,1+yo,P.Gd); px(x+9,3+yo,P.R); }
  function wizard(yo,dx){ var x=XO+dx; rct(x+7,2+yo,5,3,P.Pu); px(x+8,1+yo,P.Pu); px(x+9,0+yo,P.Pu); px(x+9,-1+yo,P.Gd); }
  function shield(yo){ rct(1,11+yo,4,8,P.Md); rct(0,12+yo,5,6,P.M); rct(2,14+yo,1,3,P.R); px(2,13+yo,P.Gd); }
  function spear(yo,dx){ var x=23+dx; for(var y=4;y<=20;y++) px(x,y+yo,P.WD); px(x,3+yo,P.M); px(x-1,4+yo,P.M); px(x+1,4+yo,P.M); px(x,2+yo,P.M); }
  function clock(yo,ring){ rct(0,9+yo,7,8,P.Md); rct(1,10+yo,5,6,P.WH); px(3,11+yo,P.K); px(2,13+yo,P.K); px(3,13+yo,P.K); px(0,8+yo,P.Md); px(6,8+yo,P.Md); px(0,17+yo,P.Md); px(6,17+yo,P.Md); if(ring){ px(-1,8+yo,P.Gd); px(7,8+yo,P.Gd); px(-1,11+yo,P.Gd); px(7,11+yo,P.Gd); } }
  function torch(yo,f){ var x=23; for(var y=10;y<=19;y++) px(x,y+yo,P.WD); px(x,9+yo,P.FLo); px(x,8+yo-(f?1:0),P.FL); px(x-1,9+yo,P.FLo); px(x+1,9+yo,P.FLo); }
  function glass(yo){ var x=22; rct(x,6+yo,5,5,P.Md); rct(x+1,7+yo,3,3,P.GL); px(x+4,11+yo,P.WD); px(x+5,12+yo,P.WD); }
  function binoc(yo){ var x=22; rct(x,7+yo,2,4,P.Md); rct(x+3,7+yo,2,4,P.Md); px(x+1,8+yo,P.GL); px(x+4,8+yo,P.GL); }
  function cross(yo){ rct(20,3+yo,3,1,P.GR); rct(21,2+yo,1,3,P.GR); px(23,5+yo,P.HRT); }
  function coins(yo,t){ px(1,(6+(t*2)%12)+yo,P.Gd); px(24,(4+((t*2)+5)%12)+yo,P.Gd); }
  function zzz(yo){ px(20,4,P.WH); px(21,3,P.WH); px(22,2,P.WH); }

  function drawScene(ctx, cw, o){
    g=ctx; GW=cw; ctx.clearRect(0,0,cw,ctx.canvas.height);
    var id=o.id, working=o.working, sleeping=o.sleeping, ev=o.event, t=o.t;
    var ph=Math.floor(o.roam?t/4:t)%4;
    var bob=working?(ph<2?0:1):(sleeping?2:1);
    var hop=0; if(o.jump){ var e=performance.now()-o.jump; if(e<620) hop=-Math.round(Math.sin(e/620*Math.PI)*6); }
    var jit=0;
    if(ev&&id==='raids'){ if(ev.kind==='alarm') jit=(t%2?1:-1); if(ev.kind==='win'&&!o.jump&&ph>=2) hop-=2; }
    var yo=2+bob+hop, dx=((working&&id==='raids')?(ph<2?0:1):0)+jit;
    var helmet=working&&id==='raids'&&!(ev&&ev.kind==='heal');
    if(id==='raids') shield(yo);
    if(id==='caves') torch(yo,ph<2);
    body(yo,dx,ph,working,(t%24<2)||sleeping,helmet);
    if(sleeping){ zzz(yo); }
    else if(id==='raids'){ if(helmet) helm(yo,dx); spear(yo,dx); }
    else if(id==='roles'){ var k=Math.floor(t/8)%3; (k===0?helm:k===1?crown:wizard)(yo,0); }
    else if(id==='alarms'){ clock(yo, working&&ph<2); }
    else if(id==='find'){ glass(yo); }
    else if(id&&id!=='caves'&&!o.roam){ binoc(yo); }
    if(ev&&id==='raids'){ if(ev.kind==='heal') cross(yo); if(ev.kind==='win'||ev.kind==='loot') coins(yo,t); }
  }

  function label(id,working,sleeping,ev){
    if(sleeping) return '😴 Спит';
    if(ev&&id==='raids') return ev.emoji+' '+ev.text;
    if(id==='raids') return working?'⚔️ В бою':'🛡️ Наготове';
    if(id==='roles') return '🎭 Переодевается';
    if(id==='alarms') return working?'⏰ Заводит':'⏰ Будильник';
    if(id==='caves') return '🕳️ В пещере';
    if(id==='find') return '🔎 Ищет цели';
    if(id) return working?'🔭 Разведка':'😺 Ждёт';
    return working?'🐾 Бежит':'😺 Ждёт';
  }

  var t=0, jumpAt=0, emote=null;
  var MEOWS=['мяу','мур','🐟','❤️','😺','🐾'];
  function frame(){
    var mode=(petState||'run'), id=(active||'');
    var working=mode==='run', sleeping=mode==='idle'||mode==='stopped';
    var ev=(petEvent && performance.now()-petEvent.at<3500)?petEvent:null;
    var jmp=(jumpAt&&performance.now()-jumpAt<620)?jumpAt:0;
    drawScene(railCtx, cv.width, {id:id,working:working,sleeping:sleeping,event:ev,t:t,jump:jmp,roam:false});
    var b='';
    if(emote && performance.now()-emote.at<1500) b=emote.txt;
    else if(ev && id==='raids') b=ev.emoji;
    if(bub){ bub.textContent=b; bub.style.opacity=b?'1':'0'; }
    var lb=label(id,working,sleeping, emote?null:ev);
    if(say && say.textContent!==lb) say.textContent=lb;
    t++;
  }
  frame(); setInterval(frame, 150);

  cv.title='Тыкни меня 🐾 (двойной клик — пробегусь)';
  cv.addEventListener('click', function(){ jumpAt=performance.now(); emote={txt:MEOWS[Math.floor(Math.random()*MEOWS.length)], at:performance.now()}; });

  var roaming=false;
  function roam(){
    if(roaming||!runCtx) return; roaming=true; runCv.style.display='block';
    var w=window.innerWidth, x=-110, rt=0;
    (function step(){
      x+=6; runCv.style.left=x+'px';
      drawScene(runCtx, runCv.width, {id:'',working:true,sleeping:false,event:null,t:rt,jump:0,roam:true});
      rt++;
      if(x<w+40) requestAnimationFrame(step);
      else { roaming=false; runCv.style.display='none'; }
    })();
  }
  cv.addEventListener('dblclick', roam);
  setInterval(function(){ if(!roaming && document.visibilityState!=='hidden' && Math.random()<0.3) roam(); }, 42000);
})();

init();
</script></body></html>
"""


LOGIN_PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🏰 Холоп — Вход</title>
<style>
 :root{color-scheme:light dark;
   --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,Helvetica,Arial,sans-serif;
   --bg:#1f1830;--card:#292140;--elev:#372d55;--ink:#f0eafa;--mut:#b2a6ce;--accent:#e6873a;
   --line:rgba(185,160,255,.12);--green:#3fbe86;--red:#f05a6b;--blue:#e6873a;
   --shadow:0 24px 60px rgba(0,0,0,.62),0 1px 0 rgba(255,255,255,.05) inset;}
 @media (prefers-color-scheme:light){:root{--bg:#efe9f9;--card:#ffffff;--elev:#ffffff;--ink:#2a2340;--mut:#6f6690;--accent:#d9772a;
   --line:rgba(110,80,190,.12);--green:#1fa877;--red:#e24a5c;--blue:#d9772a;--shadow:0 24px 60px rgba(80,55,150,.2);}}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;
   color:var(--ink);letter-spacing:-.011em;-webkit-font-smoothing:antialiased;
   font:15px/1.5 var(--font);
   background:radial-gradient(900px 500px at 50% -10%,color-mix(in srgb,var(--accent) 15%,transparent),transparent 60%),var(--bg)}
 .box{width:380px;max-width:94vw;background:var(--card);border:.5px solid var(--line);
   border-radius:22px;padding:30px 26px;box-shadow:var(--shadow)}
 h1{font-size:23px;margin:0 0 5px;font-weight:750;letter-spacing:-.02em;color:var(--ink)}
 .sub{color:var(--mut);font-size:13.5px;margin:0 0 20px;line-height:1.45}
 label{display:block;color:var(--mut);font-size:12px;font-weight:510;margin:14px 0 5px}
 input{width:100%;background:color-mix(in srgb,var(--elev) 60%,transparent);color:var(--ink);
   border:.5px solid var(--line);border-radius:12px;padding:13px;font:16px var(--font);outline:none;
   transition:border-color .18s,box-shadow .18s}
 input:focus{border-color:var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 24%,transparent)}
 button{width:100%;margin-top:18px;font:600 15px var(--font);letter-spacing:-.01em;border:0;border-radius:13px;
   padding:14px;cursor:pointer;color:#fff;box-shadow:0 1px 2px rgba(0,0,0,.2);
   background:linear-gradient(180deg,color-mix(in srgb,var(--accent) 90%,#fff),var(--accent));
   transition:transform .09s ease,filter .15s}
 button:hover{filter:brightness(1.07)} button:active{transform:scale(.985)} button:disabled{opacity:.5;cursor:default}
 button.ghost{background:transparent;border:.5px solid var(--line);color:var(--mut);font-weight:500;font-size:13.5px;margin-top:10px}
 .link{margin-top:14px;text-align:center;color:var(--mut);font-size:13px;cursor:pointer}
 .link:hover{color:var(--ink)}
 .note{color:var(--mut);font-size:12.5px;min-height:18px;margin-top:12px}
 .note.err{color:var(--red)} .note.ok{color:var(--green)}
 .safe{margin-top:20px;padding-top:16px;border-top:.5px solid var(--line);
   color:var(--mut);font-size:12px;line-height:1.55}
 .hide{display:none}
 @media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>
<div class="box">
  <h1>🏰 Вход в Холоп</h1>
  <p class="sub">Войди своим Telegram — пульт будет работать на твоём аккаунте.
    <span style="color:var(--mut)">v__VERSION__</span></p>

  <div id="step-phone">
    <label>Номер телефона (как в Telegram)</label>
    <input id="phone" type="tel" placeholder="+79991234567" autocomplete="tel">
    <button id="btn-phone" onclick="sendCode()">Получить код</button>
  </div>

  <div id="step-code" class="hide">
    <label>Код из Telegram (придёт в приложение)</label>
    <input id="code" type="text" inputmode="numeric" placeholder="12345" autocomplete="one-time-code">
    <button id="btn-code" onclick="signIn()">Войти</button>
    <button id="btn-sms" class="ghost" onclick="sendCode(true)">Код не пришёл — прислать по SMS</button>
    <div class="link" onclick="back()">← другой номер</div>
  </div>

  <div id="step-pass" class="hide">
    <label>Пароль двухфакторной защиты (2FA)</label>
    <input id="pass" type="password" placeholder="твой облачный пароль">
    <button id="btn-pass" onclick="signInPass()">Войти</button>
  </div>

  <div class="note" id="note"></div>
  <div class="safe">🔒 Твой вход хранится только на этом компьютере (файл config.json).
    Никуда не отправляется. Это игровой самобот — используешь на свой риск.</div>
</div>
<script>
const $=s=>document.getElementById(s);
function show(id){['step-phone','step-code','step-pass'].forEach(x=>$(x).classList.toggle('hide',x!==id));}
function note(t,cls){const n=$('note');n.textContent=t||'';n.className='note'+(cls?' '+cls:'');}
async function api(path,data){
  try{
    const r=await fetch('/api/auth/'+path,{method:'POST',body:JSON.stringify(data||{})});
    return await r.json();
  }catch(e){
    return {ok:false, err:'Панель не ответила. Проверь, что окно пульта не закрыто, и попробуй ещё раз.'};
  }
}
async function sendCode(sms){
  const phone=$('phone').value.trim();
  if(!phone){note('Введи номер.','err');return;}
  const b = sms ? $('btn-sms') : $('btn-phone');
  if(b) b.disabled=true;
  note(sms?'Запрашиваю SMS…':'Отправляю код…');
  const d=await api('send_code',{phone, sms:!!sms});
  if(b) b.disabled=false;
  if(!d.ok){note(d.err||'Ошибка','err');return;}
  note(d.where||'Код отправлен.', d.kind==='sms'?'ok':'');
  show('step-code'); $('code').focus();
}
async function signIn(){
  const code=$('code').value.trim();
  if(!code){note('Введи код.','err');return;}
  $('btn-code').disabled=true; note('Проверяю…');
  const d=await api('sign_in',{code});
  $('btn-code').disabled=false;
  if(!d.ok){note(d.err||'Ошибка','err');return;}
  if(d.need==='password'){note('Нужен пароль 2FA.'); show('step-pass'); $('pass').focus(); return;}
  done();
}
async function signInPass(){
  const password=$('pass').value;
  if(!password){note('Введи пароль.','err');return;}
  $('btn-pass').disabled=true; note('Проверяю…');
  const d=await api('password',{password});
  $('btn-pass').disabled=false;
  if(!d.ok){note(d.err||'Ошибка','err');return;}
  done();
}
function back(){show('step-phone'); note('');}
function done(){note('✅ Готово! Открываю пульт…','ok'); setTimeout(()=>location.href='/',900);}
$('phone').addEventListener('keydown',e=>{if(e.key==='Enter')sendCode();});
$('code').addEventListener('keydown',e=>{if(e.key==='Enter')signIn();});
$('pass').addEventListener('keydown',e=>{if(e.key==='Enter')signInPass();});
</script></body></html>
"""


def main():
    ensure_config()
    url = f"http://127.0.0.1:{PORT}/"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        print(f"Пульт уже запущен: {url}")
        webbrowser.open(url)
        return
    if IS_WIN:
        _win_sweep_orphans()   # чистим осиротевшие боты от прошлого закрытого окна (чтоб не было двух)
    tail_note = ("Это окно — сервер пульта. Закроешь окно — всё остановится."
                 if IS_WIN else "Окно можно свернуть/закрыть — на ботов не влияет.")
    print(f"🏰 Пульт Холопа v{VERSION}: {url}\n({tail_note})")
    if not os.environ.get("HOLOP_NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def _write_crash(exc):
    """Пишет полную ошибку в hub_error.log — чтобы её можно было прислать и починить."""
    import traceback
    import platform
    try:
        with open(os.path.join(HERE, "hub_error.log"), "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 64 + "\n")
            f.write("КРАШ ПУЛЬТА  " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            f.write("Python: " + sys.version.replace("\n", " ") + "\n")
            f.write("OS: " + platform.platform() + "\n")
            f.write("Папка: " + HERE + "\n")
            f.write("-" * 64 + "\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            f.write("=" * 64 + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        _write_crash(e)
        print("\n!!! Пульт упал с ошибкой.")
        print("    Полностью записано в файл  hub_error.log")
        print("    Пришли этот файл — ошибку разберут и починят.\n")
        print(repr(e))
