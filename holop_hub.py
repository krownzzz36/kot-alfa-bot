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
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
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


def _pid_alive(pid):
    """Жив ли процесс — БЕЗ его убийства (на Windows os.kill(pid,0) убивает процесс!)."""
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
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


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

    def _run(self, coro):
        import asyncio
        if self.loop is None or self.loop.is_closed():
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=self.loop.run_forever, daemon=True).start()
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def send_code(self, phone):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        cfg = load_cfg()
        api_id = int(cfg.get("api_id") or DEFAULT_API_ID)
        api_hash = cfg.get("api_hash") or DEFAULT_API_HASH
        phone = phone.strip().replace(" ", "")

        async def _go():
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            self.client = TelegramClient(StringSession(), api_id, api_hash)
            await self.client.connect()
            sent = await self.client.send_code_request(phone)
            self.phone = phone
            self.phone_code_hash = sent.phone_code_hash

        with self.lock:
            self._run(_go())

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
        "selects": [{"id": "prof", "label": "Профессия", "options": PROFS, "default": "Воин"}],
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
        "desc": "Сканирует богатых бьющихся соперников (сорт. по серебру) и выдаёт список ников. "
                "Скопируй их из лога в «Набеги» → Цели.",
        "fields": [{"id": "want", "label": "Сколько целей найти", "kind": "number", "default": 10},
                   {"id": "pages", "label": "Сколько страниц сканировать", "kind": "number", "default": 6}],
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
    alive = _pid_alive(read_pid(mid))
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
    return _pid_alive(read_night_pid()) or _pgrep("night_smash.sh")


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


