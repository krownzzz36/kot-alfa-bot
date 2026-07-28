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

VERSION = "2026.07.28-40"   # видно в консоли и в шапке панели — чтобы понимать, свежая ли версия
PY = sys.executable or "python3"


def _resolve_port():
    """Порт панели. Для МУЛЬТИОКОННОСТИ (несколько аккаунтов в разных папках) второй
    аккаунт задаёт свой порт: файл port.txt рядом со скриптом ИЛИ "port" в config.json.
    Иначе — 8777. Env HOLOP_PORT (для тестов) в приоритете."""
    env = (os.environ.get("HOLOP_PORT") or "").strip()
    if env.isdigit():
        return int(env)
    try:
        with open(os.path.join(HERE, "port.txt"), encoding="utf-8") as f:
            v = f.read().strip()
        if v.isdigit():
            return int(v)
    except OSError:
        pass
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            p = json.load(f).get("port")
        if str(p).isdigit():
            return int(p)
    except (OSError, ValueError, TypeError):
        pass
    return 8777


PORT = _resolve_port()


def _script_path(name):
    """Полный путь к скрипту в ЭТОЙ папке. Мультиоконность: боты запускаем и ищем
    (pgrep/pkill) по полному пути — чтобы второй аккаунт из другой папки не глушил
    ботов первого (у Алексея из-за этого крашилось)."""
    return os.path.join(HERE, name)