# ─────────────── разовые модули (oneshot) ───────────────
def build_args(mid, action, fields):
    f = fields or {}
    if mid == "roles":
        prof = (f.get("prof") or "Воин").strip()
        nicks = [x.strip() for x in (f.get("nick") or "").splitlines() if x.strip()]
        tail_args = ["--dry-run"] if action == "dry" else []
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
             ("id", "title", "emoji", "kind", "desc", "fields", "selects", "actions")}
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
            self._send(200, page, "text/html; charset=utf-8")
        elif p == "/api/auth/status":
            self._json({"authorized": is_authorized()})
        elif p == "/api/config":
            self._json(ui_config())
        elif p == "/api/targets":
            self._send(200, load_targets(), "text/plain; charset=utf-8")
        elif p == "/api/status_board":
            self._json(status_board())
        elif p.startswith("/api/") and p.endswith("/status"):
            mid = p.split("/")[2]
            mod = MOD.get(mid, {})
            self._json({"running": is_running(mid),
                        "state": raids_state() if mid == "raids" else ("run" if is_running(mid) else "idle"),
                        "night": night_running() if mid == "raids" else False,
                        "log": tail(mod.get("log", "")) if mod.get("log") else ""})
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
        # ── вход в аккаунт ──
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "auth":
            try:
                if parts[2] == "send_code":
                    phone = (body.get("phone") or "").strip()
                    if not phone:
                        return self._json({"ok": False, "err": "Введи номер телефона."})
                    AUTH.send_code(phone)
                    return self._json({"ok": True, "need": "code"})
                if parts[2] == "sign_in":
                    r = AUTH.sign_in(code=(body.get("code") or "").strip())
                    return self._json({"ok": True, "need": r})
                if parts[2] == "password":
                    r = AUTH.sign_in(password=(body.get("password") or ""))
                    return self._json({"ok": True, "need": r})
            except Exception as e:
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
 :root{--bg:#16171d;--panel:#1e2028;--ink:#e8e8ea;--mut:#8a8f98;--green:#2ecc71;--red:#e74c3c;
       --blue:#3b82f6;--line:#2a2d36;--card:#0f1015;}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);
      font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
 header{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--line);
        background:var(--panel);flex-wrap:wrap}
 h1{font-size:16px;margin:0 12px 0 0;font-weight:800;white-space:nowrap}
 .tab{padding:7px 13px;border-radius:9px;cursor:pointer;color:var(--mut);font-weight:600;font-size:14px;
      border:1px solid transparent}
 .tab:hover{color:var(--ink)}
 .tab.on{background:var(--card);color:var(--ink);border-color:var(--line)}
 main{padding:14px 16px}
 .desc{color:var(--mut);font-size:13px;margin:0 0 12px}
 .row{display:flex;gap:14px;align-items:stretch}
 .col-log{flex:1;min-width:0;display:flex;flex-direction:column}
 .side{width:270px;display:flex;flex-direction:column;gap:8px}
 label{display:block;color:var(--mut);font-size:12px;margin:4px 0 3px}
 input,select,textarea{width:100%;background:var(--card);color:var(--ink);border:1px solid var(--line);
      border-radius:8px;padding:8px;font:14px inherit}
 textarea{font:13px/1.5 Menlo,monospace;resize:vertical}
 button{font:600 14px inherit;border:0;border-radius:9px;padding:9px 14px;cursor:pointer;color:#fff}
 button:hover{filter:brightness(1.1)} button:active{filter:brightness(.9)}
 .b-green{background:var(--green)} .b-red{background:var(--red)} .b-blue{background:var(--blue)}
 .b-grey{background:#3a3f4a} .b-night{background:#3a3f4b;width:100%} .b-night.on{background:#6d5ae6}
 .pill{padding:4px 10px;border-radius:999px;font-weight:700;font-size:12px;margin-left:auto}
 .pill.run{background:rgba(46,204,113,.16);color:var(--green)}
 .pill.pause{background:rgba(255,255,255,.08);color:var(--mut)}
 .pill.stopped,.pill.idle{background:rgba(231,76,60,.14);color:var(--red)}
 pre.log{flex:1;min-height:340px;max-height:64vh;overflow:auto;background:var(--card);border:1px solid var(--line);
      border-radius:10px;padding:11px;margin:0;white-space:pre-wrap;word-break:break-word;
      font:12.5px/1.5 Menlo,monospace;color:#d7d7db}
 .btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
 .note{color:var(--mut);font-size:12px;min-height:16px}
 table{width:100%;border-collapse:collapse;font-size:14px}
 td,th{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
 th{color:var(--mut);font-weight:600;font-size:12px}
 .summ{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:13px}
</style></head><body>
<header><h1>🏰 Холоп — Пульт</h1><div id="tabs"></div></header>
<main id="main"></main>
<script>
const $=s=>document.querySelector(s);
let CFG=[], active=null, timer=null;

function pill(state){
  const t=state==='run'?'🟢 Работает':state==='pause'?'⏸ Пауза':
          state==='idle'?'⚪ Не запущен':state==='stopped'?'⚪ Остановлен':state;
  return `<span class="pill ${state}">${t}</span>`;
}
function fieldHTML(f){
  if(f.kind==='textarea') return `<label>${f.label}</label><textarea id="f_${f.id}" rows="${f.rows||5}" placeholder="${f.placeholder||''}"></textarea>`;
  const type=f.kind==='number'?'number':'text';
  return `<label>${f.label}</label><input id="f_${f.id}" type="${type}" value="${f.default!=null?f.default:''}">`;
}
function selectHTML(s){
  const opts=s.options.map(o=>`<option ${o===s.default?'selected':''}>${o}</option>`).join('');
  return `<label>${s.label}</label><select id="f_${s.id}">${opts}</select>`;
}
function render(mid){
  active=mid; if(timer) clearInterval(timer);
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.id===mid));
  const m=CFG.find(x=>x.id===mid); const main=$('#main');
  if(m.kind==='status'){ main.innerHTML=`<p class="desc">${m.desc||''}</p><div class="summ" id="summ">—</div>
      <table><thead><tr><th>Цель</th><th>Состояние</th><th>Обновл.</th></tr></thead><tbody id="board"></tbody></table>`;
    pollStatus(); timer=setInterval(pollStatus,2000); return; }
  let side='';
  if(m.kind==='loop'){
    side=`<div class="side"><div><button class="b-green" onclick="post('${mid}','start')">▶ Запустить</button>
      <button class="b-red" onclick="post('${mid}','stop')">⏹ Остановить</button></div>
      <button id="nightBtn" class="b-night" onclick="toggleNight()">🌙 Ночной режим</button>
      <div class="note">🌙 держит Mac бодрым (caffeinate) + сам перезапускает бота, если упал/завис. Для фарма на ночь.</div>
      <label>🎯 Цели (ник в строке)</label><textarea id="targets" rows="10" spellcheck="false"></textarea>
      <button class="b-blue" onclick="saveTargets()">💾 Сохранить список</button>
      <div class="note" id="note"></div></div>`;
  } else {
    const fields=(m.fields||[]).map(fieldHTML).join('');
    const sels=(m.selects||[]).map(selectHTML).join('');
    const acts=(m.actions||[]).map(a=>{
      const cls=a.id==='run'?'b-green':a.id==='clear'?'b-red':'b-grey';
      return `<button class="${cls}" onclick="runOne('${mid}','${a.id}')">${a.label}</button>`;
    }).join('');
    side=`<div class="side">${fields}${sels}<div class="btns">${acts}
      <button class="b-red" onclick="post('${mid}','stop')">⏹ Стоп</button></div>
      <div class="note" id="note"></div></div>`;
  }
  main.innerHTML=`<p class="desc">${m.desc||''} <span id="mpill"></span></p>
    <div class="row"><div class="col-log"><pre class="log" id="log">…</pre></div>${side}</div>`;
  if(m.kind==='loop') loadTargets();
  pollMod(); timer=setInterval(pollMod,1500);
}
let nightOn=false;
async function pollMod(){
  try{ const r=await fetch('/api/'+active+'/status'); const d=await r.json();
    const mp=$('#mpill'); if(mp) mp.innerHTML=pill(d.state);
    const nb=$('#nightBtn'); if(nb){ nightOn=!!d.night;
      nb.className='b-night'+(nightOn?' on':'');
      nb.textContent=nightOn?'🌙 Ночной режим: ВКЛ':'🌙 Ночной режим'; }
    const log=$('#log'); if(log){ const bottom=log.scrollHeight-log.scrollTop-log.clientHeight<26;
      log.textContent=d.log||'(пусто)'; if(bottom) log.scrollTop=log.scrollHeight; }
  }catch(e){}
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
async function init(){
  CFG=await (await fetch('/api/config')).json();
  $('#tabs').innerHTML=CFG.map(m=>`<span class="tab" data-id="${m.id}" onclick="render('${m.id}')">${m.emoji} ${m.title}</span>`).join('');
  render(CFG[0].id);
}
init();
</script></body></html>
"""


LOGIN_PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🏰 Холоп — Вход</title>
<style>
 :root{--bg:#16171d;--panel:#1e2028;--ink:#e8e8ea;--mut:#8a8f98;--green:#2ecc71;
       --red:#e74c3c;--line:#2a2d36;--card:#0f1015;}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      background:var(--bg);color:var(--ink);
      font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
 .box{width:360px;max-width:92vw;background:var(--panel);border:1px solid var(--line);
      border-radius:16px;padding:26px 24px}
 h1{font-size:20px;margin:0 0 4px;font-weight:800}
 .sub{color:var(--mut);font-size:13px;margin:0 0 18px}
 label{display:block;color:var(--mut);font-size:12px;margin:12px 0 5px}
 input{width:100%;background:var(--card);color:var(--ink);border:1px solid var(--line);
       border-radius:9px;padding:11px;font:16px inherit}
 button{width:100%;margin-top:16px;font:700 15px inherit;border:0;border-radius:10px;
        padding:12px;cursor:pointer;color:#fff;background:var(--green)}
 button:hover{filter:brightness(1.08)} button:disabled{opacity:.5;cursor:default}
 .link{margin-top:12px;text-align:center;color:var(--mut);font-size:13px;cursor:pointer}
 .link:hover{color:var(--ink)}
 .note{color:var(--mut);font-size:12px;min-height:18px;margin-top:12px}
 .note.err{color:var(--red)} .note.ok{color:var(--green)}
 .safe{margin-top:18px;padding-top:14px;border-top:1px solid var(--line);
       color:var(--mut);font-size:12px;line-height:1.5}
 .hide{display:none}
</style></head><body>
<div class="box">
  <h1>🏰 Вход в Холоп</h1>
  <p class="sub">Войди своим Telegram — пульт будет работать на твоём аккаунте.</p>

  <div id="step-phone">
    <label>Номер телефона (как в Telegram)</label>
    <input id="phone" type="tel" placeholder="+79991234567" autocomplete="tel">
    <button id="btn-phone" onclick="sendCode()">Получить код</button>
  </div>

  <div id="step-code" class="hide">
    <label>Код из Telegram (придёт в приложение)</label>
    <input id="code" type="text" inputmode="numeric" placeholder="12345" autocomplete="one-time-code">
    <button id="btn-code" onclick="signIn()">Войти</button>
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
  const r=await fetch('/api/auth/'+path,{method:'POST',body:JSON.stringify(data||{})});
  return r.json();
}
async function sendCode(){
  const phone=$('phone').value.trim();
  if(!phone){note('Введи номер.','err');return;}
  $('btn-phone').disabled=true; note('Отправляю код…');
  const d=await api('send_code',{phone});
  $('btn-phone').disabled=false;
  if(!d.ok){note(d.err||'Ошибка','err');return;}
  note(''); show('step-code'); $('code').focus();
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
    print(f"🏰 Пульт Холопа: {url}\n(Окно можно свернуть/закрыть — на ботов не влияет.)")
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