SMASH_PATH = _script_path("holop_smash.py")

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
        "id": "guard", "title": "Защита от бочек", "emoji": "🛡️", "kind": "guard",
        "desc": "Спокойный режим: не фармит, но делает безопасное — сторожит бочки, казна, охрана холопов.",
    },
    {
        "id": "oko", "title": "Око Саурона", "emoji": "🔥", "kind": "oko",
        "desc": "Бета. Мордор следит. Вписывай ников — Око покажет их живьём (HP/щит/КД) и даст «пнуть». Данные локальные, без запросов к игре.",
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
                     "options": ["Нет", "Да"], "default": "Нет"}],
        "actions": [{"id": "run", "label": "🔎 Найти цели"},
                    {"id": "dry", "label": "Холостой (dry-run)"}],
    },
    {
        "id": "game", "title": "Барская игра", "emoji": "🎮", "kind": "game",
        "desc": "Тамагочи-сатира: год в поместье — усадьба, сюжетные истории, 16 финалов. Ачивки и слава рода копятся между забегами.",
    },
    {
        "id": "guide", "title": "Гайд", "emoji": "📖", "kind": "guide",
        "desc": "Как всё работает: каждая вкладка, каждая кнопка и галочка — по порядку.",
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
        # набеги мог поднять и «Запустить» (smash.pid), и «Ночной режим» (night_smash.sh).
        # pgrep по ПОЛНОМУ пути — не считать «запущенным» бота другого аккаунта.
        return alive or _pgrep(SMASH_PATH) or night_running()
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
        for pat in (NIGHT_SH, SMASH_PATH):    # полный путь — только боты ЭТОЙ папки
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


def collect_logs():
    """Один текстовый файл со всеми логами для поддержки — чтобы друг прислал
    одним кликом, а не искал файлы в папке. Секретов тут нет: session_string и
    config.json НЕ включаем, только диагностические логи."""
    import platform as _pl
    head = ("КОТ АЛЬФА БОТ — логи для поддержки\n"
            f"версия: {VERSION}\n"
            f"система: {_pl.platform()}  python {_pl.python_version()}\n"
            + "=" * 60 + "\n")
    parts = [head]
    for fname, n in (("startup_log.txt", 400), ("hub_error.log", 400),
                     ("auth_log.txt", 200), ("smash.log", 400),
                     ("update_log.txt", 120)):
        try:
            with open(path(fname), encoding="utf-8", errors="replace") as f:
                body = "".join(f.readlines()[-n:]).strip()
        except OSError:
            body = "(файла нет)"
        parts.append(f"\n\n━━━ {fname} ━━━\n{body or '(пусто)'}")
    return "\n".join(parts)


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
    p = subprocess.Popen([PY, SMASH_PATH], cwd=HERE, stdout=out, stderr=out,
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
            subprocess.run(["pkill", "-f", SMASH_PATH],   # полный путь — не задеть другой аккаунт
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
                           "auto_oboz": False, "war_mode": False, "human_mode": False,
                           "notify_dm": False, "free_hunt": False,
                           "req_delay_lo": 1.5, "req_delay_hi": 3.5,
                           "bomb_defense": True, "defense_only": False,
                           "bomb_gold_kazna": True, "auto_guard": False,
                           "sauron_mode": False}


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
        out["human_mode"] = bool(body.get("human_mode", cur["human_mode"]))
        out["notify_dm"] = bool(body.get("notify_dm", cur["notify_dm"]))
        out["free_hunt"] = bool(body.get("free_hunt", cur["free_hunt"]))
        lo = max(0.2, min(float(body.get("req_delay_lo", cur["req_delay_lo"])), 20))
        hi = max(lo, min(float(body.get("req_delay_hi", cur["req_delay_hi"])), 20))
        out["req_delay_lo"], out["req_delay_hi"] = round(lo, 1), round(hi, 1)
        out["bomb_defense"] = bool(body.get("bomb_defense", cur["bomb_defense"]))
        out["defense_only"] = bool(body.get("defense_only", cur["defense_only"]))
        out["bomb_gold_kazna"] = bool(body.get("bomb_gold_kazna", cur["bomb_gold_kazna"]))
        out["auto_guard"] = bool(body.get("auto_guard", cur["auto_guard"]))
        out["sauron_mode"] = bool(body.get("sauron_mode", cur["sauron_mode"]))   # 🔥 режим Саурона
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
        if (f.get("skip_def") or "Нет").strip().lower().startswith("да"):
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
    p = subprocess.Popen([PY, _script_path(mod["script"]), *args], cwd=HERE, stdout=out, stderr=out,
                         **POPEN_KW)
    with open(pidfile(mid), "w") as f:
        f.write(str(p.pid))
    return {"ok": True}


def oneshot_stop(mid):
    _terminate(read_pid(mid))
    if not IS_WIN:
        try:
            subprocess.run(["pkill", "-f", _script_path(MOD[mid]["script"])],
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
    likes = " -or ".join(f"$_.CommandLine -like '*{_script_path(s)}*'" for s in BOT_SCRIPTS)
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
                subprocess.run(["pkill", "-f", _script_path(s)],   # только боты ЭТОЙ папки
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


# ─────────────── 🔥 ОКО САУРОНА ───────────────
# Живое состояние целей ТОЛЬКО из локальных файлов кота (0 запросов к игре):
# cd_cache.json (КД), shielded.json (щит), smash.log (последние HP/защита/уровень).
OKO_HP_RX = re.compile(r"HP\s*(\d+),\s*защ\.→\s*(\d+),\s*ур\.(\d+)")


def load_oko_targets():
    try:
        with open(path("oko_targets.txt"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def save_oko_targets(text):
    try:
        with open(path("oko_targets.txt"), "w", encoding="utf-8") as f:
            f.write(text or "")
        return True
    except OSError:
        return False


def oko_hit(name):
    """«Пнуть» — дописать ник в oko_hits.txt; смашер подхватит (приоритет/немедленно)."""
    name = (name or "").strip()
    if not name:
        return False
    try:
        with open(path("oko_hits.txt"), "a", encoding="utf-8") as f:
            f.write(name + "\n")
        return True
    except OSError:
        return False


def oko_set_sauron(on):
    """Тумблер режима Саурона — пишем в настройки боя, смашер подхватит на лету."""
    s = load_smash_settings()
    s["sauron_mode"] = bool(on)
    return save_smash_settings(s)


def oko_cards(nicks):
    now = time.time()
    try:
        with open(path("cd_cache.json"), encoding="utf-8") as f:
            cd = (json.load(f).get("next_ok") or {})
    except (OSError, ValueError, TypeError):
        cd = {}
    try:
        with open(path("shielded.json"), encoding="utf-8") as f:
            sh = json.load(f)
    except (OSError, ValueError, TypeError):
        sh = {}
    try:
        with open(path("smash.log"), encoding="utf-8", errors="replace") as f:
            lines = [ln for ln in f if "\\x" not in ln]
    except OSError:
        lines = []
    cards = []
    for name in nicks:
        cd_until = float(cd.get(name) or 0)
        sh_until = float(sh.get(name) or 0)
        hp = lvl = dfn = None
        state, when = "", ""
        for ln in reversed(lines):
            if (name + ":") in ln:
                m = OKO_HP_RX.search(ln)
                if m:
                    hp, dfn, lvl = int(m.group(1)), int(m.group(2)), int(m.group(3))
                for rx, tmpl in STATE_PATTERNS:
                    mm = rx.search(ln)
                    if mm:
                        state = tmpl.format(*[g.strip() for g in mm.groups()]) if mm.groups() else tmpl
                        break
                tm = ln.split("  ", 1)[0]
                when = tm[11:19] if len(tm) >= 19 else ""
                break
        cards.append({"name": name, "hp": hp, "level": lvl, "defense": dfn,
                      "cd_until": cd_until, "shield_until": sh_until,
                      "open": cd_until <= now and sh_until <= now,
                      "state": state, "when": when})
    s = load_smash_settings()
    return {"cards": cards, "now": now,
            "running": is_running("raids"),
            "defense_only": bool(s.get("defense_only")),
            "sauron": bool(s.get("sauron_mode"))}


def oko_cards_saved():
    nicks = [x.split("#", 1)[0].strip()
             for x in load_oko_targets().splitlines() if x.split("#", 1)[0].strip()]
    return oko_cards(nicks)


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
        elif p == "/api/version":
            self._json({"version": VERSION})       # для авто-перезагрузки страницы при обнове
        elif p == "/api/logs":
            data = collect_logs().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="kot-alfa-logs.txt"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
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
        elif p == "/api/oko/cards":
            self._json(oko_cards_saved())
        elif p == "/api/oko/targets":
            self._send(200, load_oko_targets(), "text/plain; charset=utf-8")
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
        # ── 🔥 Око Саурона ──
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "oko":
            act = parts[2]
            if act == "save":
                return self._json({"ok": save_oko_targets(raw if not body else body.get("text", ""))})
            if act == "hit":
                return self._json({"ok": oko_hit(body.get("name", ""))})
            if act == "sauron":
                return self._json({"ok": oko_set_sauron(bool(body.get("on")))})
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
   --cat:#e6873a;   /* цвет шерсти кота — в чёрной теме тёмный */
   --shadow:0 1px 2px rgba(0,0,0,.32),0 20px 46px -16px rgba(0,0,0,.6);}
 @media (prefers-color-scheme:light){:root:not([data-theme]){
   --bg:#efe9f9;--panel:#ffffff;--panel2:#f7f3fd;--elev:#ffffff;--ink:#2a2340;--mut:#6f6690;--faint:#a79fc4;
   --line:rgba(110,80,190,.10);--line2:rgba(110,80,190,.05);
   --accent:#d9772a;--blue:#d9772a;--green:#1fa877;--red:#e24a5c;--grey:#efe9fb;--purple:#7c5cf5;--amber:#c07020;
   --cat:#d9772a;
   --shadow:0 1px 2px rgba(60,40,110,.06),0 18px 40px -14px rgba(80,55,150,.16);}}
 /* 🌑 МАКСИМАЛЬНО ЧЁРНАЯ ТЕМА (по переключателю) — кот-ниндзя, всё чёрное, читаемо */
 :root[data-theme="black"]{color-scheme:dark;
   --bg:#000000;--panel:#0a0a0d;--panel2:#101015;--elev:#17171d;
   --ink:#eef0f5;--mut:#969aa6;--faint:#5f616b;--line:rgba(255,255,255,.10);--line2:rgba(255,255,255,.05);
   --accent:#5b9dff;--blue:#5b9dff;--green:#37c98a;--red:#ff5a6b;--grey:#17171d;--purple:#8f7bff;--amber:#5b9dff;
   --cat:#3b3b45;
   --shadow:0 1px 2px #000,0 24px 52px -16px #000;}
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
 /* рабочая зона: слева список настроек, справа консоль — аккордеоны открываются
    ВНИЗ НА МЕСТЕ и НЕ двигают консоль (просьба Максима — без дёрготни) */
 .workspace{display:grid;grid-template-columns:minmax(330px,430px) 1fr;gap:20px;align-items:start;
   animation:rise .42s .05s both}
 .controls{display:flex;flex-direction:column;gap:12px;min-width:0}
 .acc{background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden;box-shadow:var(--shadow);
   transition:box-shadow .25s,border-color .25s}
 .acc:hover{border-color:color-mix(in srgb,var(--accent) 38%,var(--line))}
 .acc[open]{box-shadow:0 1px 2px rgba(0,0,0,.32),0 24px 50px -18px color-mix(in srgb,var(--accent) 40%,#0009)}
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
 .console{animation:rise .42s .1s both;background:var(--panel);border:1px solid var(--line);border-radius:22px;overflow:hidden;box-shadow:var(--shadow);
   position:sticky;top:18px;display:flex;flex-direction:column;max-height:calc(100vh - 36px)}
 .console-bar{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line2)}
 .live{display:inline-flex;align-items:center;gap:7px;font:700 10.5px var(--mono);letter-spacing:.2em;color:var(--green);
   padding:4px 10px;border-radius:99px;background:color-mix(in srgb,var(--green) 13%,transparent)}
 .live::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--green);animation:blip 1.6s ease-in-out infinite}
 .console-name{font:12.5px var(--mono);color:var(--mut)}
 .console-meta{margin-left:auto;font:11px var(--mono);color:var(--faint)}
 pre.log{flex:1 1 auto;min-height:260px;overflow:auto;background:transparent;
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
   .workspace{grid-template-columns:1fr}
   .console{position:static;max-height:none}   /* на телефоне не липкая — иначе съедает экран */
   pre.log{max-height:56vh;min-height:320px;flex:none}
 }
 @media (max-width:430px){.brand-tx{font-size:15px}.btns button{min-width:0}}
 /* ── 🎮 БАРСКАЯ ИГРА ── */
 #gRoot{font-size:14px}
 .g-top{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:16px}
 .g-kick{font-size:10px;letter-spacing:.28em;color:var(--accent);text-transform:uppercase}
 .g-h1{font-size:40px;line-height:1;font-weight:750;letter-spacing:-.01em;margin-top:6px;color:var(--ink)}
 .g-sub{font-style:italic;font-size:14px;color:var(--mut);margin-top:5px}
 .g-topr{display:flex;align-items:center;gap:11px;flex-wrap:wrap}
 .g-season{font-size:12px;padding:6px 12px;border-radius:999px;background:var(--panel2);border:1px solid var(--line);color:var(--ink);white-space:nowrap}
 .g-lamp{width:11px;height:11px;border-radius:50%;background:var(--green);box-shadow:0 0 12px var(--green)}
 .g-stat{font-size:11.5px;letter-spacing:.18em;text-transform:uppercase;color:var(--green)}
 .g-hud{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px;align-items:stretch}
 .g-hud .g-h{flex:1;min-width:104px;padding:11px 14px}
 .g-hud .g-btn-ghost{align-self:stretch}
 .g-big{font-size:26px;line-height:1.15;margin-top:2px;color:var(--ink);font-weight:700;font-variant-numeric:tabular-nums}
 .g-card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px 18px;box-shadow:var(--shadow)}
 .g-grid{display:grid;grid-template-columns:308px 1fr;gap:16px;align-items:start}
 .g-col{display:flex;flex-direction:column;gap:14px;min-width:0}
 .g-row{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
 .g-face{position:relative;aspect-ratio:1/1;border-radius:12px;border:1px solid var(--line);background:var(--panel2);
   display:grid;place-items:center;padding:12px;margin-top:10px;overflow:hidden}
 .g-face canvas{width:100%;image-rendering:pixelated}
 .g-flash{position:absolute;left:0;right:0;bottom:8px;display:flex;flex-direction:column;align-items:center;gap:2px;pointer-events:none}
 .g-fl{font:600 12px var(--mono);color:var(--accent);background:color-mix(in srgb,var(--panel) 86%,transparent);
   border:1px solid var(--line);border-radius:8px;padding:2px 8px;animation:gfl 1.5s ease-out forwards;white-space:nowrap;max-width:96%;overflow:hidden;text-overflow:ellipsis}
 @keyframes gfl{0%{opacity:0;transform:translateY(8px)}18%{opacity:1;transform:translateY(0)}75%{opacity:1}100%{opacity:0;transform:translateY(-14px)}}
 .g-name{font-size:19px;font-weight:700;margin-top:10px;color:var(--ink);display:flex;align-items:center;gap:8px;flex-wrap:wrap}
 .g-trait{font-size:11px;font-weight:600;padding:3px 9px;border-radius:999px;cursor:help;
   background:color-mix(in srgb,var(--accent) 16%,transparent);border:1px solid color-mix(in srgb,var(--accent) 40%,transparent);color:var(--accent)}
 .g-dim{font-size:12px;color:var(--mut);margin-top:2px;line-height:1.4}
 .g-brow{margin-bottom:9px}
 .g-blab{display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;color:var(--mut)}
 .g-blab b{font-weight:700;font-variant-numeric:tabular-nums}
 .g-bar{height:10px;border-radius:6px;background:var(--panel2);border:1px solid var(--line);overflow:hidden}
 .g-bar i{display:block;height:100%;border-radius:5px;transition:width .45s cubic-bezier(.4,1.3,.5,1),background .3s}
 .g-ach{display:flex;flex-wrap:wrap;gap:6px}
 .g-a{width:30px;height:30px;display:grid;place-items:center;border-radius:9px;font-size:16px;cursor:help;
   background:var(--panel2);border:1px solid var(--line);filter:grayscale(1);opacity:.32;transition:.2s}
 .g-a.on{filter:none;opacity:1;border-color:var(--accent);box-shadow:0 0 12px -3px var(--accent)}
 .g-modes{display:flex;gap:5px;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:4px}
 .g-modes button{font-size:12px;padding:7px 12px;border-radius:7px;border:0;cursor:pointer;background:transparent;color:var(--mut);box-shadow:none}
 .g-modes button.on{background:var(--accent);color:#2a1206}
 .g-cmds{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));gap:10px;margin-top:12px}
 .g-cmds button{position:relative;overflow:hidden;text-align:left;padding:13px;border-radius:11px;border:1px solid var(--line);
   background:var(--panel2);color:var(--ink);box-shadow:none;transition:transform .12s,border-color .2s,opacity .2s}
 .g-cmds button:hover:not(:disabled){transform:translateY(-2px);border-color:var(--accent)}
 .g-cmds button:disabled{opacity:.42;cursor:default}
 .g-cmds button.done{opacity:.28}
 .g-ci{font-size:21px;line-height:1}
 .g-cl{font-size:14.5px;font-weight:650;margin-top:7px}
 .g-ch{font-size:10.5px;color:var(--mut);margin-top:2px;line-height:1.3}
 .g-cdbar{position:absolute;left:0;bottom:0;height:3px;width:0;background:var(--accent);opacity:.75;transition:width .9s linear}
 .g-red{margin-top:13px;width:100%;font-size:19px;font-weight:700;padding:15px;border-radius:12px;
   border:1px solid color-mix(in srgb,var(--red) 60%,transparent);color:#ffe6e2;
   background:linear-gradient(180deg,#c0433a,#8f2a22);transition:opacity .2s,transform .12s}
 .g-red:hover:not(:disabled){transform:translateY(-1px)}
 .g-red:disabled{opacity:.4;cursor:default}
 .g-red span{display:block;font-size:10px;letter-spacing:.13em;opacity:.85;margin-top:4px;text-transform:uppercase;font-weight:400}
 .g-shop{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:10px}
 .g-bld{text-align:left;padding:12px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);
   color:var(--ink);box-shadow:none;transition:transform .12s,border-color .2s,opacity .2s}
 .g-bld:hover:not(:disabled){transform:translateY(-2px);border-color:var(--accent)}
 .g-bld:disabled{cursor:default}
 .g-bld.poor{opacity:.45}
 .g-bld.own{opacity:1;border-color:color-mix(in srgb,var(--green) 55%,transparent);
   background:color-mix(in srgb,var(--green) 12%,var(--panel2))}
 .g-bld.own .g-ch{color:var(--green)}
 .g-bd{font-size:10.5px;color:var(--faint);margin-top:5px;line-height:1.32}
 .g-perks{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:9px}
 .g-perk{display:grid;grid-template-columns:auto 1fr auto;grid-template-areas:"i t c" "d d d";gap:3px 9px;align-items:center;
   text-align:left;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--ink);
   box-shadow:none;opacity:.5;transition:.2s}
 .g-perk.can{opacity:1;border-color:color-mix(in srgb,var(--accent) 45%,transparent);cursor:pointer}
 .g-perk.can:hover{transform:translateY(-2px)}
 .g-perk.own{opacity:1;border-color:color-mix(in srgb,var(--green) 50%,transparent);
   background:color-mix(in srgb,var(--green) 10%,var(--panel2));cursor:default}
 .g-pi{grid-area:i;font-size:18px}
 .g-pt{grid-area:t;font-size:13px;font-weight:650}
 .g-pc{grid-area:c;font:600 11.5px var(--mono);color:var(--accent);white-space:nowrap}
 .g-perk.own .g-pc{color:var(--green)}
 .g-pd{grid-area:d;font-size:10.5px;color:var(--faint);line-height:1.32}
 .g-two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 .g-verd{font-style:italic;font-size:17px;color:var(--ink);margin-top:7px;line-height:1.35}
 .g-btn-ghost{background:transparent;border:1px solid color-mix(in srgb,var(--accent) 45%,transparent);
   color:var(--accent);font-size:12px;letter-spacing:.06em;text-transform:uppercase;box-shadow:none;min-width:132px}
 .g-log{background:var(--panel2);border:1px solid var(--line);border-radius:16px;padding:16px 18px}
 .g-lines{display:flex;flex-direction:column;gap:7px;max-height:210px;overflow:auto;font-size:12.5px;line-height:1.4}
 .g-lines div{display:flex;gap:11px;color:var(--ink)}
 .g-lines span{color:var(--faint);flex-shrink:0;font-family:var(--mono);font-size:11.5px}
 .g-modal{position:fixed;inset:0;background:#000a;backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center;z-index:80;padding:20px}
 .g-dial{max-width:470px;width:100%;max-height:88vh;overflow:auto;background:var(--panel);border:1px solid var(--accent);border-radius:18px;
   padding:24px;box-shadow:0 30px 70px #000b;animation:rise .3s both}
 .g-mic{font-size:38px;line-height:1;margin-top:10px}
 .g-mt{font-size:26px;font-weight:750;color:var(--ink);margin-top:7px;line-height:1.15}
 .g-mm{font-size:14px;color:var(--mut);margin-top:9px;line-height:1.5}
 .g-choices{display:flex;flex-direction:column;gap:9px;margin-top:16px}
 .g-choice{width:100%;text-align:left;padding:12px 15px;border-radius:11px;border:1px solid var(--line);
   background:var(--panel2);color:var(--ink);font-size:13.5px;box-shadow:none;transition:.18s}
 .g-choice:hover{border-color:var(--accent);transform:translateY(-1px)}
 .g-hint{display:block;font:11.5px var(--mono);color:var(--faint);margin-top:3px}
 .g-toast{position:fixed;right:20px;bottom:20px;z-index:90;display:flex;align-items:center;gap:12px;
   background:var(--panel);border:1px solid var(--accent);border-radius:14px;padding:13px 17px;
   box-shadow:var(--shadow);opacity:0;transform:translateY(14px);transition:.35s;pointer-events:none;max-width:330px}
 .g-toast.on{opacity:1;transform:translateY(0)}
 @media (max-width:900px){.g-grid{grid-template-columns:1fr}.g-two{grid-template-columns:1fr}.g-h1{font-size:30px}
   .g-hud .g-h{min-width:88px;padding:9px 11px}.g-big{font-size:21px}}
 @media (prefers-reduced-motion:reduce){.g-fl{animation-duration:.01ms}.g-bar i{transition:none}
   .g-cmds button:hover:not(:disabled),.g-bld:hover:not(:disabled),.g-choice:hover,.g-perk.can:hover{transform:none}}
 .guide{max-width:820px}
 .guide-lead{color:var(--mut);font-size:13.5px;margin-bottom:16px;line-height:1.5}
 .guide-sec{margin-bottom:10px}
 .guide-body{font-size:14px;line-height:1.62;color:var(--ink)}
 .guide-body p{margin:0 0 10px} .guide-body p:last-child{margin-bottom:0}
 .guide-body ul{margin:0 0 10px;padding-left:20px} .guide-body li{margin:4px 0}
 .guide-body b{color:var(--ink);font-weight:650}
 .guide-body code{font:12.5px var(--mono);background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:1px 6px}
 .guide-body i{color:var(--mut)}
 .dl-logs{display:inline-block;text-decoration:none;background:var(--accent);color:#2a1206;font-weight:650;font-size:13.5px;padding:9px 16px;border-radius:10px;transition:filter .15s} .dl-logs:hover{filter:brightness(1.06)}
 .upd-banner{position:fixed;left:50%;top:18px;transform:translateX(-50%);z-index:200;background:var(--accent);color:#2a1206;font-weight:650;font-size:14px;padding:11px 20px;border-radius:12px;box-shadow:var(--shadow);animation:rise .3s both}
</style></head><body>
<div class="app">
<aside class="rail">
  <div class="brand"><span class="brand-tile">🐱</span><span class="brand-tx">Кот Альфа<small>БОТ</small></span></div>
  <nav id="tabs"></nav>
  <div class="pet"><div class="pet-bubble" id="petBubble"></div><canvas id="petCat" width="26" height="26"></canvas><span class="pet-say" id="petSay">🐾 Рыжик</span></div>
  <div class="rail-foot">
    <button id="themeBtn" class="b-grey" onclick="toggleTheme()" title="Обычная / максимально чёрная тема">🌑 Чёрная тема</button>
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
  if(window.GAME) GAME.stop();          // уходим со вкладки — глушим таймеры игры
  if(window.OKO) OKO.stop();            // 🔥 глушим анимацию/опрос Ока
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
  if(m.kind==='game'){ main.innerHTML=head+GAME.html(); GAME.start(); return; }
  if(m.kind==='oko'){ main.innerHTML=head+OKO.html(); OKO.start(); return; }
  if(m.kind==='guide'){ main.innerHTML=head+GUIDE_HTML; return; }
  let ctl='';
  if(m.kind==='guard'){
    ctl=`<details class="acc" open>
        <summary><span class="acc-ic">🛡️</span><span class="acc-t">Только защита от бочек</span><span class="chev"></span></summary>
        <div class="acc-body">
          <div class="note">Спокойный режим: бот НЕ фармит (не атакует), но делает все безопасные дела — сторожит и разминирует бочки (Огниво → красный фитиль), собирает казну и реинвестит, держит охрану на холопах, поддерживает ров/частокол. Что именно включено — берётся из галочек в «Набегах» (Авто-казна, Авто-защита холопов, Авто-оборона). <b>Огниво держи в запасе (10–20 шт.)</b> — покупку бот не делает.</div>
          <div class="grid2" style="margin-top:12px">
            ${swHTML('set_defense_only','🛡️ Включить режим «только защита» (не фармить)')}
          </div>
          <div class="btns" style="margin-top:12px">
            <button id="btnStart" class="b-green" onclick="startGuard()">▶ Запустить защиту</button>
            <button id="btnStop" class="b-red" onclick="post('raids','stop')">⏹ Остановить</button></div>
          <div class="note" id="gnote">Тот же бот, что и «Набеги». Включишь «только защиту» — он перестанет бить и будет лишь сторожить бочки.</div>
        </div>
      </details>`;
  } else if(m.kind==='loop'){
    ctl=`<details class="acc" open>
        <summary><span class="acc-ic">🎮</span><span class="acc-t">Управление</span><span class="chev"></span></summary>
        <div class="acc-body">
          <div class="btns"><button id="btnStart" class="b-green" onclick="startRaids()">▶ Запустить</button>
            <button id="btnStop" class="b-red" onclick="post('${mid}','stop')">⏹ Остановить</button></div>
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
          <div class="note" id="dnote">Впиши тех, у кого донат-щит (Купол/Стена) — бот их не тронет. Бот и сам заносит сюда, кого распознал (1 требушет на распознавание). <b>Не путать с обычным щитом:</b> если у цели из списка набегов поднялся обычный игровой щит, бот сам «паркует» её (файл <code>shielded.json</code>) и без единого запроса вернёт в набеги, как только щит спадёт (+1 мин запасом). Это автоматически, галочки не нужно.</div>
        </div>
      </details>
      <details class="acc">
        <summary><span class="acc-ic">⚔️</span><span class="acc-t">Настройки боя</span><span class="acc-hint">10 параметров</span><span class="chev"></span></summary>
        <div class="acc-body">
          <div class="grid2">
            <div><label class="ctl-l">Воевать, пока моё HP выше (иначе — лечиться)</label><input id="set_min_hp" type="number" min="20" max="100"></div>
            <div><label class="ctl-l">Лечиться до HP</label><input id="set_recover" type="number" min="21" max="100"></div>
            <div><label class="ctl-l">Реген: секунд на 1 HP (меньше = быстрее)</label><input id="set_sec_hp" type="number" min="5" max="600"></div>
            <div><label class="ctl-l">Пауза между действиями, сек: от (больше = человечнее, менее палевно)</label><input id="set_delay_lo" type="number" min="0.2" max="20" step="0.1"></div>
            <div><label class="ctl-l">…до</label><input id="set_delay_hi" type="number" min="0.2" max="20" step="0.1"></div>
          </div>
          <div class="grid2" style="margin-top:14px">
            ${swHTML('set_regen_auto','Авто-реген (считать по бонусам с главной)')}
            ${swHTML('set_auto_kazna','🏦 Авто-казна (сбор → депозит → реинвест)')}
            ${swHTML('set_bank_gold','🪙 Класть в казну и золото (иначе — только серебро, золото на оборону)')}
            ${swHTML('set_auto_defense','🛡️ Авто-оборона (ров + частокол активны + запас)')}
            ${swHTML('set_auto_guard','🛡️ Авто-защита холопов (держать охрану — ставит всех, когда истекает; золото с баланса/казны)')}
            ${swHTML('set_pierce','🧱 Пробивать ров/частокол у целей (иначе — пропускать)')}
            ${swHTML('set_hit_shields','🏹 Сносить донат-щит требушетом и фармить (выкл — беречь требушеты)')}
            ${swHTML('set_auto_oboz','🐴 Авто-обоз (+50% серебра с набегов — 400🏅 золота / 50 мин)')}
            ${swHTML('set_bomb_defense','💣 Защита от бочек (разминировать + восстановить после взрыва) — держать ВКЛ')}
            ${swHTML('set_bomb_gold_kazna','🏦 После взрыва брать золото на защиту холопов из казны, если на балансе мало')}
            ${swHTML('set_free_hunt','🎯 Свободная охота — бить слабейших по защите прямо с арены (без списка целей)')}
            ${swHTML('set_war','⚔️ РЕЖИМ ВОЙНЫ — бить по КД без пауз, держать цели прижатыми (палевно)')}
            ${swHTML('set_human','🧑 Человеческий режим — иногда «отходить» на 8–30 мин (дополнение, не замена ночному фарму)')}
            ${swHTML('set_notify','🔔 Слать мне в Избранное о важном (бочка/лечение) — по желанию, деф выкл')}
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
      const idAttr=a.id==='run'?' id="btnStart"':'';   // главная кнопка «запуска» — реактивная
      return `<button${idAttr} class="${cls}" onclick="runOne('${mid}','${a.id}')">${a.label}</button>`;
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
            <button id="btnStop" class="b-red" onclick="post('${mid}','stop')">⏹ Стоп</button></div>
          <div class="note" id="note"></div>
        </div>
      </details>${resultBox}`;
  }
  main.innerHTML=head+`<div class="workspace"><div class="controls">${ctl}</div>
    <section class="console"><div class="console-bar"><span class="live">LIVE</span>
      <span class="console-name">Журнал — ${m.title}</span>
      <span class="console-meta">новые снизу</span></div>
      <pre class="log" id="log">…</pre></section></div>`;
  if(m.kind==='loop'){ loadTargets(); loadDonate(); loadSettings(); }
  if(m.kind==='guard'){ loadGuard(); }
  pollId=(m.kind==='guard')?'raids':active;   // вкладка защиты сторожит тот же смашер «набеги»
  pollMod(); timer=setInterval(pollMod,1500);
}
let nightOn=false;
let pollId='';
async function startRaids(){ try{ await fetch('/api/raids/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({defense_only:false})}); }catch(e){} post('raids','start'); }
async function saveGuard(){ const el=$('#set_defense_only'); try{ await fetch('/api/raids/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({defense_only:!!(el&&el.checked),bomb_defense:true})}); const n=$('#gnote'); if(n) n.textContent='Сохранено ✓ — меняется на лету.'; }catch(e){} }
async function startGuard(){ const el=$('#set_defense_only'); if(el) el.checked=true; await saveGuard(); post('raids','start'); }
async function loadGuard(){ try{ const d=await (await fetch('/api/raids/settings')).json(); const el=$('#set_defense_only'); if(el){ el.checked=!!d.defense_only; el.onchange=saveGuard; } }catch(e){} }
async function pollMod(){
  try{ const r=await fetch('/api/'+(pollId||active)+'/status'); const d=await r.json();
    const mp=$('#mpill'); if(mp) mp.innerHTML=pill(d.state); petState=d.state||'run';
    // кнопки реагируют на состояние: активная — цветом, неактивная — серым
    const on=(d.state==='run'||d.state==='pause');
    const bS=$('#btnStart'); if(bS) bS.className=on?'b-grey':'b-green';
    const bT=$('#btnStop');  if(bT) bT.className=on?'b-red':'b-grey';
    if(d.log){ var _l=(d.log.trim().split('\n').pop()||''); if(_l!==petLastLine){ petLastLine=_l; var _e=classifyLog(_l); if(_e) petEvent=_e; } }
    const nb=$('#nightBtn'); if(nb){ nightOn=!!d.night;
      nb.className='b-night'+(nightOn?' on':'');
      nb.textContent=nightOn?'🌙 Ночной режим: ВКЛ':'🌙 Ночной режим'; }
    const log=$('#log'); if(log){
      // обычный порядок: старые сверху, новые снизу. Держим скролл внизу,
      // если пользователь сам не увёл его вверх читать историю.
      const atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<40;
      const txt=(d.log||'(пусто)').replace(/\n+$/,'');
      if(log.textContent!==txt){ log.textContent=txt; if(atBottom) log.scrollTop=log.scrollHeight; }
    }
  }catch(e){}
  loadResults();
}
async function loadResults(){
  const t=$('#results'); if(!t) return;
  // не перетираем поле, пока в нём выделяют/печатают — иначе слетает выделение
  if(document.activeElement===t) return;
  try{ const txt=await (await fetch('/api/'+(pollId||active)+'/results')).text();
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
    const bd=$('#set_bomb_defense'); if(bd) bd.checked=(d.bomb_defense!==false);
    const bgk=$('#set_bomb_gold_kazna'); if(bgk) bgk.checked=(d.bomb_gold_kazna!==false);
    const ag=$('#set_auto_guard'); if(ag) ag.checked=!!d.auto_guard;
    const fh=$('#set_free_hunt'); if(fh) fh.checked=!!d.free_hunt;
    const wm=$('#set_war'); if(wm) wm.checked=!!d.war_mode;
    const hm=$('#set_human'); if(hm) hm.checked=!!d.human_mode;
    const nt=$('#set_notify'); if(nt) nt.checked=!!d.notify_dm;
    if(c) c.disabled=!!(ra&&ra.checked);
    const dl=$('#set_delay_lo'); if(dl && document.activeElement!==dl) dl.value=d.req_delay_lo;
    const dh=$('#set_delay_hi'); if(dh && document.activeElement!==dh) dh.value=d.req_delay_hi;
  }catch(e){}
}
async function saveSettings(){
  const a=$('#set_min_hp'), b=$('#set_recover'); if(!a||!b) return;
  const body={my_min_hp:parseInt(a.value||'25',10), my_recover_to:parseInt(b.value||'50',10),
    sec_per_hp:parseInt(($('#set_sec_hp')||{}).value||'60',10),
    req_delay_lo:parseFloat(($('#set_delay_lo')||{}).value||'1.5'),
    req_delay_hi:parseFloat(($('#set_delay_hi')||{}).value||'3.5'),
    regen_auto:!!($('#set_regen_auto')||{}).checked,
    auto_kazna:!!($('#set_auto_kazna')||{}).checked,
    auto_defense:!!($('#set_auto_defense')||{}).checked,
    pierce_defenses:!!($('#set_pierce')||{}).checked,
    hit_shields:!!($('#set_hit_shields')||{}).checked,
    bank_gold:!!($('#set_bank_gold')||{}).checked,
    auto_oboz:!!($('#set_auto_oboz')||{}).checked,
    bomb_defense:!!($('#set_bomb_defense')||{}).checked,
    bomb_gold_kazna:!!($('#set_bomb_gold_kazna')||{}).checked,
    auto_guard:!!($('#set_auto_guard')||{}).checked,
    free_hunt:!!($('#set_free_hunt')||{}).checked,
    war_mode:!!($('#set_war')||{}).checked,
    human_mode:!!($('#set_human')||{}).checked,
    notify_dm:!!($('#set_notify')||{}).checked};
  try{ const r=await fetch('/api/raids/settings',{method:'POST',body:JSON.stringify(body)});
    const d=await r.json(); const n=$('#snote');
    if(n){ n.textContent=d.ok?'✅ настройки сохранены — применятся в ближайший цикл':'ошибка сохранения';
      setTimeout(()=>{n.textContent='Меняется на лету — бот подхватит в ближайший цикл.';},6000);}
    loadSettings();
  }catch(e){}
}
/* ─────────── 📖 ГАЙД (при изменениях в пульте — обновлять и его!) ─────────── */
const GUIDE_SECTIONS=[
 ['🚀','С чего начать',`
   <p><b>Пульт</b> — это панель управления твоим ботом для игры @holop. Всё работает <b>локально на твоём компьютере</b>, вход в Telegram никуда не уходит.</p>
   <p><b>Запуск:</b> двойной клик по <code>START.bat</code> (Windows) или <code>START-mac.command</code> (Mac). Откроется вкладка в браузере — это и есть пульт.</p>
   <p><b>Обновления прилетают сами.</b> Панель сама заметит новую версию и перезагрузится — ручной Cmd+Shift+R больше не нужен. Если новых функций всё же нет — полностью закрой окно пульта и запусти заново. Версия видна внизу слева.</p>
   <p><b>Тема:</b> переключатель «🌑 Чёрная тема» внизу слева — обычная или максимально чёрная. В чёрной кот надевает маску ниндзя 🥷.</p>
   <p><b>Вкладки слева</b> — это разделы. Кот 🐾 внизу живой: реагирует на то, что делает бот. Тыкни его; двойной клик — пробежится.</p>`],
 ['🟢','Кнопки запуска',`
   <p>У каждого бота две кнопки: <b>Запустить</b> и <b>Остановить</b>. Они подсказывают состояние цветом:</p>
   <ul><li><b>Бот стоит:</b> «Запустить» горит <span style="color:var(--green)">зелёным</span> (жми её), «Остановить» серая.</li>
   <li><b>Бот работает:</b> «Остановить» горит <span style="color:var(--red)">красным</span>, «Запустить» серая.</li></ul>
   <p>Пилюля справа вверху («🟢 Работает / ⚪ Остановлен») дублирует статус.</p>
   <p><b>🛑 Стоп-кран</b> (внизу слева) — гасит ВСЕ боты разом. <b>👤 Сменить аккаунт</b> — выйти и войти заново (если Telegram отозвал сессию); списки и настройки при этом остаются.</p>`],
 ['⚔️','Набеги — авто-бой',`
   <p>Бот сам бьёт по твоему списку целей и защищает замок от бочки. Всё правится на лету — бот подхватит в ближайший цикл.</p>
   <p><b>🎯 Цели</b> — список ников (по одному в строке). Кого бот бьёт. Пусто — бот ждёт, никого не трогает.</p>
   <p><b>🛡️ Щитники — не бить</b> — ники, у кого донат-щит (Купол/Стена). Бот их пропустит и не потратит требушет. Бот и сам сюда дописывает, кого распознал в бою.</p>
   <p><b>🌙 Ночной режим</b> — держит Mac бодрым и сам перезапускает бота, если тот упал/завис. Для фарма на ночь.</p>`],
 ['🔥','Око Саурона',`
   <p><b>Живая доска целей в стиле Мордора.</b> Впиши ников (по одному в строке) и жми <b>🔥 Разжечь Око</b> — покажет карточки: HP, щит, кулдаун (тикают в реальном времени), уровень и защиту. Данные берутся <b>из локальных файлов кота — ни одного запроса к игре</b>, так что абсолютно не палевно.</p>
   <p><b>🔥 Пнуть</b> — ударить по цели вне очереди. Если идут <b>Набеги</b> — цель бьётся <b>приоритетно</b> (первой, подхват между целями ~30 сек). Если бот в режиме <b>«только защита»</b> — <b>немедленный вылет</b> по ней. Работает, только пока кот запущен (Набеги или Защита) — иначе бить некому.</p>
   <p><b>Карточка с огненной рамкой «⚔ ЦЕЛЬ ОТКРЫТА»</b> — щит спал и КД прошёл, можно бить прямо сейчас.</p>
   <p><b>👁 Режим Саурона</b> — пасхалка: кот «перевоплощается», в журнале появляются зловещие реплики («ПОКОРЁН», «повержен предо мной»), пульт тлеет огнём. На фарм не влияет — чистый антураж. Выключается тем же тумблером.</p>`],
 ['🎛️','Набеги — Настройки боя',`
   <p><b>Воевать, пока HP выше</b> — если моё HP упало ниже, бот уходит лечиться. <b>Лечиться до HP</b> — до какого значения восстанавливаться.</p>
   <p><b>Реген: секунд на 1 HP</b> — скорость восстановления. Можно вписать вручную. Или включить <b>Авто-реген</b> — тогда бот сам считает по бонусам с Территории И <b>уточняет по факту</b> (замеряет реальное восстановление во время лечения). При левелапе пересчитает сам.</p>
   <p><b>Пауза между действиями (от/до)</b> — сколько секунд бот выжидает между запросами к игре. Больше = человечнее и менее палевно (но медленнее). По умолчанию 1.5–3.5с. Хочешь совсем «как человек» — ставь 3–7. На лечении бот теперь спит почти до цели и перечитывает HP 1–2 раза, а не пять (меньше запросов).</p>
   <p><b>🏦 Авто-казна</b> — собирает доход и кладёт в депозит (реинвест). <b>🪙 Класть в казну и золото</b> — по умолчанию в казну идёт только серебро, золото остаётся свободным на оборону; включи, если хочешь копить и золото.</p>
   <p><b>🛡️ Авто-оборона</b> — держит ров и частокол активными + запас. Ров/частокол — расходники (блок 1 и 3 набега), бот перепроверяет их чаще и сразу после атаки на тебя.</p>
   <p><b>🛡️ Авто-защита холопов</b> — проактивно держит охрану на холопах: раз в ~час заходит, и если у кого-то охрана слетела/истекла — ставит всех на защиту. Золото берёт с баланса; не хватает — 10% из казны (та же галочка «брать золото из казны»). Это НЕ то же, что защита после взрыва — тут бот держит охрану постоянно, чтобы холопов не увели.</p>
   <p><b>Два режима бота.</b> Все безопасные (неатакующие) действия — казна, охрана холопов, оборона, защита от бочек — работают в ОБОИХ режимах: и в «Набегах» (боевой), и во вкладке «🛡️ Защита от бочек» (спокойный, без атак). Разница только в том, что боевой ещё и бьёт по целям. Так что можно оставить бота в спокойном режиме на ночь — он не фармит, но казну собирает, охрану держит и бочки гасит.</p>
   <p><b>🧱 Пробивать ров/частокол</b> — бить сквозь них (иначе пропускать защищённых). <b>🏹 Сносить донат-щит требушетом</b> — фармить щитников за требушеты (выкл — беречь требушеты).</p>
   <p><b>🐴 Авто-обоз</b> — покупает обоз «+50% серебра с набегов на 50 мин» за золото (собирает с холопов / из казны). Срок хранит в файле, лишний раз в магазин не лезет.</p>
   <p><b>💣 Защита от бочек</b> — пока бот работает и галочка включена, он сам следит за бочками (динамитом). Прилетела бочка → жмёт <b>Огниво</b>, потом <b>красный фитиль</b>. Угадал (шанс 1/3) — территория цела. Не угадал и рвануло — бот сам <b>лечит территорию</b> (100 000🪙 из казны) и <b>ставит охрану всем холопам</b> (10% золота из казны), лишнее возвращает в депозит. <b>Огниво держи в запасе (10–20 шт.)</b> — бот его не покупает, ⭐-варианты (Мастер, 25⭐) не трогает. Отдельная вкладка <b>«🛡️ Защита от бочек»</b> — режим «только сторожу, не фармлю»: включаешь и бот просто караулит бочки, ничего не атакуя.</p>
   <p><b>🏦 Брать золото на защиту из казны</b> — на защиту холопов после взрыва нужно золото. Если держишь золото на балансе (на кармане) — оставь выкл, бот возьмёт оттуда. Если сгружаешь золото в казну (депозит) — включи, и бот сам снимет недостающее из казны. По умолчанию включено.</p>
   <p><b>🔔 Слать мне в Избранное</b> — важные события (бочка/лечение) прилетают тебе личным сообщением в «Избранное» Telegram. Узнаёшь сразу, даже не открывая пульт. <b>По умолчанию выключено</b> — включи галочкой, если хочешь уведомления.</p>
   <p><b>🧑 Человеческий режим</b> — бот иногда «отходит» на 8–30 минут, чтобы активность не была машинно-ровной сутками (менее палевно). Это ДОПОЛНЕНИЕ, не замена — фармить продолжает, в том числе ночью. По умолчанию выключен.</p>
   <p><b>⚔️ РЕЖИМ ВОЙНЫ</b> — бьёт по КД почти без пауз, держит цели прижатыми. <b>Палевно</b> (много запросов к игре) — включать под конкретный замес, не сутками.</p>`],
 ['🎭','Роли холопов',`
   <p>Перегоняет холопа в нужную профессию: выгоняет → тут же захватывает обратно → читает профессию → повторяет, пока не выпадет нужная. Бот успевает в 30-сек окно эксклюзива, поэтому холопа не уводят (руками — уводят).</p>
   <p><b>Ники</b> — по одному в строке. <b>Профессия</b> — целевая (по умолчанию Воин). <b>Авто-разжаб</b> — если холоп под охраной, бот снимет её зельем жаб ИЗ ЗАПАСА (звёзды не тратит) и заберёт. Без этого на защищённом холопе бот просто остановится.</p>
   <p>Кнопки: <b>Перегнать</b> (боевой), <b>Проверить</b> (только показать профессию), <b>Холостой</b> (dry-run, ничего не жмёт).</p>
   <p>⚠️ Зелье жаб бери из запаса заранее — на старт каждого защищённого холопа уходит примерно одно.</p>`],
 ['🔎','Найти цели',`
   <p>Сканирует богатых бьющихся соперников (сортировка по серебру) и выдаёт список ников. Справа в поле «Найденные ники» — кнопки <b>Копировать</b> и <b>В Набеги</b> (добавит в список набегов без дублей).</p>
   <p><b>Сколько целей / страниц</b> — глубина поиска. <b>Макс. защита</b> — 0 = любая; напр. 500 для боя за 1 HP. <b>Пропускать цели с ров/частокол/защитой</b> — Да/Нет.</p>`],
 ['📊','Разведка (КД/щиты)',`
   <p>Вставь ников — покажет, когда у каждого спадёт щит/КД и восстановится HP (время по МСК). Можно поставить напоминалки «за N минут: готовься к атаке на X».</p>`],
 ['🕳️','Пещеры и ⏰ Будильники',`
   <p><b>Пещеры</b> — автопроход трёх пещер по очереди до стоп-уровня. Настройки: мин. шанс победы, мин. HP, стоп-уровень.</p>
   <p><b>Будильники</b> — отложенные мигалки в чат к моменту спадения охраны холопов (за N минут до).</p>`],
 ['🎮','Барская игра',`
   <p>Мини-игра для отдыха: содержи холопа-кота, богатей, не доведи до бунта. Команды, события с выбором, сюжетные цепочки, куча финалов. <b>Ачивки копятся между забегами</b> (Салтычиха, Мироед, Вольная…). Рекорд и число забегов сохраняются. Чистая забава, на бота не влияет.</p>`],
 ['🔒','Про безопасность',`
   <p>Твой вход хранится только на этом компьютере (файл <code>config.json</code>) и никуда не отправляется. Это игровой самобот — против правил Telegram, используешь на свой риск.</p>
   <p>Бот работает только у участников закрытой группы тестеров — защита от того, чтобы архив не расползся по чужим людям.</p>
   <p><b>Если код входа не приходит:</b> Telegram шлёт его не по SMS, а сообщением в сам Telegram — ищи чат «Telegram» (служебный, вверху списка). Или жми «Код не пришёл — прислать по SMS».</p>`],
 ['🛠️','Если что-то не так',`
   <ul>
   <li><b>Нажимаю Запустить — ничего:</b> закрой окно пульта полностью и запусти заново.</li>
   <li><b>Новых функций нет после обновы:</b> та же причина — панель держала старую версию в памяти. Полный перезапуск лечит.</li>
   <li><b>Журнал завалило «Server closed the connection»:</b> рвётся связь (интернет/VPN). Бот сам переподключается; зафиксируй VPN на одной стране.</li>
   <li><b>«Ключ отозван» / вылет сессии:</b> VPN сменил страну на ходу. Зафиксируй одну страну, жми «Сменить аккаунт».</li>
   <li>Не помогло — нажми кнопку ниже и пришли скачанный файл в чат. В нём все логи разом (пароля и ключа аккаунта там нет).</li></ul>
   <p><a class="dl-logs" href="/api/logs" download="kot-alfa-logs.txt">📥 Скачать логи для поддержки</a></p>
   <p><i>Журнал идёт обычным порядком: новые строки снизу.</i></p>`],
 ['👥','Несколько аккаунтов (мультиоконность)',`
   <p>Хочешь качать <b>два аккаунта сразу</b>? Сделай так:</p>
   <ul>
   <li><b>Скопируй всю папку пульта</b> рядом (напр. «Пульт-2»). Заходи во второй аккаунт из неё — свой вход, свои цели и настройки.</li>
   <li>В папке второго аккаунта создай файл <code>port.txt</code> и впиши туда другое число — например <b>8778</b> (у первого остаётся 8777).</li>
   <li>Запускай обе папки как обычно (двойной клик). Первая откроется на <code>127.0.0.1:8777</code>, вторая — на <code>127.0.0.1:8778</code>.</li></ul>
   <p>Панели и боты аккаунтов <b>больше не мешают друг другу</b> — запуск/стоп/обнова одного не трогают второй (раньше вторая панель крашилась — починено).</p>`],
];
const GUIDE_HTML=`<div class="guide">
  <div class="guide-lead">Пульт живёт и обновляется — этот гайд обновляется вместе с ним. Разверни любой раздел.</div>
  ${GUIDE_SECTIONS.map((s,i)=>`<details class="acc guide-sec"${i===0?' open':''}>
    <summary><span class="acc-ic">${s[0]}</span><span class="acc-t">${s[1]}</span><span class="chev"></span></summary>
    <div class="acc-body guide-body">${s[2]}</div>
  </details>`).join('')}
</div>`;

function applyTheme(){
  const black=localStorage.getItem('holop_theme')==='black';
  if(black) document.documentElement.setAttribute('data-theme','black');
  else document.documentElement.removeAttribute('data-theme');   // стандартная (авто свет/тьма)
  const b=document.getElementById('themeBtn');
  if(b) b.textContent=black?'🎨 Обычная тема':'🌑 Чёрная тема';
}
function toggleTheme(){
  const black=localStorage.getItem('holop_theme')==='black';
  localStorage.setItem('holop_theme', black?'standard':'black');
  applyTheme();
}
applyTheme();   // применяем сразу, до отрисовки

// 🔄 авто-перезагрузка при обнове: если версия на сервере сменилась — сама подхватит
// свежую страницу (больше не нужен ручной Cmd+Shift+R).
const PAGE_VERSION="__VERSION__";
async function checkVersion(){
  try{
    const d=await (await fetch('/api/version',{cache:'no-store'})).json();
    if(d.version && d.version!==PAGE_VERSION){
      const b=document.createElement('div'); b.className='upd-banner';
      b.textContent='🔄 Пришло обновление ('+d.version+') — обновляю панель…';
      document.body.appendChild(b);
      setTimeout(()=>location.reload(true), 1200);
    }
  }catch(e){}
}
setInterval(checkVersion, 12000);

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
  function cssv(n,f){ return (getComputedStyle(document.documentElement).getPropertyValue(n)||'').trim()||f; }
  function isBlack(){ return document.documentElement.getAttribute('data-theme')==='black'; }
  var acc=cssv('--cat', cssv('--accent','#e6873a'));
  function drk(hex,f){ var n=parseInt(hex.replace('#',''),16); return 'rgb('+Math.round(((n>>16)&255)*f)+','+Math.round(((n>>8)&255)*f)+','+Math.round((n&255)*f)+')'; }
  var P={K:'#241a1e',G:acc,D:drk(acc,.72),C:'#f3e7cf',E:'#3f9fd6',W:'#f6fbff',N:'#cf7280',I:'#e3a6ac',
    M:'#c2c7d4',Md:'#767c8f',WD:'#8a5a30',Gd:'#e8c14e',R:'#e0503a',Pu:'#9b7be6',FL:'#ffce4d',FLo:'#ff8a3a',
    WH:'#f3f0fb',GL:'rgba(120,190,240,.9)',GR:'#4bd08a',HRT:'#ff6b81',NB:'#d23c4e',MK:'#141419'};
  function refreshCat(){ var c=cssv('--cat',cssv('--accent','#e6873a')); P.G=c; P.D=drk(c,.72); }
  // 🥷 повязка ниндзя + маска — только в чёрной теме
  function ninja(yo,dx){ var x=XO+dx;
    rct(x+2,4+yo,15,2,P.MK);                       // тёмная основа повязки
    rct(x+2,4+yo,15,1,P.NB);                        // красная полоса
    px(x+16,3+yo,P.NB);px(x+17,3+yo,P.MK);          // узел
    px(x+18,4+yo,P.NB);px(x+19,5+yo,P.MK);px(x+18,6+yo,P.NB);  // хвосты
    rct(x+3,10+yo,12,3,P.MK);                       // маска на морду (ниже глаз)
  }
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
    refreshCat();                                   // цвет шерсти по теме (на лету)
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
    if(isBlack()) ninja(yo,dx);                     // 🥷 в чёрной теме — кот-ниндзя
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
    var w=window.innerWidth, rt=0;
    var dir=Math.random()<0.5?1:-1;               // 1=вправо, -1=влево (Максим: чтоб не только направо)
    var x=dir===1?-110:(w+40);
    (function step(){
      x+=6*dir; runCv.style.left=x+'px';
      if(dir===-1){ runCtx.save(); runCtx.translate(runCv.width,0); runCtx.scale(-1,1); }  // зеркалим спрайт
      drawScene(runCtx, runCv.width, {id:'',working:true,sleeping:false,event:null,t:rt,jump:0,roam:true});
      if(dir===-1){ runCtx.restore(); }
      rt++;
      var done=dir===1?(x>=w+40):(x<=-110);
      if(!done) requestAnimationFrame(step);
      else { roaming=false; runCv.style.display='none'; }
    })();
  }
  cv.addEventListener('dblclick', roam);
  setInterval(function(){ if(!roaming && document.visibilityState!=='hidden' && Math.random()<0.3) roam(); }, 42000);
})();

/* ══════════════════════════════════════════════════════════════════════════
   🎮 БАРСКАЯ ИГРА «ПУЛЬТ ХОЛОПА» — тамагочи-сатира про барина и холопа.
   Цель: дожить до дня 20 или набить 1000 🪙, не доведя холопа до бунта.
   Ачивки копятся в браузере (localStorage) между забегами.
   ══════════════════════════════════════════════════════════════════════════ */
/* ══════════════════════════════════════════════════════════════════════════
   🔥 ОКО САУРОНА — живая доска целей в стиле Мордора. Данные локальные (0 запросов).
   ══════════════════════════════════════════════════════════════════════════ */
const OKO = (function(){
  let eyeTimer=null, pollTimer=null, tickTimer=null, cards=[], active=false, sauron=false, T=0;
  function esc(s){return (s==null?'':(''+s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function hash(s){let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return h>>>0;}
  function fmt(sec){sec=Math.max(0,Math.floor(sec));
    if(sec>=3600){const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60);return h+'ч '+(''+m).padStart(2,'0')+'м';}
    const m=Math.floor(sec/60),s=sec%60;return m+':'+(''+s).padStart(2,'0');}

  /* ── пиксельное Око (ImageData 64×80, масштаб пикселями) ── */
  function drawEye(cv){
    const W=64,H=80,ctx=cv.getContext('2d');
    let img=ctx.createImageData(W,H),d=img.data;
    const cx=31.5,cy=40, t=T;
    for(let y=0;y<H;y++)for(let x=0;x<W;x++){
      const i=(y*W+x)*4;
      const dx=x-cx, dy=y-cy, nx=dx/23, ny=dy/35, rr=nx*nx+ny*ny;
      // мерцающий шум пламени
      const n=(Math.sin(x*12.9+y*4.7+t*3.1)*0.5+0.5)*(Math.sin(y*7.3-t*2.3+x*0.7)*0.5+0.5);
      let r=0,g=0,b=0,a=0;
      if(rr<1.0){                        // тело глаза — янтарная склера
        const glow=1-rr;                 // ярче к центру
        r=255; g=Math.round(120+110*glow); b=Math.round(20+40*glow); a=255;
        // вертикальный зрачок-щель
        const px=Math.abs(nx)/0.16, py=Math.abs(ny)/0.66;
        if(px*px+py*py<1){
          const core=1-(px*px+py*py);
          const pulse=0.5+0.5*Math.sin(t*4);
          r=Math.round(30+225*core*pulse); g=Math.round(10+90*core*pulse); b=0; a=255;
          if(core<0.28){r=8;g=4;b=2;}    // чёрная кромка щели
        }
      } else if(rr<2.4){                  // огненный венец — языки пламени с мерцанием
        const edge=(2.4-rr)/1.4;
        const flame=edge*0.7+n*0.7;
        if(flame>0.55){
          a=Math.round(Math.min(255,(flame-0.5)*430));
          r=255; g=Math.round(70+150*n); b=Math.round(15*n);
        }
      }
      d[i]=r;d[i+1]=g;d[i+2]=b;d[i+3]=a;
    }
    ctx.putImageData(img,0,0);
  }

  /* ── пиксельный орк-аватар на карточке (статичный, seed из ника) ── */
  function drawOrc(cv,name){
    const ctx=cv.getContext('2d'),h=hash(name);
    ctx.clearRect(0,0,14,15);
    const skin=['#4b6b2e','#5c7a34','#3f5f2b','#6b7d2f'][h%4];
    const dark='#2c421d', tusk='#e8e0c8', eye=['#ff3b1f','#ffd23b','#ff6a1f'][(h>>3)%3];
    function px(x,y,c){ctx.fillStyle=c;ctx.fillRect(x,y,1,1);}
    // голова (симметрично)
    for(let y=2;y<=12;y++)for(let x=3;x<=10;x++){ if((x===3||x===10)&&(y<4||y>10))continue; px(x,y,skin);}
    px(2,5,skin);px(2,6,skin);px(11,5,skin);px(11,6,skin);       // уши
    for(let x=3;x<=10;x++)px(x,3,dark);                          // бровь
    const ey=5+((h>>5)%2);
    px(4,ey,eye);px(5,ey,eye);px(8,ey,eye);px(9,ey,eye);         // глаза
    for(let x=5;x<=8;x++)px(x,10,dark);                          // рот
    px(5,11,tusk);px(8,11,tusk);                                 // клыки
  }

  function timerHTML(c,now){
    if(c.shield_until>now) return '🛡 щит '+fmt(c.shield_until-now);
    if(c.cd_until>now)     return '⌛ КД '+fmt(c.cd_until-now);
    return '<b class="open-lbl">⚔ ЦЕЛЬ ОТКРЫТА</b>';
  }
  function cardHTML(c,i,now){
    const hp=(c.hp==null)?null:Math.max(0,Math.min(100,c.hp));
    const opened=(c.cd_until<=now&&c.shield_until<=now);
    return `<div class="oko-card${opened?' open':''}" id="okoc${i}">
      <canvas class="oko-orc" width="14" height="15" data-n="${esc(c.name)}"></canvas>
      <div class="oko-body">
        <div class="oko-nick">${esc(c.name)}</div>
        <div class="oko-hp"><i style="width:${hp==null?0:hp}%"></i><span>${hp==null?'HP ?':hp+' HP'}</span></div>
        <div class="oko-meta">${c.level?('ур.'+c.level):'ур.?'} · защ.${c.defense!=null?c.defense:'?'}${(c.state&&/побед|снят|недоступ/i.test(c.state))?(' · '+esc(c.state)):''}</div>
        <div class="oko-timer" id="okot${i}">${timerHTML(c,now)}</div>
      </div>
      <button class="oko-hit" onclick="OKO.hit(${i},this)">🔥 ПНУТЬ</button>
    </div>`;
  }

  function render(){
    const wrap=document.getElementById('okoGrid'); if(!wrap)return;
    const now=Date.now()/1000;
    if(!cards.length){ wrap.innerHTML='<div class="oko-empty">Око пусто. Впиши имена жертв и разожги Око.</div>'; return; }
    wrap.innerHTML=cards.map((c,i)=>cardHTML(c,i,now)).join('');
    wrap.querySelectorAll('.oko-orc').forEach(cv=>drawOrc(cv,cv.dataset.n));
  }
  function tick(){
    const now=Date.now()/1000;
    cards.forEach((c,i)=>{ const el=document.getElementById('okot'+i); if(el)el.innerHTML=timerHTML(c,now);
      const cd=document.getElementById('okoc'+i); if(cd)cd.classList.toggle('open',c.cd_until<=now&&c.shield_until<=now); });
  }
  async function fetchCards(){
    try{ const r=await fetch('/api/oko/cards'); const d=await r.json();
      cards=d.cards||[]; setMode(d);
      render();
    }catch(e){}
  }
  function setMode(d){
    const s=document.getElementById('okoMode'); if(!s)return;
    if(!d.running) s.innerHTML='😴 Кот спит — включи «Набеги» или «Защиту», иначе «пнуть» не сработает.';
    else if(d.defense_only) s.innerHTML='🛡 Спокойный режим: «пнуть» = <b>немедленный вылет</b> по цели.';
    else s.innerHTML='⚔️ Набеги идут: «пнуть» = <b>приоритетный удар</b> (первым в проходе).';
    if(typeof d.sauron==='boolean'){ sauron=d.sauron; const t=document.getElementById('okoSw'); if(t)t.checked=sauron; applySauron(); }
  }
  function applySauron(){
    const w=document.getElementById('okoWrap'); if(w)w.classList.toggle('sauron',sauron);
    document.body.classList.toggle('sauron-on',sauron);
  }

  return {
    html(){ return `
      <style>
      .oko-wrap{--ember:#ff5a1f;--ember2:#ff8a2b;--blood:#7a0d0d;--sulf:#ffd36b;--void:#0a0705;--ash:#2a2320;
        position:relative;border-radius:16px;overflow:hidden;padding:22px 18px 26px;
        background:radial-gradient(120% 90% at 50% -8%,#1c0f08 0%,#100a07 45%,#070403 100%);
        color:#e9d8c4;font-family:var(--mono);
        border:1px solid rgba(255,90,31,.18);box-shadow:inset 0 0 90px rgba(255,60,10,.10),0 24px 60px -20px #000;}
      .oko-head{display:flex;flex-direction:column;align-items:center;gap:8px;text-align:center;position:relative;z-index:2}
      .oko-eye{width:150px;height:188px;image-rendering:pixelated;filter:drop-shadow(0 0 26px rgba(255,80,20,.75)) drop-shadow(0 0 60px rgba(255,40,0,.35))}
      .oko-beta{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
        color:#ffd36b;-webkit-text-fill-color:#ffd36b;background:rgba(255,90,20,.16);border:1px solid rgba(255,150,60,.5);
        border-radius:6px;padding:2px 7px;margin-left:12px;vertical-align:middle;top:-6px;position:relative}
      .oko-title{font-family:var(--font);font-weight:800;letter-spacing:.32em;font-size:26px;
        background:linear-gradient(180deg,#ffe08a,#ff7a1f 55%,#b31313);-webkit-background-clip:text;background-clip:text;color:transparent;
        text-transform:uppercase;margin:2px 0 0}
      .oko-sub{color:#b98b63;font-size:12px;letter-spacing:.14em;text-transform:uppercase;max-width:520px;line-height:1.5}
      .oko-controls{position:relative;z-index:2;margin:20px auto 0;max-width:640px}
      .oko-ta{width:100%;box-sizing:border-box;background:#0d0806;color:#f0dcc2;border:1px solid rgba(255,120,40,.22);
        border-radius:10px;padding:11px 12px;font-family:var(--mono);font-size:14px;min-height:82px;resize:vertical}
      .oko-ta:focus{outline:none;border-color:var(--ember);box-shadow:0 0 0 3px rgba(255,90,20,.18)}
      .oko-btns{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:11px}
      .oko-ignite{flex:1;min-width:200px;cursor:pointer;border:none;border-radius:10px;padding:13px 16px;font-weight:800;
        font-family:var(--font);letter-spacing:.14em;text-transform:uppercase;color:#1a0a02;font-size:14px;
        background:linear-gradient(180deg,#ffd35a,#ff7a1f 60%,#d63a09);box-shadow:0 0 24px rgba(255,90,20,.5),inset 0 1px 0 rgba(255,255,255,.4)}
      .oko-ignite:active{transform:translateY(1px)}
      .oko-swrap{display:flex;align-items:center;gap:9px;color:#e0a978;font-size:12px;letter-spacing:.08em;
        text-transform:uppercase;user-select:none;cursor:pointer;padding:9px 12px;border:1px solid rgba(255,120,40,.22);border-radius:10px;background:#0d0806}
      .oko-swrap input{position:absolute;opacity:0;pointer-events:none}
      .oko-dot{width:38px;height:20px;border-radius:20px;background:#3a2a20;position:relative;transition:.2s;flex:none}
      .oko-dot::after{content:'';position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#7a6a5a;transition:.2s}
      .oko-swrap input:checked+.oko-dot{background:linear-gradient(90deg,#ff7a1f,#d63a09)}
      .oko-swrap input:checked+.oko-dot::after{left:20px;background:#ffe08a;box-shadow:0 0 10px #ff7a1f}
      .oko-mode{margin-top:10px;color:#c79a72;font-size:12px;text-align:center;min-height:16px}
      .oko-grid{position:relative;z-index:2;margin-top:20px;display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(230px,1fr))}
      .oko-empty{grid-column:1/-1;text-align:center;color:#7d5f48;padding:26px;font-size:13px}
      .oko-card{position:relative;display:grid;grid-template-columns:34px 1fr;grid-template-rows:auto auto;gap:3px 11px;
        background:linear-gradient(160deg,#1a0f09,#120b07);border:1px solid rgba(255,110,40,.16);border-radius:12px;padding:12px 12px 52px;
        transition:.18s;overflow:hidden}
      .oko-card::before{content:'';position:absolute;inset:0;background:radial-gradient(80% 60% at 50% 120%,rgba(255,70,10,.10),transparent);pointer-events:none}
      .oko-card.open{border-color:rgba(255,110,30,.6);box-shadow:0 0 0 1px rgba(255,110,30,.35),0 0 26px -6px rgba(255,90,20,.55)}
      .oko-orc{grid-row:1/3;width:34px;height:37px;image-rendering:pixelated;align-self:start;filter:drop-shadow(0 1px 2px #000)}
      .oko-nick{font-weight:800;color:#ffdca8;font-family:var(--font);font-size:15px;letter-spacing:.02em;word-break:break-word}
      .oko-hp{position:relative;height:13px;border-radius:7px;background:#2a0d0d;overflow:hidden;margin:4px 0 2px;border:1px solid rgba(255,80,40,.2)}
      .oko-hp i{position:absolute;inset:0 auto 0 0;background:linear-gradient(90deg,#8a1010,#ff3b2a);border-radius:7px}
      .oko-hp span{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;text-shadow:0 1px 2px #000;letter-spacing:.04em}
      .oko-meta{grid-column:2;color:#a9805f;font-size:11px;letter-spacing:.02em}
      .oko-timer{grid-column:1/3;margin-top:7px;color:#d7a679;font-size:12.5px;letter-spacing:.03em}
      .oko-timer .open-lbl{color:#ff6a1f;text-shadow:0 0 10px rgba(255,90,20,.6);font-family:var(--font);letter-spacing:.08em}
      .oko-hit{position:absolute;left:12px;right:12px;bottom:12px;cursor:pointer;border:none;border-radius:9px;padding:9px;
        font-family:var(--font);font-weight:800;letter-spacing:.12em;text-transform:uppercase;font-size:12px;color:#210c02;
        background:linear-gradient(180deg,#ffb14a,#ff6a1f 60%,#c93408);box-shadow:0 4px 14px -4px rgba(255,90,20,.7);transition:.14s}
      .oko-hit:hover{filter:brightness(1.08)}
      .oko-hit:active{transform:translateY(1px)}
      .oko-hit.done{background:linear-gradient(180deg,#6d5a3a,#4a3a24);color:#e9d8c4;box-shadow:none}
      /* парящие угли */
      .oko-embers{position:absolute;inset:0;z-index:1;pointer-events:none;overflow:hidden}
      .oko-embers i{position:absolute;bottom:-10px;width:3px;height:3px;border-radius:50%;background:#ff7a1f;
        box-shadow:0 0 6px 1px rgba(255,110,20,.8);opacity:0;animation:okoEmber linear infinite}
      @keyframes okoEmber{0%{transform:translateY(0) translateX(0);opacity:0}
        12%{opacity:.9}88%{opacity:.7}100%{transform:translateY(-420px) translateX(var(--dx,20px));opacity:0}}
      /* режим Саурона — усиление */
      .oko-wrap.sauron{box-shadow:inset 0 0 140px rgba(255,50,0,.28),0 24px 60px -20px #000}
      .oko-wrap.sauron .oko-eye{filter:drop-shadow(0 0 40px rgba(255,90,20,.95)) drop-shadow(0 0 90px rgba(255,30,0,.6))}
      .oko-wrap.sauron .oko-embers i{animation-duration:2.6s !important}
      body.sauron-on::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:9;
        box-shadow:inset 0 0 200px rgba(160,20,0,.22),inset 0 -120px 160px -80px rgba(255,60,0,.25);animation:okoPulse 4s ease-in-out infinite}
      @keyframes okoPulse{0%,100%{opacity:.7}50%{opacity:1}}
      </style>
      <div class="oko-wrap" id="okoWrap">
        <div class="oko-embers" id="okoEmbers"></div>
        <div class="oko-head">
          <canvas class="oko-eye" id="okoEye" width="64" height="80"></canvas>
          <div class="oko-title">Око Саурона<sup class="oko-beta">Бета</sup></div>
          <div class="oko-sub">Ни одна жертва не укроется. Впиши имена — Око узрит их плоть, щиты и часы бессилия.</div>
        </div>
        <div class="oko-controls">
          <textarea class="oko-ta" id="okoTa" spellcheck="false" placeholder="ник в строке — за кем следит Око&#10;Дубай&#10;Славик"></textarea>
          <div class="oko-btns">
            <button class="oko-ignite" onclick="OKO.ignite()">🔥 Разжечь Око</button>
            <label class="oko-swrap"><input type="checkbox" id="okoSw" onchange="OKO.sauron(this.checked)"><span class="oko-dot"></span>Режим Саурона</label>
          </div>
          <div class="oko-mode" id="okoMode"></div>
        </div>
        <div class="oko-grid" id="okoGrid"></div>
      </div>`;
    },
    start(){
      active=true; T=0;
      // угли
      const emb=document.getElementById('okoEmbers');
      if(emb){let h='';for(let i=0;i<18;i++){const l=(i*5.5+3)%100,dur=(3.2+(i%5)*0.7),dl=(i*0.4)%4,dx=((i*37)%40-20)+'px';
        h+=`<i style="left:${l}%;animation-duration:${dur}s;animation-delay:${dl}s;--dx:${dx}"></i>`;}emb.innerHTML=h;}
      // Око — анимация
      const eye=document.getElementById('okoEye');
      if(eye){ eyeTimer=setInterval(()=>{T+=0.13;drawEye(eye);},70); drawEye(eye); }
      // подтянуть сохранённый список и сразу зажечь, если есть
      fetch('/api/oko/targets').then(r=>r.text()).then(txt=>{
        const ta=document.getElementById('okoTa'); if(ta)ta.value=txt;
        if(txt.trim()){ OKO.ignite(); }
      }).catch(()=>{});
    },
    stop(){
      active=false;
      if(eyeTimer)clearInterval(eyeTimer); if(pollTimer)clearInterval(pollTimer); if(tickTimer)clearInterval(tickTimer);
      eyeTimer=pollTimer=tickTimer=null;
      document.body.classList.remove('sauron-on');
    },
    ignite(){
      const ta=document.getElementById('okoTa'); const txt=ta?ta.value:'';
      fetch('/api/oko/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:txt})})
        .then(()=>{ fetchCards();
          if(pollTimer)clearInterval(pollTimer); pollTimer=setInterval(fetchCards,4000);
          if(tickTimer)clearInterval(tickTimer); tickTimer=setInterval(tick,1000);
        }).catch(()=>{});
    },
    hit(i,btn){
      const c=cards[i]; if(!c)return;
      fetch('/api/oko/hit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:c.name})})
        .then(r=>r.json()).then(d=>{ if(btn){btn.classList.add('done');btn.textContent='🔥 ОКО ОБРАЩЕНО';setTimeout(()=>{btn.classList.remove('done');btn.textContent='🔥 ПНУТЬ';},2600);} })
        .catch(()=>{});
    },
    sauron(on){
      sauron=!!on; applySauron();
      fetch('/api/oko/sauron',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:sauron})}).catch(()=>{});
    }
  };
})();

const GAME = (function(){
/* ── хранилища ── */
const LS_ACH='holop_ach_v1', LS_BEST='holop_best_v1', LS_RUNS='holop_runs_v1',
      LS_FAME='holop_fame_v2', LS_PERK='holop_perk_v2', LS_RUN='holop_run_v2';
const jget=(k,d)=>{try{const v=JSON.parse(localStorage.getItem(k));return v==null?d:v}catch(e){return d}};
const jset=(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}};
const num=(k)=>{try{return +localStorage.getItem(k)||0}catch(e){return 0}};
const setnum=(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}};
const clamp=v=>Math.max(0,Math.min(100,v));
const now=()=>{const d=new Date();return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');};
const pick=a=>a[Math.floor(Math.random()*a.length)];
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* ── такты ── */
const TICK=1600, PER_DAY=10, YEAR=24, GOAL=2200;

/* ── ачивки (копятся навсегда) ── */
const ACH=[
 {id:'survivor',ic:'🏆',t:'Год прожит',d:'Дотянуть до конца года (день 24)'},
 {id:'rich',ic:'👑',t:'Мироед',d:'Набить 2200 🪙'},
 {id:'freedom',ic:'🕊️',t:'Вольная',d:'Отпустить холопа на волю'},
 {id:'wedding',ic:'💒',t:'Сваха',d:'Женить холопа'},
 {id:'steward',ic:'📜',t:'Кадровик',d:'Сделать холопа управляющим'},
 {id:'inventor',ic:'⚙️',t:'Меценат',d:'Довести холопью машину до ума'},
 {id:'circus',ic:'🎪',t:'Медвежий угол',d:'Выйти на ярмарку с медведем'},
 {id:'estate',ic:'🏰',t:'Образцовая усадьба',d:'Отстроить все восемь построек'},
 {id:'saltychiha',ic:'🩸',t:'Салтычиха',d:'Довести до бунта в тираническом режиме'},
 {id:'saint',ic:'👼',t:'Святой барин',d:'Победить с моралью и лояльностью 90+'},
 {id:'ruin',ic:'⚰️',t:'Банкрот',d:'Разориться в ноль'},
 {id:'disgrace',ic:'🎩',t:'Опала',d:'Растерять всю репутацию'},
 {id:'nobeat',ic:'🤝',t:'Пацифист',d:'Победить, ни разу не дав нагоняй'},
 {id:'hoarder',ic:'🧺',t:'Скопидом',d:'Победить, ни разу не выписав премию'},
 {id:'speedrun',ic:'⚡',t:'Спидран',d:'2200 🪙 быстрее дня 18'},
 {id:'comeback',ic:'🔄',t:'С того света',d:'Поднять мораль с 5 до 80'},
 {id:'coffee',ic:'☕',t:'Бариста',d:'Подать сбитень 20 раз за забег'},
 {id:'overtime',ic:'⏻',t:'Эффективный менеджер',d:'Нажать сверхурочку 10 раз'},
 {id:'literate',ic:'📚',t:'Просветитель',d:'Обучить холопа грамоте'},
 {id:'brew',ic:'🍺',t:'Винокур',d:'Поставить брагу'},
 {id:'ghost',ic:'👻',t:'Барабашка',d:'Разгадать привидение в поместье'},
 {id:'bear',ic:'🐻',t:'Медвежья услуга',d:'Пережить медведя'},
 {id:'tsar',ic:'🎺',t:'Государев глаз',d:'Принять ревизора'},
 {id:'love',ic:'💘',t:'Амур',d:'Холоп влюбился'},
 {id:'strike',ic:'✊',t:'Профсоюз',d:'Пережить ропот и не сорваться'},
 {id:'fire',ic:'🔥',t:'Погорелец',d:'Пережить пожар'},
 {id:'treasure',ic:'💰',t:'Кладоискатель',d:'Докопаться до клада'},
 {id:'robbers',ic:'🗡️',t:'Отбился',d:'Прогнать разбойников'},
 {id:'recruit',ic:'🎖️',t:'Откупился',d:'Отбить холопа от рекрутчины'},
 {id:'allmodes',ic:'🎭',t:'Гибкий подход',d:'Побывать во всех трёх режимах за забег'},
 {id:'winter',ic:'❄️',t:'Зимовщик',d:'Дожить до зимы ни разу не уронив мораль ниже 30'},
 {id:'veteran',ic:'🎖️',t:'Ветеран',d:'Сыграть 10 забегов'},
 {id:'cats',ic:'🐈',t:'Кошачий барин',d:'Погладить кота 15 раз за забег'},
 {id:'builder',ic:'🔨',t:'Строитель',d:'Поставить 4 постройки за забег'},
];
const loadAch=()=>jget(LS_ACH,{});
const fame=()=>num(LS_FAME);
const perks=()=>jget(LS_PERK,{});

/* ── наследство рода (перки между забегами) ── */
const PERKS=[
 {id:'chest',ic:'🧰',t:'Дедов сундук',c:3, d:'Начинать с +90 🪙'},
 {id:'name', ic:'🎩',t:'Доброе имя',  c:5, d:'Стартовая репутация 68'},
 {id:'izba', ic:'🛖',t:'Крепкая изба',c:8, d:'Изба стоит на месте с первого дня'},
 {id:'smart',ic:'🧠',t:'Смышлёный холоп',c:11,d:'Стартовое «дело» 62'},
 {id:'tact', ic:'🤍',t:'Барский такт',c:14,d:'Мораль убывает на 20% медленнее'},
 {id:'trade',ic:'⚖️',t:'Ярмарочная хватка',c:18,d:'Доход +15%'},
 {id:'barn', ic:'🌾',t:'Родовой овин',c:24,d:'Овин достаётся по наследству'},
 {id:'angel',ic:'🪽',t:'Ангел-хранитель',c:32,d:'Один раз за забег спасает от гибели'},
];

/* ── сезоны ── */
const SEASONS=[
 {id:'spr',ic:'🌱',t:'Весна',d:'земля оттаяла, всё дёшево и весело',gold:0.95,drain:0.9,moral:0.45,upkeep:1.0},
 {id:'sum',ic:'☀️',t:'Лето',t2:'страда',gold:1.15,drain:1.2,moral:0.15,upkeep:1.0},
 {id:'aut',ic:'🍂',t:'Осень',t2:'урожай и ярмарки',gold:1.4,drain:1.0,moral:0.0,upkeep:1.1},
 {id:'win',ic:'❄️',t:'Зима',t2:'дрова дороги, дни коротки',gold:0.75,drain:1.3,moral:-0.2,upkeep:1.7},
];
const season=()=>SEASONS[Math.min(3,Math.floor((S.day-1)/6))];

/* ── постройки усадьбы ── */
const BLD=[
 {id:'izba', ic:'🛖',t:'Тёплая изба', c:120,d:'бодрость сама прибывает (+0.8/такт)'},
 {id:'banya',ic:'🔥',t:'Баня',        c:190,d:'мораль сама прибывает (+0.6/такт)'},
 {id:'chapel',ic:'⛪',t:'Часовня',     c:260,d:'лояльность тает вдвое медленнее'},
 {id:'barn', ic:'🌾',t:'Овин',        c:300,d:'+25% к доходу'},
 {id:'school',ic:'📚',t:'Школа',      c:380,d:'«дело» почти не падает, +10% дохода'},
 {id:'fence',ic:'🏰',t:'Частокол',    c:340,d:'воры и разбойники обходят стороной'},
 {id:'bees', ic:'🐝',t:'Пасека',      c:440,d:'+7 🪙 в день, +мораль'},
 {id:'garden',ic:'🍎',t:'Яблоневый сад',c:520,d:'+11 🪙 в день, +репутация'},
];

/* ── характеры холопа ── */
const TRAITS=[
 {id:'strong',ic:'💪',t:'Двужильный', d:'бодрость тратит вполсилы'},
 {id:'lazy',  ic:'😴',t:'Ленивый',    d:'дело чахнет быстрее, зато нервы крепкие'},
 {id:'smart', ic:'🧠',t:'Смекалистый',d:'+20% дохода, тянется к грамоте'},
 {id:'merry', ic:'🍺',t:'Гулящий',    d:'праздники и брага заходят вдвойне'},
 {id:'proud', ic:'✊',t:'Гордый',      d:'нагоняй бьёт вдвое, похвала — вдвое'},
 {id:'cat',   ic:'🐈',t:'Кошатник',   d:'кот иногда приносит монету'},
 {id:'lover', ic:'💘',t:'Влюбчивый',  d:'сердечные дела приходят раньше'},
 {id:'pious', ic:'🙏',t:'Богомольный',d:'лояльность крепка, часовня дешевле'},
];
const NAMES=['Прошка','Фомка','Ерёма','Гаврила','Тимоха','Кузьма','Афоня','Никишка','Захарка','Мирон'];
const tr=()=>TRAITS.find(t=>t.id===S.trait)||TRAITS[0];

/* ══════════ СОБЫТИЯ И СЮЖЕТНЫЕ АРКИ ══════════
   arc/step — цепочка: шаг выходит, только когда пройден предыдущий и настал день ready.
   ch: {l:текст, lg:итог, d:дельты, f:флаг, go:задержка_дней, end:финал, ach:ачивка}  */
const EVENTS=[
/* ─── АРКА «ГРАМОТА» ─── */
 {id:'gr1',arc:'gram',step:1,ic:'📖',t:'Бродячий грамотей',
  m:'У ворот человек с книгой под мышкой: «Обучу холопа буквам, барин. Недорого и почти не больно».',
  c:[{l:'Обучить (−70 🪙)',lg:'Холоп освоил буквы. Теперь читает вывески, барские записки и, кажется, между строк.',d:{gold:-70,moral:20,prod:12},f:'literate',ach:'literate',go:2},
     {l:'Прогнать грамотея',lg:'«Много будешь знать — плохо будешь спать». Грамотей ушёл, ворча про тёмный век.',d:{moral:-10,rep:-6}}]},
 {id:'gr2',arc:'gram',step:2,ic:'📓',t:'Холоп читает вслух',
  m:'Грамотный холоп нашёл вашу расходную книгу и читает её вслух. С выражением. Соседи заслушались.',
  c:[{l:'Пусть считает хозяйство',lg:'Холоп взялся за счёты. Обнаружил, что приказчик вас обкрадывал третий год.',d:{gold:120,prod:16,moral:14},go:3},
     {l:'Отобрать книгу',lg:'Книга отобрана. Холоп уже всё запомнил — память у грамотных цепкая и обидчивая.',d:{moral:-16,loyalty:-14}}]},
 {id:'gr3',arc:'gram',step:3,ic:'⚙️',t:'Холоп чертит машину',
  m:'На заднем дворе стоит штука из бочки, колеса и верёвок. По задумке — сама воду качает. По виду — сама разваливается.',
  c:[{l:'Дать денег на затею (−90 🪙)',lg:'Машина заработала! Вода идёт сама, соседи ходят смотреть, холоп ходит гордый.',d:{gold:-90,prod:30,moral:26,rep:24},f:'machine',ach:'inventor',go:4},
     {l:'Сделать управляющим',lg:'Холоп назначен управляющим. Считает лучше барина, спорит вежливо и всегда прав.',d:{moral:30,loyalty:26,prod:22,gold:60},end:'steward'},
     {l:'Запретить самодеятельность',lg:'Машина разобрана на дрова. Холоп молча смотрел, как ломают его бочку.',d:{moral:-28,loyalty:-20}}]},
 {id:'gr4',arc:'gram',step:4,ic:'🏭',t:'За машиной приехали из губернии',
  m:'Чиновник в очках осмотрел машину и предлагает выкупить чертёж. Холоп смотрит на вас во все глаза.',
  c:[{l:'Продать чертёж (+320 🪙)',lg:'Чертёж продан. Холоп кивнул и ушёл в овин. Больше он ничего не изобретал.',d:{gold:320,moral:-24,loyalty:-18}},
     {l:'Отправить холопа с машиной в столицу',lg:'Холоп уехал показывать машину государю. Пишет письма. Хорошо пишет.',end:'inventor'}]},
/* ─── АРКА «СЕРДЕЧНАЯ» ─── */
 {id:'lv1',arc:'love',step:1,ic:'💘',t:'Холоп влюбился',
  m:'Ходит, вздыхает, вместо работы вырезает на заборе сердечки. Забор, надо сказать, стал наряднее.',ach:'love',cond:s=>s.trait==='lover'||s.day>=4,
  c:[{l:'Благословить',lg:'Барин благословил. Холоп светится и работает за двоих — правда, вдвое медленнее.',d:{moral:28,loyalty:24,prod:8},go:2},
     {l:'Запретить и загрузить работой',lg:'Любовь запрещена указом. Холоп работает молча и страшно.',d:{moral:-26,loyalty:-18,prod:16}}]},
 {id:'lv2',arc:'love',step:2,ic:'🎀',t:'Сваты у ворот',
  m:'Приехали сваты из соседней деревни. Невеста — Марфа, приданое — коза. Холоп краснеет и мнёт шапку.',
  c:[{l:'Сыграть свадьбу (−170 🪙)',lg:'Свадьба гуляла три дня. Казна пуста, изба полна, все довольны.',d:{gold:-170,moral:40,loyalty:40,rep:25},f:'wed',go:3},
     {l:'Выкупить невесту у соседа (−260 🪙)',lg:'Марфа выкуплена и переехала. Соседский барин обижен, ваш холоп — нет.',d:{gold:-260,moral:44,loyalty:44,rep:-8},f:'wed',go:3},
     {l:'Отказать сватам',lg:'Сваты уехали. Холоп не сказал ни слова, но взгляд запомнился надолго.',d:{moral:-32,loyalty:-30},go:3}]},
 {id:'lv3',arc:'love',step:3,ic:'💒',t:'Молодые просятся своим двором',
  m:'Женатый холоп просит отделиться: свой угол, своя корова, оброк исправно.',cond:s=>s.flags.wed,
  c:[{l:'Отпустить своим двором',lg:'Молодые зажили отдельно и платят исправно. Иногда заходят с пирогами.',end:'wedding'},
     {l:'Держать при себе',lg:'Молодые остались в людской. Тесно, шумно, зато под присмотром.',d:{moral:-14,loyalty:-10,gold:60}}]},
 {id:'lv3b',arc:'love',step:3,ic:'🌙',t:'Ночью скрипнула калитка',
  m:'Холоп и Марфа собрали узелок. Стоят у калитки и смотрят на барское окно.',cond:s=>!s.flags.wed,
  c:[{l:'Сделать вид, что спите',lg:'Ушли вдвоём в ночь. Вы стояли у окна и почему-то улыбались.',end:'runaway'},
     {l:'Окликнуть',lg:'Вернулись. Молчат. Работают. Смотрят в пол.',d:{moral:-30,loyalty:-24,prod:10}}]},
/* ─── АРКА «ВОЛЬНАЯ» ─── */
 {id:'fr1',arc:'free',step:1,ic:'🕊️',t:'Разговор о воле',
  m:'Вечером у печи холоп заговорил осторожно: «А сколько, барин, вольная-то нынче стоит?»',cond:s=>s.day>=6,
  c:[{l:'Назвать цену: 400 🪙',lg:'Цена названа. Холоп кивнул и стал считать в уме. Считает он теперь быстро.',d:{loyalty:8,prod:14},f:'price',go:4},
     {l:'«Не для того тебя кормлю»',lg:'Разговор окончен. Холоп больше не заговаривал — но и не забыл.',d:{moral:-18,loyalty:-14}}]},
 {id:'fr2',arc:'free',step:2,ic:'💵',t:'Холоп принёс выкуп',
  m:'На стол легла тряпица с медью и серебром. Копил, подрабатывал, недоедал. Тут ровно столько, сколько вы сказали.',
  c:[{l:'Взять деньги, дать вольную (+400 🪙)',lg:'Вольная подписана и деньги взяты. По закону — честно. По совести — как посмотреть.',d:{gold:400},end:'buyout'},
     {l:'Дать вольную даром',lg:'Вы порвали расписку и подписали вольную. Холоп ушёл, обернувшись трижды.',end:'freedom'},
     {l:'Взять деньги и передумать',lg:'Деньги взяты, вольная не дана. Такое не прощают.',d:{gold:400,moral:-45,loyalty:-45,rep:-25}}]},
/* ─── АРКА «РЕВИЗОР» ─── */
 {id:'rv1',arc:'rev',step:1,ic:'📨',t:'Слух о ревизоре',
  m:'По уезду ползёт слух: едет государев человек с проверкой. У соседа уже красят забор.',cond:s=>s.day>=4,
  c:[{l:'Готовиться: красить и прятать (−80 🪙)',lg:'Забор покрашен, недоимки спрятаны, холоп выучен говорить «всем доволен».',d:{gold:-80,rep:10,energy:-14},f:'ready',go:2},
     {l:'Ничего не делать',lg:'«Авось пронесёт». Классическая стратегия.',go:2}]},
 {id:'rv2',arc:'rev',step:2,ic:'🎺',t:'Карета у ворот',
  m:'Государев глаз приехал смотреть, как вы тут хозяйствуете. Смотрит внимательно.',ach:'tsar',
  c:[{l:'Накрыть стол (−110 🪙)',lg:'Ревизор доволен, ел много, писал мало.',d:{gold:-110,rep:26}},
     {l:'Показать всё как есть',lg:s=>s.flags.ready?'Ревизор всё осмотрел и остался приятно удивлён.':'Ревизор всё записал. Как есть — оказалось так себе.',d:{rep:-14,moral:10}},
     {l:'Выставить холопа за главного',lg:'Холоп провёл экскурсию блестяще. Ревизор в восторге, холоп горд, барин лишний.',d:{rep:22,moral:16,loyalty:14}}]},
/* ─── АРКА «РОПОТ» ─── */
 {id:'rt1',arc:'riot',step:1,ic:'😠',t:'Ропот в людской',
  m:'Из людской доносится не песня, а разговор. Замолкают, когда вы проходите мимо.',cond:s=>s.st.moral<40,
  c:[{l:'Выйти и поговорить',lg:'Барин вышел и выслушал. Требования: щи гуще и выходной. Договорились.',d:{moral:22,loyalty:16,gold:-40},ach:'strike'},
     {l:'Сделать вид, что не слышали',lg:'Разговоры не смолкли. Стали тише.',d:{moral:-8},go:2},
     {l:'Прикрикнуть',lg:'Замолчали сразу. Слишком сразу.',d:{moral:-16,loyalty:-12,prod:8},go:2}]},
 {id:'rt2',arc:'riot',step:2,ic:'🗣️',t:'Подстрекатель из-за реки',
  m:'В людской ночует чужой мужик и рассказывает, как у них барина «попросили».',
  c:[{l:'Выгнать чужака',lg:'Чужак выгнан. Слова его остались.',d:{loyalty:-8,moral:-6},go:2},
     {l:'Позвать за стол и выслушать',lg:'Барин напоил чужака и переспорил. Холоп смотрел с уважением.',d:{moral:20,loyalty:18,rep:8},ach:'strike'},
     {l:'Позвать урядника (−40 🪙)',lg:'Урядник пришёл, посмотрел, взял денег и ушёл. Стало хуже.',d:{gold:-40,moral:-20,loyalty:-18,rep:-12},go:2}]},
 {id:'rt3',arc:'riot',step:3,ic:'🔥',t:'Вилы у крыльца',
  m:'Утром на крыльце стоит холоп. В руках вилы. Смотрит спокойно — и это страшнее крика.',
  c:[{l:'Выйти без охраны и говорить',lg:s=>s.st.moral>35?'Барин вышел один. Говорили долго. Вилы прислонили к стене.':'Барин вышел один. Говорить не дали.',d:{moral:26,loyalty:22,rep:12}},
     {l:'Пообещать всё (−200 🪙)',lg:'Обещано всё и сразу: паёк, выходные, новая изба. Дорого, но живой.',d:{gold:-200,moral:34,loyalty:28}},
     {l:'Запереться в доме',lg:'Барин заперся. К вечеру в людской было пусто, а в лесу — многолюдно.',end:'riot'}]},
/* ─── АРКА «МЕДВЕДЬ» ─── */
 {id:'br1',arc:'bear',step:1,ic:'🐻',t:'Медведь в малиннике',
  m:'В барском малиннике поселился медведь. Сидит, ест, никого не боится, малину не оставляет.',ach:'bear',
  c:[{l:'Послать холопа прогнать',lg:'Холоп вернулся без штанов, но с медовухой. Медведь ушёл сам и вернулся к ужину.',d:{energy:-25,moral:-10,rep:12},go:2},
     {l:'Прикормить (−60 🪙)',lg:'Медведь прикормлен. Теперь он тоже на довольствии и смотрит на вас как на своего.',d:{gold:-60,moral:16,rep:10},f:'misha',go:2},
     {l:'Идти самому с рогатиной',lg:'Барин вышел на медведя. Медведь ушёл из уважения. Холоп в шоке, репутация в небе.',d:{rep:30,loyalty:25,moral:18}}]},
 {id:'br2',arc:'bear',step:2,ic:'🎪',t:'Медведь научился плясать',
  m:'Холоп с медведем что-то репетируют за овином. Медведь топчется в такт. Выглядит перспективно.',cond:s=>s.flags.misha,
  c:[{l:'Везти на ярмарку',lg:'Медведь плясал, народ платил, холоп собирал шапку. Балаган удался.',d:{gold:280,rep:26,moral:24},ach:'circus',go:3},
     {l:'Запретить балаган',lg:'Медведь распущен, холоп расстроен, ярмарка обошлась без вас.',d:{moral:-18,rep:-8}}]},
 {id:'br3',arc:'bear',step:3,ic:'🎠',t:'Ярмарочный антрепренёр',
  m:'Приехал человек в клетчатом и зовёт холопа с медведем в большой балаган. Обещает славу и долю.',
  c:[{l:'Отпустить с медведем',lg:'Холоп уехал с медведем в балаган. Присылает афиши и деньги.',end:'circus'},
     {l:'Не отпускать (+180 🪙 отступных)',lg:'Антрепренёр заплатил отступных и уехал. Холоп смотрел вслед телеге.',d:{gold:180,moral:-20,loyalty:-16}}]},
/* ─── АРКА «КЛАД» ─── */
 {id:'tr1',arc:'trs',step:1,ic:'🗺️',t:'Бумага в подполе',
  m:'Холоп чинил пол и нашёл под доской бумагу. На бумаге — кривая карта и слово «тута».',
  c:[{l:'Копать вместе',lg:'Копали до ночи. Нашли горшок черепков и одну монету. Зато весело было.',d:{gold:20,moral:18,energy:-16,loyalty:12},go:3},
     {l:'Отправить холопа копать одного',lg:'Холоп копал один и вернулся мрачный. Может, не всё показал.',d:{energy:-22,moral:-12},go:3},
     {l:'Выбросить бумагу',lg:'Бумага сожжена. Спится спокойнее.',d:{moral:-6}}]},
 {id:'tr2',arc:'trs',step:2,ic:'💰',t:'Под старой яблоней звякнуло',
  m:'Лопата ударилась о железо. Под корнями — окованный сундучок дедовых времён.',ach:'treasure',
  c:[{l:'Забрать всё себе (+340 🪙)',lg:'Клад изъят в казну. Холоп молча отряхнул руки и ушёл.',d:{gold:340,loyalty:-22,moral:-16}},
     {l:'Поделить по-честному (+190 🪙)',lg:'Клад поделён. Холоп растроган и, кажется, прослезился в шапку.',d:{gold:190,moral:26,loyalty:24,rep:10}},
     {l:'Отдать всё холопу',lg:'Барин отдал клад целиком. Холоп не понял, но запомнил навсегда.',d:{moral:36,loyalty:40,rep:16}}]},
/* ─── АРКА «ПРИЗРАК» ─── */
 {id:'gh1',arc:'ghost',step:1,ic:'👻',t:'Воет в западном крыле',
  m:'Ночью что-то воет и гремит. Холоп отказывается туда ходить даже за деньги.',rare:1,
  c:[{l:'Пойти проверить вдвоём',lg:'Шли со свечой и ухватом. Что-то грохнуло и затихло. Стало ещё интереснее.',d:{moral:10,energy:-10},go:2},
     {l:'Запереть крыло навсегда',lg:'Крыло заперто. Теперь там живёт легенда, и она вполне довольна.',d:{moral:-8,rep:8}},
     {l:'Брать деньги за экскурсии (+90 🪙)',lg:'Соседи платят, чтобы посмотреть на «духа». Гениально и почти честно.',d:{gold:90,rep:14,moral:6}}]},
 {id:'gh2',arc:'ghost',step:2,ic:'🕯️',t:'Ночное дежурство',
  m:'Сидите вдвоём в темноте. В полночь загремело прямо над головой.',ach:'ghost',
  c:[{l:'Посветить наверх',lg:'Это был кот в ведре. Кот теперь тоже на довольствии и делает вид, что так и задумано.',d:{moral:24,loyalty:18,rep:6}},
     {l:'Бежать',lg:'Бежали вдвоём и одинаково быстро. Утром договорились никому не рассказывать.',d:{moral:16,loyalty:20,rep:-10}}]},
/* ─── АРКА «РАЗБОЙНИКИ» ─── */
 {id:'rb1',arc:'rob',step:1,ic:'🗡️',t:'Лихие люди в лесу',
  m:'На тракте пошаливают. Прислали слово: «Барин, готовь двести — и живи спокойно».',cond:s=>s.day>=8,
  c:[{l:'Заплатить (−200 🪙)',lg:'Заплачено. Спокойно. Стыдно, но спокойно.',d:{gold:-200,rep:-10}},
     {l:'Готовиться к обороне (−90 🪙)',lg:'Куплены рогатины и фонари. Холоп точит косу с непонятным воодушевлением.',d:{gold:-90,prod:-6,loyalty:10},f:'armed',go:2},
     {l:'Не отвечать',lg:'Ответа не последовало. Молчание тоже ответ.',go:2}]},
 {id:'rb2',arc:'rob',step:2,ic:'🔥',t:'Ночью у ворот',
  m:'Собаки заходятся лаем. У ворот огни и голоса.',
  c:[{l:'Дать отпор',lg:s=>(s.flags.armed||s.blds.fence)?'Отбились! Разбойники ушли ни с чем, а холоп наутро стал деревенским героем.':'Отбивались чем было. Отбились, но овин подпалили.',
      d:{gold:-60,moral:20,loyalty:26,rep:24,energy:-24},ach:'robbers'},
     {l:'Откупиться на месте (−260 🪙)',lg:'Отдали серебро и остались целы. Холоп смотрел, как уносят казну.',d:{gold:-260,moral:-10,rep:-12}}]},
/* ─── АРКА «РЕКРУТЧИНА» ─── */
 {id:'rc1',arc:'rec',step:1,ic:'🥁',t:'Рекрутский набор',
  m:'В уезде набор. По разнарядке с вашего двора — одна душа. Барабанщик уже у ворот.',cond:s=>s.day>=10,
  c:[{l:'Откупиться (−230 🪙)',lg:'Квитанция получена, холоп остался. Дорого, но дом без него пустой.',d:{gold:-230,loyalty:30,moral:26},ach:'recruit'},
     {l:'Отдать холопа',lg:'Холопа увели под барабан. Вы махнули рукой с крыльца.',end:'recruit'},
     {l:'Спрятать в лесу на неделю',lg:'Холоп пересидел в шалаше. Комары были беспощадны, но набор прошёл мимо.',d:{energy:-30,moral:-8,loyalty:18,rep:-10}}]},
/* ─── ОДИНОЧНЫЕ СОБЫТИЯ ─── */
 {id:'e1',ic:'🛌',t:'Холоп просит выходной',m:'«Барин, дай продыху — спина не казённая, а своя».',
  c:[{l:'Дать выходной',lg:'Выходной дан. Холоп кланяется до земли и уходит спать до обеда.',d:{moral:16,loyalty:12,energy:22,prod:-8}},
     {l:'Отказать',lg:'В выходном отказано. Холоп мрачно берётся за вилы… то есть за дело.',d:{moral:-14,loyalty:-8,prod:12}}]},
 {id:'e2',ic:'📜',t:'Гонец: недоимка!',m:'Прискакал гонец — с вас оброк в казну княжью. И побыстрее.',
  c:[{l:'Заплатить (−70 🪙)',lg:'Оброк уплачен. Казна похудела, честь цела.',d:{gold:-70,rep:6}},
     {l:'Отправить холопа отработать',lg:'Холоп отправлен отрабатывать. Вернулся злой и мокрый.',d:{energy:-22,moral:-8,prod:6}},
     {l:'Спрятаться в погребе',lg:'Барин просидел в погребе три часа. Гонец уехал. Позор, но бесплатно.',d:{rep:-14,moral:6}}]},
 {id:'e3',ic:'🏚️',t:'Сосед переманивает',m:'Соседний барин сулит холопу тёплую избу и щи с мясом. Щи, говорят, действительно с мясом.',
  c:[{l:'Поднять паёк (−50 🪙)',lg:'Паёк поднят. Холоп остаётся верен щам.',d:{gold:-50,loyalty:24,moral:10}},
     {l:'Пригрозить соседу',lg:'Сосед обиделся, холоп напуган. Все несчастны.',d:{loyalty:8,moral:-18,rep:-12}},
     {l:'Предложить соседу купить (+150 🪙)',lg:'Торг вышел знатный. Холоп вернулся сам через день — не понравилось.',d:{gold:150,loyalty:-26,moral:-16}}]},
 {id:'e4',ic:'🤒',t:'Холоп занемог',m:'Кашляет, жар, бормочет во сне что-то про светлое будущее.',
  c:[{l:'Позвать лекаря (−60 🪙)',lg:'Лекарь поставил на ноги и выписал счёт на две строки.',d:{gold:-60,energy:32,moral:14}},
     {l:'«Само пройдёт»',lg:'Холоп через силу вышел на работу. Кашляет назло и с намёком.',d:{energy:-18,moral:-16,prod:8}},
     {l:'Лечить брагой',lg:'Народная медицина. Холоп здоров, но песни пел до утра.',d:{energy:18,moral:20,prod:-14}}]},
 {id:'e5',ic:'🎊',t:'Праздник урожая',m:'Вся деревня гуляет. Холоп косится на околицу, как кот на сметану.',season:'aut',
  c:[{l:'Отпустить гулять',lg:'Холоп гуляет и славит доброго барина на всю деревню.',d:{moral:24,loyalty:16,prod:-10,rep:12}},
     {l:'Работать в праздник (+110 🪙)',lg:'Пока все гуляют — холоп куёт монету и обиду.',d:{gold:110,moral:-22,energy:-14,task:2,rep:-10}}]},
 {id:'e6',ic:'🐈',t:'Кот украл сметану',m:'Барский кот опрокинул кринку. Холоп стоит рядом с виноватым видом. Кот — с довольным.',
  c:[{l:'Обвинить холопа',lg:'Холоп наказан за кота. Кот доволен. Справедливость плачет в сенях.',d:{moral:-20,loyalty:-14,prod:6}},
     {l:'Обвинить кота',lg:'Кот приговорён к лишению сметаны на сутки. Холоп еле сдержал смех.',d:{moral:16,loyalty:12}}]},
 {id:'e7',ic:'💃',t:'Соседи зовут на бал',m:'Приглашение на бал. Ехать надо в приличном, а приличное в закладе.',
  c:[{l:'Ехать (−100 🪙)',lg:'Барин блистал. Про холопа рассказывал так, будто их у него сорок.',d:{gold:-100,rep:28,moral:6}},
     {l:'Остаться дома',lg:'Барин остался дома и весь вечер играл с холопом в шашки. Тот выиграл трижды.',d:{moral:18,loyalty:16,rep:-8}}]},
 {id:'e8',ic:'🐺',t:'Волки у околицы',m:'Ночью воют волки. Скотина беспокоится, холоп не спит, барин делает вид, что спит.',season:'win',
  c:[{l:'Ставить капканы (−45 🪙)',lg:'Капканы поставлены. Поймался соседский пёс. Сосед обижен, пёс тоже.',d:{gold:-45,rep:-10,moral:6}},
     {l:'Жечь костры всю ночь',lg:'Всю ночь жгли костры и разговаривали. Волки ушли, а разговор запомнился.',d:{energy:-24,moral:14,loyalty:14}}]},
 {id:'e9',ic:'🛏️',t:'Барин занемог',m:'Вы слегли. Хозяйство осталось на холопе. Целиком.',rare:1,
  c:[{l:'Довериться холопу',lg:'Холоп справился, ничего не украл и даже приумножил. Совестно немножко.',d:{gold:90,loyalty:26,moral:20,prod:14}},
     {l:'Руководить из постели',lg:'Барин руководил хрипом и жестами. Вышло скверно и обидно для всех.',d:{prod:-14,moral:-10,energy:-10}}]},
 {id:'e10',ic:'🌾',t:'Урожай сам-семь',m:'Уродилось так, что телеги не хватает. Небывалое дело, соседи ходят щупать.',season:'aut',
  c:[{l:'Продать всё (+190 🪙)',lg:'Продано на ярмарке. Казна звенит, амбар пуст, холоп без сил.',d:{gold:190,moral:-8,energy:-18}},
     {l:'Часть раздать деревне',lg:'Раздали соседям. Про доброго барина поют песни. Правда, без имени.',d:{gold:70,rep:32,moral:22,loyalty:18}}]},
 {id:'e11',ic:'📝',t:'Донос на барина',m:'Кто-то написал в губернию, что вы холопа мучаете. Требуют объяснений в трёх экземплярах.',
  c:[{l:'Откупиться (−130 🪙)',lg:'Бумага утеряна в пути. Так бывает, особенно за 130 🪙.',d:{gold:-130,rep:-6}},
     {l:'Позвать холопа свидетелем',lg:s=>s.st.loyalty>50?'Холоп сказал правду. Правда оказалась в вашу пользу.':'Холоп сказал правду. Правда оказалась не в вашу пользу.',d:{rep:14,loyalty:10,moral:10}}]},
 {id:'e12',ic:'🥶',t:'Мороз лютый',m:'Ударил мороз. В людской изба выстыла, дров мало, а зима только начала.',season:'win',
  c:[{l:'Отдать барские дрова',lg:'Барин мёрз в кабинете, холоп спал в тепле. Небывалое дело для этих мест.',d:{moral:30,loyalty:30,energy:14,rep:10}},
     {l:'Пусть терпит',lg:'Холоп спал в тулупе и обиде. Обида грела лучше.',d:{moral:-24,energy:-22,loyalty:-16}}]},
 {id:'e13',ic:'🎭',t:'Заезжий актёр',m:'Труппа даёт представление на площади. Холоп никогда не видел театра.',
  c:[{l:'Свести на спектакль (−40 🪙)',lg:'Холоп плакал на трагедии и хлопал громче всех. Актёры кланялись ему отдельно.',d:{gold:-40,moral:28,loyalty:16}},
     {l:'Не до глупостей',lg:'Театр отменяется. Работа — вот настоящее искусство.',d:{moral:-12,prod:8}}]},
 {id:'e14',ic:'🧾',t:'Дедовы долги',m:'В сундуке нашлись расписки покойного батюшки. Долгов больше, чем ожидалось. Заметно больше.',rare:1,
  c:[{l:'Платить честно (−160 🪙)',lg:'Долги уплачены. Имя чисто, кошелёк пуст, спина прямая.',d:{gold:-160,rep:26,moral:8}},
     {l:'Сжечь расписки',lg:'Бумаги горят хорошо. Совесть — хуже.',d:{rep:-26,moral:-10}}]},
 {id:'e15',ic:'🔥',t:'Пожар в овине!',m:'Горит овин. Огонь весёлый, ветер попутный, вёдра далеко.',rare:1,ach:'fire',
  c:[{l:'Тушить всем миром',lg:'Потушили. Холоп вынес из огня поросёнка и стал героем деревни.',d:{energy:-30,moral:22,loyalty:24,rep:20,gold:-50}},
     {l:'Спасать казну',lg:'Казна цела, овин нет. Холоп смотрел молча — это было хуже криков.',d:{gold:70,moral:-32,loyalty:-26}}]},
 {id:'e16',ic:'🍯',t:'Цыгане с медведем',m:'Табор встал у реки. Предлагают погадать, продать медведя и купить вашу лошадь.',rare:1,
  c:[{l:'Погадать (−30 🪙)',lg:'Нагадали дальнюю дорогу и казённый дом. Холоп побледнел, барин рассмеялся.',d:{gold:-30,moral:-6,rep:6}},
     {l:'Не связываться',lg:'Табор уехал к утру. Лошадь на месте — уже хорошо.',d:{rep:4}}]},
 {id:'e17',ic:'🧵',t:'Ярмарка в уезде',m:'Большая ярмарка. Можно выгодно продать, а можно и прогулять.',
  c:[{l:'Торговать (+130 🪙)',lg:'Наторговали знатно. Холоп таскал мешки и считал в уме быстрее приказчика.',d:{gold:130,energy:-20,prod:8}},
     {l:'Гулять с холопом (−60 🪙)',lg:'Ели пряники, смотрели на карусель, купили свистульку. Лучший день в году.',d:{gold:-60,moral:32,loyalty:28}}]},
 {id:'e18',ic:'🪶',t:'Письмо из столицы',m:'Дальний родственник зовёт вложиться в верное дело. Дело называется «пароходное общество».',rare:1,
  c:[{l:'Вложить (−150 🪙)',lg:'Деньги вложены. Остаётся ждать.',d:{gold:-150,rep:8},risk:'ship'},
     {l:'Вежливо отказать',lg:'Отказано в изящных выражениях. Родственник обиделся, деньги целы.',d:{rep:-4}}]},
 {id:'e19',ic:'🐓',t:'Пропала птица',m:'Со двора пропали три курицы. Холоп клянётся, что это лиса. Лиса молчит.',
  c:[{l:'Поверить холопу',lg:'Барин поверил. На следующий день у крыльца лежала лиса. Совпадение?',d:{moral:18,loyalty:16}},
     {l:'Устроить розыск',lg:'Обыскали людскую. Кур не нашли, доверие потеряли.',d:{moral:-20,loyalty:-18,prod:6}}]},
 {id:'e20',ic:'🌧️',t:'Дожди зарядили',m:'Льёт третий день. Работа встала, все сидят по избам и смотрят в окно.',season:'spr',
  c:[{l:'Отпустить по избам',lg:'Все отдыхали. Барин впервые за месяц выспался.',d:{moral:16,energy:22,prod:-10}},
     {l:'Гнать работать под дождём',lg:'Работали под дождём. Простыли все, включая барина.',d:{prod:12,energy:-24,moral:-16}}]},
];

/* ── финалы ── */
const ENDS={
 steward:{w:1,t:'УПРАВЛЯЮЩИЙ',ic:'📜',ach:'steward',m:'Холоп ведёт хозяйство лучше вас. Вы теперь просто живёте здесь. И вам, честно говоря, нравится.'},
 inventor:{w:1,t:'ИЗОБРЕТАТЕЛЬ',ic:'⚙️',ach:'inventor',m:'Ваш холоп показывает машину в столице. В газете написали «крепостной механик». Вас упомянули в скобках.'},
 wedding:{w:1,t:'СВОИМ ДВОРОМ',ic:'💒',ach:'wedding',m:'Молодые зажили отдельно, платят исправно и зовут в гости на Пасху. Круговорот в природе.'},
 runaway:{w:1,t:'УШЛИ ВДВОЁМ',ic:'🌙',ach:'freedom',m:'Вы стояли у окна и не окликнули. Утром в людской было пусто, а на столе — записка с одним словом «спасибо».'},
 freedom:{w:1,t:'ВОЛЬНАЯ',ic:'🕊️',ach:'freedom',m:'Вы отпустили холопа даром. Он ушёл свободным, а вы остались с пустым домом и странным теплом в груди.'},
 buyout:{w:1,t:'ВЫКУПИЛСЯ',ic:'💵',ach:'freedom',m:'Он честно выкупился, вы честно отпустили. Через год он открыл лавку и присылает вам чай.'},
 circus:{w:1,t:'БАЛАГАН',ic:'🎪',ach:'circus',m:'Холоп с медведем гремят на всю губернию. На афише написано «Прошка и Михайло». Вас не написали, но деньги шлют.'},
 estate:{w:1,t:'ОБРАЗЦОВАЯ УСАДЬБА',ic:'🏰',ach:'estate',m:'Восемь построек, полные амбары, довольный холоп. Соседи приезжают перенимать опыт и завидовать.'},
 rich:{w:1,t:'МИРОЕД',ic:'👑',ach:'rich',m:'Две тысячи с лишком монет! Соседи шепчутся, холоп худеет, но какая же у вас казна.'},
 survivor:{w:1,t:'ГОД ПРОЖИТ',ic:'🏆',ach:'survivor',m:'Полный круг: весна, лето, осень, зима. Холоп жив, вы тоже, усадьба стоит. Это успех.'},
 riot:{w:0,t:'БУНТ',ic:'🔥',m:'Холоп поднял бунт, забрал вилы и ушёл в лес. Говорят, там теперь целая артель таких.'},
 escape:{w:0,t:'ПОБЕГ',ic:'🏃',m:'Холоп сбежал к соседу — там паёк жирнее и барин не орёт по утрам.'},
 dead:{w:0,t:'ЗАГНАЛ',ic:'💀',m:'Холоп слёг окончательно. Лекарь развёл руками, сосед покачал головой, а вы всё считаете убытки.'},
 ruin:{w:0,t:'РАЗОРЕНИЕ',ic:'⚰️',ach:'ruin',m:'Казна пуста, имение заложено. Холоп предложил вам работу у соседа. Вы думаете.'},
 disgrace:{w:0,t:'ОПАЛА',ic:'🎩',ach:'disgrace',m:'В уезде вас больше не принимают. Приглашения не приходят, а урядник здоровается через раз.'},
 recruit:{w:0,t:'РЕКРУТЧИНА',ic:'🥁',m:'Холопа увели под барабан на двадцать пять лет. Дом стал очень тихим.'},
};

/* ── состояние ── */
let S=null, iv=null, fiv=null, keyh=null, prevLog=0, prevShop='', prevAch=0;

function fresh(){
 const p=perks(), blds={};
 if(p.izba) blds.izba=1;
 if(p.barn) blds.barn=1;
 return {v:2, name:pick(NAMES), trait:pick(TRAITS).id,
  st:{moral:70,energy:80,loyalty:55,prod:p.smart?62:45},
  rep:p.name?68:50, gold:60+(p.chest?90:0),
  mode:'normal', modeDay:0, day:1, tick:0, tasks:0, run:true,
  ev:null, over:null, flags:{}, arcs:{}, used:{}, cd:{}, blds:blds,
  angel:!!p.angel, minMoral:100, winterOK:1,
  cnt:{coffee:0,overtime:0,scold:0,bonus:0,cat:0,modes:{}},
  log:[{t:now(),msg:'🏰 Новый год начался. Холоп на дворе, казна при вас. С Богом.'}]};
}

/* ── журнал / ачивки ── */
function say(msg){ S.logN=(S.logN||0)+1; S.log=[{t:now(),msg}].concat(S.log).slice(0,60); }
function unlock(id){
 if(!id||!S) return; const a=loadAch(); if(a[id]) return;
 a[id]=Date.now(); jset(LS_ACH,a);
 const def=ACH.find(x=>x.id===id); if(!def) return;
 say('🏅 Ачивка: '+def.ic+' «'+def.t+'» — '+def.d);
 toast(def.ic,'Ачивка получена',def.t+' — '+def.d);
}
function toast(ic,head,body){
 const el=document.getElementById('gAchToast'); if(!el) return;
 el.innerHTML='<div style="font-size:26px">'+ic+'</div><div><div style="font-weight:700">'+esc(head)+'</div><div style="font-size:12.5px;opacity:.85">'+esc(body)+'</div></div>';
 el.classList.add('on');
 clearTimeout(el._h); el._h=setTimeout(function(){el.classList.remove('on')},4200);
}

/* ── множители ── */
const MODEF={humane:{dec:.6,gold:.8,plus:1.4,minus:.7},normal:{dec:1,gold:1,plus:1,minus:1},tyrant:{dec:1.5,gold:1.45,plus:.7,minus:1.5}};
function goldMult(){
 const p=perks(); let m=MODEF[S.mode].gold*season().gold;
 if(S.blds.barn) m*=1.25;
 if(S.blds.school) m*=1.10;
 if(S.trait==='smart') m*=1.20;
 if(p.trade) m*=1.15;
 return m;
}
function upkeep(){ return Math.round((9+Object.keys(S.blds).length*3)*season().upkeep); }
function incomePerTick(){
 const st=S.st;
 if(st.energy<=8||st.prod<=12) return 0;
 return (1.6+st.prod*0.085)*goldMult();
}
function dayIncome(){ return Math.round(incomePerTick()*PER_DAY + (S.blds.bees?7:0) + (S.blds.garden?11:0) - upkeep()); }

/* ── применение дельт ── */
function fmtd(d){
 if(!d) return '';
 const L={moral:'мораль',energy:'бодрость',loyalty:'лояльность',prod:'дело',rep:'репутация',gold:'🪙'};
 const out=[];
 ['gold','moral','energy','loyalty','prod','rep'].forEach(function(k){
   if(!d[k]) return; out.push((d[k]>0?'+':'−')+Math.abs(d[k])+' '+L[k]);
 });
 return out.join(' · ');
}
function apply(d,msg,opt){
 if(!S||S.over) return;
 opt=opt||{};
 const f=MODEF[S.mode], t=S.trait;
 const g=function(k){
  let v=d[k]||0; if(!v) return 0;
  v = v>0 ? v*f.plus : v*f.minus;
  if(t==='proud'&&(k==='moral'||k==='loyalty')&&opt.social) v*=2;
  if(t==='merry'&&opt.fun&&v>0) v*=2;
  return v;
 };
 const before=S.st.moral;
 S.st.moral=clamp(S.st.moral+g('moral'));
 S.st.energy=clamp(S.st.energy+g('energy'));
 S.st.loyalty=clamp(S.st.loyalty+g('loyalty'));
 S.st.prod=clamp(S.st.prod+g('prod'));
 if(d.rep) S.rep=clamp(S.rep+d.rep);
 if(d.gold) S.gold=Math.max(0,S.gold+d.gold);
 if(d.task) S.tasks+=d.task;
 S.minMoral=Math.min(S.minMoral,S.st.moral);
 if(S.minMoral<=5&&S.st.moral>=80) unlock('comeback');
 if(S.st.moral<=8&&before>8) say('⚠ Холоп на грани. Ещё немного — и вилы.');
 if(msg) say(msg);
 flash(d);
 checkOver();
}
function flash(d){
 const box=document.getElementById('gFlash'); if(!box||!d) return;
 const txt=fmtd(d); if(!txt) return;
 const n=document.createElement('div'); n.className='g-fl'; n.textContent=txt;
 box.appendChild(n); setTimeout(function(){ if(n.parentNode) n.parentNode.removeChild(n); },1500);
}

/* ── финал ── */
function endNow(key){
 const e=ENDS[key]; if(!e||!S||S.over) return;
 if(!e.w&&S.angel&&(key==='riot'||key==='escape'||key==='dead')){
  S.angel=false; S.st.moral=Math.max(S.st.moral,45); S.st.energy=Math.max(S.st.energy,45); S.st.loyalty=Math.max(S.st.loyalty,45);
  say('🪽 Ангел-хранитель вступился: беда прошла стороной. Второй раз не спасёт.');
  toast('🪽','Ангел-хранитель','Спас от гибели — один раз за забег'); return;
 }
 S.over=e; S.run=false; S.ev=null;
 if(e.ach) unlock(e.ach);
 if(key==='riot'&&S.mode==='tyrant') unlock('saltychiha');
 if(e.w&&S.st.moral>=90&&S.st.loyalty>=90) unlock('saint');
 if(e.w&&S.cnt.scold===0) unlock('nobeat');
 if(e.w&&S.cnt.bonus===0) unlock('hoarder');
 if(S.gold>=GOAL&&S.day<18) unlock('speedrun');
 if(Object.keys(S.cnt.modes).length>=3) unlock('allmodes');
 if(Object.keys(S.blds).length>=4) unlock('builder');
 if(Object.keys(S.blds).length>=8) unlock('estate');
 const sc=score(); if(sc>num(LS_BEST)) setnum(LS_BEST,sc);
 const gain=Math.max(1,Math.floor(sc/250)); setnum(LS_FAME,fame()+gain);
 S.fameGain=gain;
 const runs=num(LS_RUNS)+1; setnum(LS_RUNS,runs); if(runs>=10) unlock('veteran');
 say('🏁 '+e.t+'. Счёт '+sc+', славы за забег: +'+gain+'.');
 try{localStorage.removeItem(LS_RUN)}catch(e2){}
 layout();
}
function checkOver(){
 if(!S||S.over) return;
 if(S.st.moral<=0) return endNow('riot');
 if(S.st.loyalty<=0) return endNow('escape');
 if(S.st.energy<=0&&(S.sick||0)>=3) return endNow('dead');
 if(S.rep<=0&&S.day>3) return endNow('disgrace');
 if(S.gold<=0&&S.day>4&&S.noMoney>=3) return endNow('ruin');
 if(S.gold>=GOAL) return endNow('rich');
 if(Object.keys(S.blds).length>=8) return endNow('estate');
 if(S.day>YEAR) return endNow('survivor');
}
function score(){
 return Math.round(S.gold + S.tasks*4 + S.day*25 + S.rep*2 + Object.keys(S.blds).length*60 + (S.over&&S.over.w?300:0));
}

/* ── такт ── */
function tick(){
 if(!S||!S.run||S.over||S.ev) return;
 const f=MODEF[S.mode], sea=season(), t=S.trait, p=perks();
 const inc=incomePerTick();
 if(inc>0){
  S.gold+=inc;
  S.tasks+= (S.tick%2===0)?1:0;
  S.st.energy=clamp(S.st.energy-(t==='strong'?0.3:0.5));
 }
 let dm=0.55*f.dec*sea.drain, de=0.6*f.dec*sea.drain, dl=0.35*f.dec, dp=0.5;
 if(p.tact) dm*=0.8;
 if(t==='strong') de*=0.6;
 if(t==='lazy'){ dp*=1.7; dm*=0.75; }
 if(t==='pious') dl*=0.6;
 if(S.blds.chapel) dl*=0.5;
 if(S.blds.school) dp*=0.25;
 S.st.moral=clamp(S.st.moral-dm+sea.moral+(S.blds.banya?0.6:0)+(S.blds.bees?0.2:0));
 S.st.energy=clamp(S.st.energy-de+(S.blds.izba?0.8:0));
 S.st.loyalty=clamp(S.st.loyalty-dl);
 S.st.prod=clamp(S.st.prod-dp);
 S.minMoral=Math.min(S.minMoral,S.st.moral);
 if(t==='cat'&&Math.random()<0.02){ const c=6+Math.floor(Math.random()*10); S.gold+=c; say('🐈 Кот принёс монету неизвестного происхождения: +'+c+' 🪙.'); }
 S.tick++;
 if(S.tick%5===0) saveRun();
 if(S.tick>=PER_DAY) newDay();
 checkOver(); sync();
}
function newDay(){
 const seaBefore=season().id;
 if(S.st.energy<=1){
  S.sick=(S.sick||0)+1;
  S.st.moral=clamp(S.st.moral-6);
  say(S.sick>=3?'🛌 Холоп третий день не встаёт. Лекарь качает головой.'
     :'🛌 Холоп слёг — работы нет. Дайте отдых и сбитень, пока не поздно ('+S.sick+'/3).');
 } else S.sick=0;
 S.tick=0; S.day++;
 const sea=season();
 const up=upkeep();
 if(S.gold>=up){ S.gold-=up; S.noMoney=0; }
 else { S.gold=0; S.noMoney=(S.noMoney||0)+1;
   S.st.moral=clamp(S.st.moral-9); S.st.loyalty=clamp(S.st.loyalty-7);
   say('🍲 Кормить нечем — в людской пусто и мрачно. (нужно '+up+' 🪙)'); }
 if(S.blds.bees) S.gold+=7;
 if(S.blds.garden){ S.gold+=11; S.rep=clamp(S.rep+0.4); }
 if(S.blds.fence) S.rep=clamp(S.rep+0.3);
 if(S.blds.banya&&S.day%3===0){ S.st.moral=clamp(S.st.moral+8); say('🔥 Банный день. Холоп красный, довольный и пахнет берёзой.'); }
 if(S.st.moral<30) S.winterOK=0;
 if(sea.id!=='win'&&seaBefore!==sea.id) say(sea.ic+' Настала '+sea.t.toLowerCase()+' — '+(sea.t2||sea.d)+'.');
 if(sea.id==='win'&&seaBefore!=='win'){ say('❄️ Настала зима — дрова дороги, дни коротки.'); if(S.winterOK) unlock('winter'); }
 say('— День '+S.day+' · '+sea.ic+' '+sea.t+' · содержание −'+up+' 🪙 —');
 if(S.day>YEAR){ checkOver(); return; }
 const chance = 0.55 + (S.st.moral<35?0.15:0);
 if(Math.random()<chance) fireEvent();
 saveRun();
}

/* ── события ── */
function eligible(){
 const seaId=season().id, arcs=[], singles=[];
 for(let i=0;i<EVENTS.length;i++){
  const e=EVENTS[i];
  if(e.season&&e.season!==seaId) continue;
  if(e.cond&&!e.cond(S)) continue;
  if(e.arc){
   const a=S.arcs[e.arc]||{step:0,ready:0};
   if(a.step>=99) continue;
   if(e.step!==a.step+1) continue;
   if(S.day<(a.ready||0)) continue;
   if(S.used[e.id]) continue;
   arcs.push(e); continue;
  }
  if(S.used[e.id]) continue;
  if(e.rare&&Math.random()>0.4) continue;
  singles.push(e);
 }
 return {arcs:arcs,singles:singles};
}
function fireEvent(){
 const el=eligible();
 let pool=[];
 if(el.arcs.length&&(Math.random()<0.72||!el.singles.length)) pool=el.arcs;
 else pool=el.singles.length?el.singles:el.arcs;
 if(!pool.length) return;
 const e=pick(pool);
 S.used[e.id]=1; S.ev=e;
 if(e.ach) unlock(e.ach);
 say('❗ '+(e.ic||'')+' '+e.t);
 layout();
}
function choose(i){
 if(!S||!S.ev) return;
 const e=S.ev, ch=e.c[i], d=Object.assign({},ch.d||{});
 let lg=(typeof ch.lg==='function')?ch.lg(S):ch.lg;
 if(ch.risk==='ship'){
  if(Math.random()<0.5){ d.gold=(d.gold||0)+380; lg='Через неделю пришли дивиденды. Невероятно, но да: вложение окупилось втрое.'; }
  else lg='Общество лопнуло на третий день. Пароход, кажется, тоже.';
 }
 if(e.arc){
  const nx = ch.go? {step:e.step, ready:S.day+ch.go} : {step:99, ready:0};
  S.arcs[e.arc]=nx;
 }
 if(e.f) S.flags[e.f]=1;
 if(ch.f) S.flags[ch.f]=1;
 if(ch.ach) unlock(ch.ach);
 S.ev=null;
 apply(d,lg,{social:true});
 if(ch.end) endNow(ch.end);
 saveRun(); layout();
}

/* ── команды ── */
const CMDS=[
 {ic:'⛏',l:'Работай!',    h:'+дело, −бодрость', cd:3, d:{prod:14,energy:-8,moral:-4,gold:4,task:1}, m:'Холоп трудится в поте лица.'},
 {ic:'☕',l:'Сбитень',     h:'+бодрость · −6 🪙',cd:6, d:{energy:20,moral:4,gold:-6}, m:'Подан горячий сбитень. Холоп воспрял и обжёгся.', k:'coffee'},
 {ic:'🛋',l:'Отдых',       h:'+бодрость, +мораль',cd:8,d:{energy:24,moral:9,prod:-6}, m:'Холоп отдыхает. Совесть барина чиста, вроде бы.'},
 {ic:'🎖',l:'Похвалить',   h:'+мораль, +лояльность',cd:6,d:{moral:14,loyalty:10}, m:'«Молодец!» — холоп зарделся и приосанился.', social:1},
 {ic:'🗯',l:'Нагоняй',     h:'−мораль, +дело',   cd:8, d:{moral:-15,loyalty:-10,prod:12,task:1}, m:'Холоп получил нагоняй. Работает быстрее, любит меньше.', k:'scold', social:1},
 {ic:'💰',l:'Премия',      h:'+всё · −45 🪙',    cd:14,d:{moral:20,loyalty:18,prod:6,gold:-45}, m:'Премия выписана. Холоп готов на подвиги.', k:'bonus', social:1},
 {ic:'🐈',l:'Погладить кота',h:'мелочь, а приятно',cd:5,d:{moral:7,energy:3,rep:1}, m:'Кот обласкан. В доме стало теплее на пару градусов.', k:'cat', fun:1},
 {ic:'🍺',l:'Поставить брагу',h:'один раз за забег · −35 🪙',cd:6,d:{gold:-35,moral:14}, m:'Брага поставлена в погреб. Ждём-с.', f:'brew', ach:'brew', fun:1},
];
function cmd(i){
 const c=CMDS[i];
 if(!S||S.over||S.ev||!S.run) return;
 if(c.f&&S.flags[c.f]) return;
 if((S.cd[i]||0)>0) return;
 S.cd[i]=c.cd;
 if(c.k) S.cnt[c.k]=(S.cnt[c.k]||0)+1;
 if(c.f) S.flags[c.f]=1;
 if(c.ach) unlock(c.ach);
 if(S.cnt.coffee>=20) unlock('coffee');
 if(S.cnt.cat>=15) unlock('cats');
 apply(c.d,c.m,{social:c.social,fun:c.fun});
 if(c.f) layout(); else sync();
}
function overtime(){
 if(!S||S.over||S.ev||!S.run) return;
 if((S.cd.ot||0)>0) return;
 S.cd.ot=20; S.cnt.overtime++;
 if(S.cnt.overtime>=10) unlock('overtime');
 apply({prod:26,energy:-30,moral:-22,loyalty:-13,gold:12,task:2},
       'ВСЕ НА СВЕРХУРОЧКУ! Холоп смотрит с укором, но пашет.');
 sync();
}
function cool(){
 if(!S) return;
 for(const k in S.cd) if(S.cd[k]>0) S.cd[k]--;
}
function setMode(k,l){
 if(!S||S.over||S.ev||!S.run) return;
 if(S.mode===k) return;
 if(S.day-S.modeDay<1){ say('Режим уже меняли сегодня. Барину тоже нужна выдержка.'); sync(); return; }
 S.mode=k; S.modeDay=S.day; S.cnt.modes[k]=1;
 say('Режим содержания сменён на «'+l+'».'); layout();
}

/* ── усадьба ── */
function price(b){ return (S.trait==='pious'&&b.id==='chapel')?Math.round(b.c*0.7):b.c; }
function buy(id){
 if(!S||S.over||S.ev||!S.run) return;
 const b=BLD.find(x=>x.id===id); if(!b||S.blds[id]) return;
 const p=price(b); if(S.gold<p) return;
 S.gold-=p; S.blds[id]=1;
 say('🔨 Построено: '+b.ic+' '+b.t+' (−'+p+' 🪙). '+b.d+'.');
 toast(b.ic,'Постройка готова',b.t+' — '+b.d);
 if(Object.keys(S.blds).length>=4) unlock('builder');
 saveRun(); checkOver(); layout();
}

/* ── наследство ── */
function buyPerk(id){
 const p=PERKS.find(x=>x.id===id); if(!p) return;
 const have=perks(); if(have[id]) return;
 if(fame()<p.c) return;
 setnum(LS_FAME,fame()-p.c);
 have[id]=1; jset(LS_PERK,have);
 toast(p.ic,'Наследство рода',p.t+' — '+p.d);
 layout();
}

/* ── сохранение забега ── */
function saveRun(){
 if(!S||S.over) return;
 const cp=Object.assign({},S); cp.ev=S.ev?S.ev.id:null; cp.over=null; cp.run=false;
 jset(LS_RUN,cp);
}
function loadRun(){
 const r=jget(LS_RUN,null);
 if(!r||r.v!==2||r.over||!r.st) return null;
 r.run=false;
 r.ev=r.ev?(EVENTS.find(e=>e.id===r.ev)||null):null;
 return r;
}
function reset(){ try{localStorage.removeItem(LS_RUN)}catch(e){} S=fresh(); layout(); }

/* ── портрет ── */
const FACE=["...K............K...","..KIK..........KIK..","..KIIK........KIIK..",".KGGGGGGGGGGGGGGGGK.",".KGGGGGGGGGGGGGGGGK.",".KGGGGGGGGGGGGGGGGK.",".KGGEEGGGGGGGEEGGGK.",".KGGEWGGGGGGGEWGGGK.",".KGGEEGGGGGGGEEGGGK.",".KGGGGGGNNGGGGGGGGK.",".KGGGGCCCCCCCGGGGGK..","..KGGCCCCCCCCCGGGK..","..KKGGGGGGGGGGGKK...",".KGGGGGGGGGGGGGGGK..","KGGGGGGGGGGGGGGGGGK.","KGGGGCCCCCCCCCGGGGK.","KGGGGCCCCCCCCCGGGGK."];
let ft=0;
function drawFace(){
 const cv=document.getElementById('gFace'); if(!cv||!S) return;
 const x=cv.getContext('2d');
 const P={K:'#241a1e',G:'#df8236',D:'#b5651f',I:'#e3a6ac',E:'#3f9fd6',W:'#f6fbff',N:'#cf7280',C:'#f3e7cf',R:'#c8536a',Ch:'#f0a0a8',S:'#bcd9f0'};
 x.clearRect(0,0,20,18);
 const px=(a,b,c)=>{x.fillStyle=c;x.fillRect(a,b,1,1);};
 ft++;
 const m=S.st.moral, en=S.st.energy;
 const sleepy=en<=18, blink=sleepy?true:(ft%22<2);
 const mood=m>=70?'happy':m>=40?'ok':m>=15?'sad':'angry';
 for(let r=0;r<FACE.length;r++){const row=FACE[r];for(let c=0;c<row.length;c++){const ch=row[c];if(ch==='.')continue;px(c,r,P[ch]);}}
 px(6,4,P.D);px(8,4,P.D);px(11,4,P.D);
 const ce=()=>[[4,6],[5,6],[4,7],[5,7],[4,8],[5,8],[13,6],[14,6],[13,7],[14,7],[13,8],[14,8]].forEach(p=>px(p[0],p[1],P.G));
 if(season().id==='win'){ for(let c=3;c<17;c++) px(c,3,P.S); px(2,4,P.S); px(17,4,P.S); }
 if(blink){ce();[[4,7],[5,7],[13,7],[14,7]].forEach(p=>px(p[0],p[1],P.K));}
 else if(mood==='happy'){ce();[[3,7],[4,6],[5,6],[6,7]].forEach(p=>px(p[0],p[1],P.K));[[12,7],[13,6],[14,6],[15,7]].forEach(p=>px(p[0],p[1],P.K));px(2,10,P.Ch);px(3,10,P.Ch);px(16,10,P.Ch);px(17,10,P.Ch);[[8,12],[9,13],[10,13],[11,12]].forEach(p=>px(p[0],p[1],P.K));px(9,13,P.R);px(10,13,P.R);}
 else if(mood==='ok'){px(5,7,P.K);px(13,7,P.K);px(9,12,P.K);px(10,12,P.K);}
 else if(mood==='sad'){ce();[[4,7],[5,7],[13,7],[14,7]].forEach(p=>px(p[0],p[1],P.E));px(5,8,P.K);px(13,8,P.K);px(3,6,P.D);px(15,6,P.D);[[8,13],[9,12],[10,12],[11,13]].forEach(p=>px(p[0],p[1],P.K));}
 else {ce();[[4,7],[5,7],[13,7],[14,7]].forEach(p=>px(p[0],p[1],P.E));px(5,7,P.K);px(13,7,P.K);[[3,5],[4,6],[5,6]].forEach(p=>px(p[0],p[1],P.K));[[15,5],[14,6],[13,6]].forEach(p=>px(p[0],p[1],P.K));x.fillStyle=P.R;x.fillRect(8,12,4,2);}
}

/* ── тексты ── */
const col=v=>v>=60?'var(--green)':v>=30?'#e6a15a':'var(--red)';
function verdict(){
 const st=S.st, worst=Math.min(st.moral,st.energy,st.loyalty);
 if(worst<=15) return 'Так вы его загоните. Или он вас.';
 if(st.energy<=25) return 'Валится с ног. Дайте продыху, барин.';
 if(st.loyalty<=25) return 'Смотрит в сторону соседского двора. Нехорошо смотрит.';
 if(st.prod>=80) return 'Пашет как проклятый. Барин доволен, соседи завидуют.';
 if(st.moral>=85&&st.loyalty>=80) return 'Предан до гроба. Идеальный холоп, даже неловко.';
 if(st.prod<=20) return 'Прохлаждается. Требуется барское вмешательство.';
 if(S.rep>=80) return 'В уезде о вас говорят только хорошее. Подозрительно хорошее.';
 if(S.rep<=25) return 'Соседи здороваются сквозь зубы.';
 return 'Холоп в рабочем состоянии. Можно наглеть дальше.';
}
function advice(){
 const st=S.st, up=upkeep();
 if(S.gold<up*2) return '💡 Казна тощая: содержание съедает '+up+' 🪙 в день. Гоните дело или ставьте Овин.';
 if(st.energy<30) return '💡 Сбитень и отдых — иначе слёг.';
 if(st.moral<30) return '💡 Похвала, премия или гуманный режим. Мораль в ноль — это вилы.';
 if(st.loyalty<35) return '💡 Лояльность падает сама. Часовня или премия её держат.';
 if(!Object.keys(S.blds).length&&S.gold>150) return '💡 Пора строиться: усадьба работает, пока вы отдыхаете.';
 if(st.prod<35) return '💡 «Работай!» и Школа — дело само не идёт.';
 return '💡 Всё ровно. Копите на постройки и ждите событий.';
}

/* ══════════ ОТРИСОВКА ══════════
   layout() — строит каркас (редко). sync() — обновляет цифры каждый такт. */
function layout(){
 const root=document.getElementById('gRoot'); if(!root||!S) return;
 const sea=season(), t=tr(), ach=loadAch(), got=ACH.filter(a=>ach[a.id]).length, pk=perks();
 root.innerHTML=
 '<div class="g-top">'+
  '<div><div class="g-kick">изделие №1 · барский пуск · мини-игра</div>'+
   '<div class="g-h1">ПУЛЬТ ХОЛОПА</div>'+
   '<div class="g-sub">«Нажми — и он побежит»</div></div>'+
  '<div class="g-topr"><span class="g-season">'+sea.ic+' '+sea.t+'</span>'+
   '<span class="g-lamp" id="gLamp"></span><span class="g-stat" id="gStatus"></span></div>'+
 '</div>'+
 '<div class="g-hud">'+
  hud('День','gDay')+hud('Казна 🪙','gGold')+hud('Доход/день','gInc')+hud('Счёт','gScore')+hud('Рекорд','gBest')+hud('Слава','gFame')+
  '<button class="g-btn-ghost" id="gPause" onclick="GAME.pause()"></button>'+
 '</div>'+
 '<div class="g-grid">'+
  '<div class="g-col">'+
   '<div class="g-card"><div class="g-kick">Подопечный</div>'+
    '<div class="g-face"><canvas id="gFace" width="20" height="18"></canvas><div id="gFlash" class="g-flash"></div></div>'+
    '<div class="g-name">'+esc(S.name)+' <span class="g-trait" title="'+esc(t.d)+'">'+t.ic+' '+esc(t.t)+'</span></div>'+
    '<div class="g-dim" id="gMood"></div></div>'+
   '<div class="g-card"><div class="g-kick" style="margin-bottom:10px">Состояние</div><div id="gBars"></div></div>'+
   '<div class="g-card"><div class="g-kick" style="margin-bottom:8px">Ачивки · '+got+'/'+ACH.length+'</div>'+
    '<div class="g-ach">'+ACH.map(a=>'<span class="g-a '+(ach[a.id]?'on':'')+'" title="'+esc(a.t)+' — '+esc(a.d)+'">'+a.ic+'</span>').join('')+'</div></div>'+
  '</div>'+
  '<div class="g-col">'+
   '<div class="g-card">'+
    '<div class="g-row"><div class="g-kick">Режим содержания</div>'+
     '<div class="g-modes">'+[['humane','Гуманный'],['normal','Обычный'],['tyrant','Тиранический']].map(m=>
       '<button onclick="GAME.mode(\''+m[0]+'\',\''+m[1]+'\')" class="'+(S.mode===m[0]?'on':'')+'">'+m[1]+'</button>').join('')+'</div></div>'+
    '<div class="g-cmds" id="gCmds">'+CMDS.map((c,i)=>
      '<button id="gc'+i+'" onclick="GAME.cmd('+i+')"><div class="g-ci">'+c.ic+'</div>'+
      '<div class="g-cl">'+c.l+'</div><div class="g-ch">'+c.h+'</div><i class="g-cdbar"></i></button>').join('')+'</div>'+
    '<button class="g-red" id="gOt" onclick="GAME.overtime()">⏻ ВСЕ НА СВЕРХУРОЧКУ'+
      '<span id="gOtSub">большая красная кнопка · применять с наслаждением</span></button>'+
   '</div>'+
   '<div class="g-card"><div class="g-row" style="margin-bottom:10px"><div class="g-kick">Усадьба</div>'+
     '<div class="g-dim">содержание: '+upkeep()+' 🪙/день</div></div>'+
    '<div class="g-shop" id="gShop">'+BLD.map(b=>{
      const own=!!S.blds[b.id], p=price(b);
      return '<button class="g-bld '+(own?'own':'')+'" data-b="'+b.id+'" '+(own?'disabled':'onclick="GAME.buy(\''+b.id+'\')"')+
        ' title="'+esc(b.d)+'"><div class="g-ci">'+b.ic+'</div><div class="g-cl">'+b.t+'</div>'+
        '<div class="g-ch">'+(own?'построено':p+' 🪙')+'</div><div class="g-bd">'+esc(b.d)+'</div></button>';
    }).join('')+'</div></div>'+
   '<div class="g-two">'+
    '<div class="g-card"><div class="g-kick">Вердикт системы</div>'+
     '<div class="g-verd" id="gVerd"></div><div class="g-dim" id="gAdv" style="margin-top:10px"></div></div>'+
    '<div class="g-card"><div class="g-kick">Забегов сыграно</div>'+
     '<div class="g-big" style="font-size:38px">'+num(LS_RUNS)+'</div>'+
     '<div class="g-dim">Цель: '+GOAL+' 🪙, все 8 построек или прожить год (день '+YEAR+').</div>'+
     '<button class="g-btn-ghost" style="margin-top:10px" onclick="GAME.reset()">↺ новая игра</button></div>'+
   '</div>'+
   '<div class="g-card"><div class="g-row" style="margin-bottom:10px"><div class="g-kick">Наследство рода</div>'+
     '<div class="g-dim">слава: <b id="gFame2">'+fame()+'</b> ★ · капает за каждый забег</div></div>'+
    '<div class="g-perks">'+PERKS.map(p=>{
      const own=!!pk[p.id], can=fame()>=p.c;
      return '<button class="g-perk '+(own?'own':(can?'can':''))+'" '+(own?'disabled':'onclick="GAME.perk(\''+p.id+'\')"')+
       ' title="'+esc(p.d)+'"><span class="g-pi">'+p.ic+'</span><span class="g-pt">'+p.t+'</span>'+
       '<span class="g-pc">'+(own?'✓ есть':p.c+' ★')+'</span><span class="g-pd">'+esc(p.d)+'</span></button>';
    }).join('')+'</div></div>'+
   '<div class="g-log"><div class="g-kick" style="margin-bottom:10px">Барский журнал</div>'+
    '<div class="g-lines" id="gLog"></div></div>'+
  '</div>'+
 '</div>'+
 '<div id="gModal"></div><div id="gAchToast" class="g-toast"></div>';
 prevLog=-1;
 sync(); drawFace();
}
function hud(t,id){ return '<div class="g-card g-h"><div class="g-kick">'+t+'</div><div class="g-big" id="'+id+'">—</div></div>'; }

function sync(){
 if(!S||!document.getElementById('gRoot')) return;
 const st=S.st, worst=Math.min(st.moral,st.energy,st.loyalty);
 const set=(id,v)=>{const e=document.getElementById(id); if(e&&e.textContent!==String(v)) e.textContent=v;};
 const sea=season();
 set('gDay',S.day+'/'+YEAR);
 set('gGold',Math.floor(S.gold));
 const di=dayIncome(); set('gInc',(di>0?'+':'')+di);
 set('gScore',score()); set('gBest',num(LS_BEST)); set('gFame',fame());
 const pz=document.getElementById('gPause');
 if(pz) pz.textContent=S.over?'⏹ забег окончен':(S.run?'⏸ Пауза':'▶ Продолжить');
 const lamp=document.getElementById('gLamp'), stx=document.getElementById('gStatus');
 const cl=S.over?'var(--red)':worst<=15?'var(--red)':S.run?'var(--green)':'#e6a15a';
 const status=S.over?'ИГРА ОКОНЧЕНА':worst<=15?'КРИТИЧЕСКОЕ СОСТОЯНИЕ':S.ev?'СОБЫТИЕ':S.run?'ХОЛОП НА СВЯЗИ':'ПАУЗА';
 if(lamp){ lamp.style.background=cl; lamp.style.boxShadow='0 0 12px '+cl; }
 if(stx){ stx.style.color=cl; stx.textContent=status; }
 const mood=st.moral>=70?'сияет':st.moral>=40?'терпит':st.moral>=15?'приуныл':'на грани';
 set('gMood','Настроение: '+mood+' · режим: '+({humane:'гуманный',normal:'обычный',tyrant:'тиранический'})[S.mode]+' · '+sea.ic+' '+sea.t.toLowerCase());
 const bars=document.getElementById('gBars');
 if(bars){
  const G=[['Мораль',st.moral],['Бодрость',st.energy],['Лояльность',st.loyalty],['Дело',st.prod],['Репутация',S.rep]];
  if(!bars._built){
   bars.innerHTML=G.map((g,i)=>'<div class="g-brow"><div class="g-blab"><span>'+g[0]+'</span><b id="gbv'+i+'"></b></div>'+
     '<div class="g-bar"><i id="gbi'+i+'"></i></div></div>').join('');
   bars._built=1;
  }
  G.forEach((g,i)=>{
   const v=Math.round(g[1]), b=document.getElementById('gbv'+i), it=document.getElementById('gbi'+i);
   if(b){ b.textContent=v+'%'; b.style.color=col(v); }
   if(it){ it.style.width=v+'%'; it.style.background=col(v); }
  });
 }
 CMDS.forEach((c,i)=>{
  const b=document.getElementById('gc'+i); if(!b) return;
  const done=c.f&&S.flags[c.f], cd=S.cd[i]||0;
  const off=done||cd>0||!S.run||!!S.over||!!S.ev;
  b.disabled=off; b.classList.toggle('done',!!done);
  const bar=b.querySelector('.g-cdbar'); if(bar) bar.style.width=cd>0?Math.round(cd/c.cd*100)+'%':'0%';
 });
 const ot=document.getElementById('gOt');
 if(ot){ const cd=S.cd.ot||0; ot.disabled=cd>0||!S.run||!!S.over||!!S.ev;
   const sub=document.getElementById('gOtSub'); if(sub) sub.textContent=cd>0?('остывает · '+cd):'большая красная кнопка · применять с наслаждением'; }
 document.querySelectorAll('#gShop .g-bld').forEach(function(b){
  const key=b.getAttribute('data-b'); if(!key||S.blds[key]) return;
  const bd=BLD.find(x=>x.id===key); if(!bd) return;
  b.disabled=S.gold<price(bd)||!S.run||!!S.over||!!S.ev;
  b.classList.toggle('poor',S.gold<price(bd));
 });
 const v=document.getElementById('gVerd'); if(v) v.textContent=verdict();
 const a=document.getElementById('gAdv'); if(a) a.textContent=advice();
 const lg=document.getElementById('gLog');
 if(lg&&prevLog!==(S.logN||0)){
  prevLog=S.logN||0;
  lg.innerHTML=S.log.map(l=>'<div><span>'+l.t+'</span>'+esc(l.msg)+'</div>').join('');
 }
 modal();
}
function modal(){
 const box=document.getElementById('gModal'); if(!box) return;
 const key=S.over?('o'+S.over.t):S.ev?('e'+S.ev.id):'';
 if(box._key===key) return; box._key=key;
 if(S.ev){
  const e=S.ev;
  box.innerHTML='<div class="g-modal"><div class="g-dial">'+
   '<div class="g-kick">Событие · день '+S.day+' · '+season().ic+' '+season().t+'</div>'+
   '<div class="g-mic">'+(e.ic||'❗')+'</div>'+
   '<div class="g-mt">'+esc(e.t)+'</div><div class="g-mm">'+esc(e.m)+'</div>'+
   '<div class="g-choices">'+e.c.map((c,i)=>{
     const hint=fmtd(c.d);
     return '<button class="g-choice" onclick="GAME.choose('+i+')">'+esc(c.l)+
       (hint?'<span class="g-hint">'+esc(hint)+'</span>':'')+'</button>';
   }).join('')+'</div></div></div>';
  return;
 }
 if(S.over){
  const o=S.over, c=o.w?'#e6a15a':'var(--red)';
  box.innerHTML='<div class="g-modal"><div class="g-dial" style="text-align:center;border-color:'+c+'">'+
   '<div style="font-size:50px">'+o.ic+'</div>'+
   '<div class="g-mt" style="color:'+c+'">'+esc(o.t)+'</div>'+
   '<div class="g-mm">'+esc(o.m)+'</div>'+
   '<div class="g-dim" style="margin-top:14px">Счёт: <b style="color:var(--ink);font-size:20px">'+score()+'</b>'+
     ' · дней: '+S.day+' · построек: '+Object.keys(S.blds).length+' · рекорд: '+num(LS_BEST)+'</div>'+
   '<div class="g-dim" style="margin-top:6px">Славы за забег: <b style="color:var(--accent)">+'+(S.fameGain||0)+' ★</b> — тратится в «Наследстве рода».</div>'+
   '<button class="g-choice" style="margin-top:16px;text-align:center" onclick="GAME.reset()">↺ Ещё разок</button>'+
   '</div></div>';
  return;
 }
 box.innerHTML='';
}

/* ── клавиатура ── */
function onKey(e){
 if(!S||e.metaKey||e.ctrlKey||e.altKey) return;
 const tag=(e.target&&e.target.tagName||'').toLowerCase();
 if(tag==='input'||tag==='textarea'||tag==='select') return;
 if(S.ev){
  const n=parseInt(e.key,10);
  if(n>=1&&n<=S.ev.c.length){ e.preventDefault(); choose(n-1); }
  return;
 }
 if(e.code==='Space'){ e.preventDefault(); pub.pause(); return; }
 const n=parseInt(e.key,10);
 if(n>=1&&n<=CMDS.length){ e.preventDefault(); cmd(n-1); }
}

/* ── публичное ── */
const pub={
 html(){ return '<div id="gRoot"></div>'; },
 start(){
  if(!S){ S=loadRun()||fresh(); }
  this.stop();
  layout();
  iv=setInterval(function(){ if(!S||!S.run||S.over||S.ev) return; cool(); tick(); },TICK);
  fiv=setInterval(drawFace,170);
  keyh=onKey; document.addEventListener('keydown',keyh);
  if(!this._vis){ this._vis=function(){ if(document.visibilityState==='hidden'&&S&&S.run&&!S.over){ S.run=false; saveRun(); sync(); } };
    document.addEventListener('visibilitychange',this._vis); }
 },
 stop(){
  if(iv){clearInterval(iv);iv=null;} if(fiv){clearInterval(fiv);fiv=null;}
  if(keyh){document.removeEventListener('keydown',keyh);keyh=null;}
  saveRun();
 },
 cmd:cmd, overtime:overtime, choose:choose, reset:reset, buy:buy, perk:buyPerk,
 mode:setMode,
 pause(){ if(S&&!S.over&&!S.ev){ S.run=!S.run; if(!S.run) saveRun(); sync(); } },
};
return pub;
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
