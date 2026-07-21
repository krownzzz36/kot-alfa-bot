#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOLOP REROLL — быстрый перегон холопа в нужную профессию (по умолчанию Воин).

Алгоритм (точно как в живой игре, проверено вручную 25.06.2026):
  0) задаёшь ник холопа
  1) открыть список «Мои холопы», найти холопа, убедиться что на нём НЕ стоит щит/охрана
  2) ВЫГНАТЬ холопа
  3) сразу: Холопы → Найти → Поиск по нику → отправить ник → результаты
  4) ЗАХВАТИТЬ его обратно (только за серебро 🪙, звёзды ⭐ никогда)
  5) прочитать профессию:
       • Воин  → поставить «Охрана (120🏅)» → СТОП ✅
       • не Воин → к шагу 2 (повтор)
  Повтор до max_iterations.

ПОЧЕМУ БОТ НЕ ТЕРЯЕТ ХОЛОПА (а руками — теряешь):
  После выгона у прежнего владельца есть ~30 секунд эксклюзива на обратный захват —
  в это окно никто другой холопа забрать не может. Бот делает «выгнать → захватить»
  за 1–2 секунды и всегда успевает. Руками это занимает минуту → холопа уводят.

Запуск:
    python holop_reroll.py "Яр"                 # боевой: гнать в Воина
    python holop_reroll.py "Яр" --dry-run       # ничего не жмёт, только показывает шаги
    python holop_reroll.py "Яр" --prof Ополченец
    python holop_reroll.py --list nicks.txt     # пакетно: список ников из файла

Конфиг: config.json (см. config.example.json). Первый запуск спросит телефон + код
из Telegram (и пароль 2FA, если есть) → создаст holop_session.session.

⚠️ Риски: автоматизация аккаунта формально против ToS Telegram; игра может банить за
ботоводство. Запускай редко, с паузами, не 24/7 — это твой осознанный риск.
"""

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
from datetime import datetime

from telethon import TelegramClient
from telethon.tl.custom import Message
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

HERE = os.path.dirname(os.path.abspath(__file__))


def _make_console_utf8():
    """Windows-консоль (cp1251/cp866) не умеет эмодзи и рушит вывод с UnicodeEncodeError.
    Переводим stdout/stderr в UTF-8 с заменой непечатаемого — процесс больше не падает."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_make_console_utf8()

# ════════════════════════════════════════════════════════════════════════════
#  ВСЕ ХРУПКИЕ СТРОКИ ИГРЫ — ПРАВЬ ТОЛЬКО ТУТ, ЕСЛИ ИГРА ОБНОВИТСЯ
#  (тексты кнопок и сообщений бота @holop, проверены вживую 25.06.2026)
# ════════════════════════════════════════════════════════════════════════════
UI = {
    # — команда и меню «Холопы» —
    "cmd_holopy":        "Холопы",          # текстовая команда открыть меню холопов
    "menu_marker":       "Холопов:",        # маркер в тексте меню «⛓️ Холопы»
    "btn_open_list":     "Холопы (",        # кнопка «Холопы (27)» — открыть список
    "btn_find":          "Найти",           # кнопка поиска холопов (внутри меню Холопы)

    # — список «Мои холопы» —
    "list_marker":       "Мои холопы",      # маркер экрана списка
    "block_sep":         "👤 ",             # начало блока одного холопа
    "page_re":           r"Страница\s*(\d+)/(\d+)",
    "prof_re":           r"🎭[^\n]*?([А-Яа-яЁё]+)",  # профессия в строке с 🎭
    "guard_marker":      "🛡",              # стоит рядом с именем = холоп под охраной

    # — кнопки в списке —
    "btn_guard":         "Охрана (120",     # поставить охрану (за золото 🏅) — НЕ звёзды
    "btn_kick":          "Выгнать",

    # — пагинация —
    "page_first":        "‹‹",
    "page_prev":         "‹",
    "page_next":         "›",
    "page_last":         "››",
    "btn_back":          "Назад",

    # — поиск холопов —
    "search_marker":     "Поиск холопов",   # экран «🔍 Поиск холопов»
    "btn_search_nick":   "Поиск по нику",
    "nick_prompt":       "Введи ник",       # «🔎 Поиск холопа — Введи ник или ID игрока»
    "results_marker":    "Результаты поиска",

    # — захват —
    "btn_capture":       "Захватить",       # кнопка подтверждения захвата (если экран есть)
    "captured_marker":   "своим холопом",   # «✅ Ты сделал X своим холопом!» — захват удался
    "btn_guard_after":   "Защитить за 120", # охрана прямо на экране после захвата

    # — ЗАПРЕЩЁННЫЕ маркеры (звёзды/платное — бот НИКОГДА не жмёт такие кнопки) —
    "star":              "⭐",
    "potion":            "🧪",              # «купи 🧪» = нужно зелье жаб (платно)
    "silver":            "🪙",              # серебро — разрешено

    # — стоп-статусы холопа в результатах поиска (нельзя захватить бесплатно) —
    "bad_status":        ("купи 🧪", "зелье жаб", "соклановец", "княжий щит", "кандал",
                          "охрана", "⛔", "недоступен", "нет слотов", "💣"),

    # — игровые кулдауны/ошибки в ответе бота —
    "cooldown_words":    ("подожд", "перезарядк", "кулдаун", "слишком часто", "ещё рано"),
}

# профессии: канон → эмодзи (для распознавания и логов)
PROFESSIONS = {
    "Воин": "⚔️", "Ополченец": "🛡️", "Волхв": "🧙", "Пахарь": "👨‍🌾",
    "Ремесленник": "🔨", "Зодчий": "📐", "Лазутчик": "🗡️",
}
PROF_KEYS = list(PROFESSIONS.keys())

# стоимость охраны в золоте (🏅) — нужно проверять баланс перед постановкой
GUARD_COST = 120

# тексты-«шумы», прилетающие в чат между ответами бота (НЕ ответ на наше действие)
NOISE = (
    "идёт осада", "Замок атакован", "ЗАМОК ОТКРЫТ", "Щит замка", "С возвращением",
    "АТАКА ОТБИТА", "АРЕНА БИТВ", "Отомстить", "Бочка заложена", "идёт штурм",
    "осада", "Набег", "заминир",
)

# невидимые символы-обёртки имени в кнопках результатов (⁨ник⁩)
INVIS = dict.fromkeys(map(ord, "⁦⁧⁨⁩‎‏"), None)


# ──────────────────────────────────────────────────────────────────────────
#  логирование: и в консоль, и в файл run.log
# ──────────────────────────────────────────────────────────────────────────
logger = logging.getLogger("holop")


def setup_logging():
    _make_console_utf8()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(os.path.join(HERE, "run.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
    logger.addHandler(fh)


def log(msg):
    logger.info(msg)


def load_config():
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        print("Нет config.json — скопируй config.example.json в config.json и впиши api_id/api_hash.")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ════════════════════════════════════════════════════════════════════════════
#  ПАРСЕРЫ (чистые функции — покрыты юнит-тестами в test_parsers.py)
# ════════════════════════════════════════════════════════════════════════════
def clean_name(s):
    """Убрать невидимые символы, эмодзи-щит и пробелы по краям."""
    return (s or "").translate(INVIS).replace(UI["guard_marker"], "").strip()


def parse_profession(block):
    """Вернуть канон профессии из текста (блок списка ИЛИ экран захвата), либо None.

    Берём ключ профессии, который встречается РАНЬШЕ всех в тексте. Это надёжно
    и для списка («🎭 ⚔️ Воин (+20%)»), и для экрана захвата
    («🎭 Профессия: ⚔️ Воин (...)») — слово «Профессия» профессией не является.
    """
    low = block.lower()
    best, best_pos = None, len(block) + 1
    for k in PROF_KEYS:
        i = low.find(k.lower())
        if 0 <= i < best_pos:
            best, best_pos = k, i
    return best


def parse_holop_block(text, nick):
    """Из текста списка «Мои холопы» вернуть (профессия, guarded) для nick, либо (None, None)."""
    for blk in text.split(UI["block_sep"])[1:]:
        first = blk.splitlines()[0] if blk.splitlines() else ""
        if clean_name(first) != nick.strip():
            continue
        guarded = UI["guard_marker"] in first
        return parse_profession(blk), guarded
    return None, None


def parse_pages(text):
    """Вернуть (текущая, всего) страниц или (1, 1)."""
    m = re.search(UI["page_re"], text or "")
    return (int(m.group(1)), int(m.group(2))) if m else (1, 1)


def parse_gold(text):
    """Вернуть баланс золота (🏅) из меню «Холопы»/«Территория», либо None.
    Формат в тексте: «🏅 Золото: 150» (иногда с K, напр. «1.2K»)."""
    m = re.search(r"Золото:\s*([\d.,]+)\s*([KkКк]?)", text or "")
    if not m:
        return None
    num = float(m.group(1).replace(",", "").replace(" ", ""))
    if m.group(2).lower() in ("k", "к"):
        num *= 1000
    return int(num)


def result_is_free(button_text):
    """Кнопка холопа в результатах поиска: можно ли захватить бесплатно (за серебро)?"""
    low = button_text.lower()
    if UI["star"] in button_text or UI["potion"] in button_text:
        return False
    return not any(bad.lower() in low for bad in UI["bad_status"])


def result_nick(button_text):
    """Извлечь имя из кнопки результата «⁨Ярск⁩ — захватить» (может быть обрезано «…»)."""
    name = button_text.translate(INVIS)
    for sep in (" — ", " · ", " - "):
        if sep in name:
            name = name.split(sep)[0]
            break
    return name.strip()


def result_name_matches(button_text, nick):
    """Совпадает ли кнопка результата с ником с учётом обрезки длинных ников («Земля Слав…»)."""
    raw = result_nick(button_text)
    if raw == nick:
        return True
    if raw.endswith("…") and nick.startswith(raw[:-1].strip()):
        return True
    return False


# ════════════════════════════════════════════════════════════════════════════
#  БОТ
# ════════════════════════════════════════════════════════════════════════════
class Reroller:
    def __init__(self, client, cfg, dry_run, check_only=False):
        self.c = client
        self.cfg = cfg
        self.bot = cfg.get("bot_username", "holop")
        self.dry = dry_run
        self.check_only = check_only
        self.lo = float(cfg.get("min_delay", 0.8))
        self.hi = float(cfg.get("max_delay", 1.8))
        # быстрые паузы на критичном участке выгон→захват (чтобы успеть в 30-сек окно)
        self.flo = float(cfg.get("fast_min_delay", 0.15))
        self.fhi = float(cfg.get("fast_max_delay", 0.4))
        self.allow_star = bool(cfg.get("allow_star_spend", False))
        # «два окна»: id сообщений, которые переиспользуем вместо переоткрытия меню
        self.list_id = None      # сообщение «Мои холопы» на странице холопа
        self.search_id = None    # сообщение поиска/результатов по нику
        self.capture_id = None   # экран «✅ … своим холопом!» (профессия + охрана)
        # статистика
        self.stats = {}

    async def pause(self, fast=False):
        lo, hi = (self.flo, self.fhi) if fast else (self.lo, self.hi)
        await asyncio.sleep(random.uniform(lo, hi))

    # ---------- низкоуровневые помощники ----------
    async def recent(self, limit=10):
        return await self.c.get_messages(self.bot, limit=limit)

    async def refetch(self, msg_id):
        return await self.c.get_messages(self.bot, ids=msg_id)

    async def send(self, text):
        return await self._flood(self.c.send_message(self.bot, text))

    async def _flood(self, coro):
        """Выполнить корутину, переживая FloodWaitError (ждём ровно сколько просят + рандом)."""
        while True:
            try:
                return await coro
            except FloodWaitError as e:
                wait = e.seconds + random.uniform(1, 3)
                log(f"  ⏳ FloodWait: Telegram просит подождать {e.seconds}с — сплю {wait:.0f}с")
                await asyncio.sleep(wait)

    def flat_buttons(self, msg: Message):
        out = []
        if msg and msg.buttons:
            for r, row in enumerate(msg.buttons):
                for col, b in enumerate(row):
                    out.append((r, col, (b.text or "")))
        return out

    async def wait_for(self, contains, min_id=0, tries=15, delay=0.5):
        """Дождаться свежего сообщения бота с подстрокой contains (шум отсекаем)."""
        for _ in range(tries):
            for m in await self.recent(12):
                if m.out:
                    continue
                t = m.message or ""
                if any(n in t for n in NOISE):
                    continue
                if contains and contains not in t:
                    continue
                if m.id < min_id:
                    continue
                if m.id == min_id and not contains:
                    continue
                return m
            await asyncio.sleep(delay)
        return None

    async def click_pos(self, msg: Message, r, col, *, label="", fast=False):
        if self.dry:
            log(f"  [dry] клик: {label or (r, col)}")
            return None
        res = await self._flood(msg.click(r, col))
        await self.pause(fast=fast)
        return res

    def has_star(self, text):
        return UI["star"] in (text or "") or UI["potion"] in (text or "")

    # ---------- список «Мои холопы» ----------
    async def open_menu(self):
        """Открыть меню «⛓️ Холопы». Вернуть сообщение-меню."""
        sent = await self.send(UI["cmd_holopy"])
        # «Холопы» может прийти как меню, так и сразу как список — берём любое
        m = await self.wait_for(UI["menu_marker"], min_id=sent.id, tries=8) \
            or await self.wait_for(UI["list_marker"], min_id=sent.id, tries=8)
        if not m:
            raise RuntimeError("Не дождался ответа на команду «Холопы»")
        return m

    async def open_list(self):
        """Открыть список «Мои холопы» (страница 1). Вернуть сообщение-список."""
        m = await self.open_menu()
        if UI["list_marker"] in (m.message or ""):
            return m  # уже список
        for r, col, t in self.flat_buttons(m):
            if UI["btn_open_list"] in t:
                await self.click_pos(m, r, col, label=t)
                break
        lst = await self.wait_for(UI["list_marker"], min_id=0, tries=12)
        if not lst:
            raise RuntimeError("Не открылся список «Мои холопы»")
        return lst

    async def find_on_pages(self, nick):
        """Пролистать список и найти страницу с nick. Вернуть (msg, prof, guarded)."""
        lst = await self.open_list()
        _, pages = parse_pages(lst.message or "")
        for _ in range(pages + 2):
            lst = await self.refetch(lst.id)
            prof, guarded = parse_holop_block(lst.message or "", nick)
            if prof is not None:
                return lst, prof, guarded
            # листаем вперёд; если вперёд некуда (последняя стр.) — пробуем назад
            moved = await self._page(lst, UI["page_next"]) or await self._page(lst, UI["page_prev"])
            if not moved:
                break
        return lst, None, None

    async def _page(self, msg, arrow):
        for r, col, t in self.flat_buttons(msg):
            if t.strip() == arrow:
                await self.click_pos(msg, r, col, label=f"страница {arrow}")
                return True
        return False

    def holop_buttons(self, msg: Message, nick):
        """Найти (row,col) кнопок Выгнать/Охрана для строки nick на текущей странице."""
        flat = self.flat_buttons(msg)
        name_idx = None
        for i, (r, col, t) in enumerate(flat):
            b = clean_name(t)
            if b == nick or (b.endswith("…") and nick.startswith(b[:-1])):
                name_idx = i
                break
        if name_idx is None:
            return None
        res = {}
        for j in range(name_idx + 1, len(flat)):           # Выгнать — после имени
            if UI["btn_kick"] in flat[j][2]:
                res["kick"] = flat[j][:2]
                break
        for j in range(name_idx - 1, -1, -1):              # Охрана — перед именем
            if UI["btn_guard"] in flat[j][2] and not self.has_star(flat[j][2]):
                res["guard"] = flat[j][:2]
                break
        return res

    # ---------- действия ----------
    async def refresh_list(self, nick):
        """Обновить окно списка (листнуть туда-обратно) и вернуть его с видимой строкой nick.
        Так после захвата холоп снова появляется в списке — без переоткрытия меню."""
        lst = await self.refetch(self.list_id) if self.list_id else None
        if lst is None or UI["list_marker"] not in (lst.message or ""):
            lst, _, _ = await self.find_on_pages(nick)
            self.list_id = lst.id
        # листнём туда-обратно, чтобы список перерисовался и холоп вернулся в строку
        cur, total = parse_pages(lst.message or "")
        if total > 1:
            a, b = (UI["page_next"], UI["page_prev"]) if cur < total else (UI["page_prev"], UI["page_next"])
            if await self._page(lst, a):
                lst = await self.refetch(lst.id)
                await self._page(lst, b)
                lst = await self.refetch(lst.id)
        if not self.holop_buttons(lst, nick):       # не на этой странице — найдём заново
            lst, _, _ = await self.find_on_pages(nick)
        self.list_id = lst.id
        return lst

    async def kick(self, nick):
        lst = await self.refresh_list(nick)
        btns = self.holop_buttons(lst, nick)
        if not btns or "kick" not in btns:
            raise RuntimeError(f"Не нашёл кнопку «Выгнать» у {nick} (возможно, увели или под охраной)")
        r, col = btns["kick"]
        await self.click_pos(lst, r, col, label=f"Выгнать {nick}", fast=True)
        log(f"  выгнал {nick}")

    async def _press(self, msg, text_substr):
        """Нажать на кнопку сообщения по подстроке текста. Вернуть True, если нашли."""
        for r, col, t in self.flat_buttons(msg):
            if text_substr in t:
                await self.click_pos(msg, r, col, label=t, fast=True)
                return True
        return False

    async def open_nick_search_full(self):
        """Полное открытие «🔎 Поиск холопа» через меню (запасной путь)."""
        menu = await self.open_menu()
        src = menu
        if UI["list_marker"] in (menu.message or ""):
            await self._press(menu, UI["btn_back"])
            src = await self.wait_for(UI["menu_marker"], min_id=0, tries=8) or menu
        await self._press(src, UI["btn_find"])
        search = await self.wait_for(UI["search_marker"], min_id=0, tries=10)
        if not search:
            raise RuntimeError("Не открылся «Поиск холопов»")
        self.search_id = search.id
        await self._press(search, UI["btn_search_nick"])
        prompt = await self.wait_for(UI["nick_prompt"], min_id=search.id, tries=8)
        if not prompt:
            raise RuntimeError("Нет приглашения ввести ник")
        return prompt

    async def goto_nick_prompt(self):
        """Дойти до приглашения ввести ник БЫСТРО, переиспользуя окно поиска.
        С экрана захвата возвращаемся «Назад» → результаты → «Поиск по нику»."""
        msg = await self.refetch(self.search_id) if self.search_id else None
        if msg:
            has_btn = any(UI["btn_search_nick"] in t for _, _, t in self.flat_buttons(msg))
            if not has_btn and UI["captured_marker"] in (msg.message or ""):
                # это экран захвата — жмём «Назад», попадаем в результаты поиска
                if await self._press(msg, UI["btn_back"]):
                    msg = await self.wait_for(UI["results_marker"], tries=6) \
                          or await self.wait_for(UI["search_marker"], tries=4)
                    if msg:
                        self.search_id = msg.id
            if msg and any(UI["btn_search_nick"] in t for _, _, t in self.flat_buttons(msg)):
                await self._press(msg, UI["btn_search_nick"])
                prompt = await self.wait_for(UI["nick_prompt"], tries=8)
                if prompt:
                    return prompt
        return await self.open_nick_search_full()

    async def _open_results(self, nick, via_back):
        """Получить сообщение результатов поиска по нику.
        via_back=True: вернуться «Назад» с экрана захвата → холоп снова доступен (БЕЗ нового поиска).
        Иначе (или если «Назад» не сработал) — полный поиск по нику."""
        if via_back and self.capture_id:
            scr = await self.refetch(self.capture_id)
            if scr and await self._press(scr, UI["btn_back"]):
                res = await self.wait_for(UI["results_marker"], tries=8)
                if res:
                    self.search_id = res.id
                    return res
        await self.goto_nick_prompt()
        s = await self.send(nick)
        res = await self.wait_for(UI["results_marker"], min_id=s.id, tries=12)
        if res:
            self.search_id = res.id
        return res

    async def capture(self, nick, via_back=False):
        """Захватить nick за серебро. Вернуть ВЫПАВШУЮ ПРОФЕССИЮ (строку) или None.

        Профессию читаем прямо с экрана «✅ … своим холопом! 🎭 Профессия: …».
        via_back=True → повторный захват через «Назад» в окне результатов, без нового поиска."""
        res = await self._open_results(nick, via_back)
        if not res:
            log("  ⚠️ нет результатов поиска")
            return None

        # кнопка именно нашего ника (может быть обрезан «…»), свободного, за серебро (не ⭐)
        target = None
        for r, col, t in self.flat_buttons(res):
            if not result_name_matches(t, nick):
                continue
            if not result_is_free(t):
                log(f"  ⛔ {nick} недоступен бесплатно: «{t}» — пропускаю")
                continue
            target = (r, col, t)
            break
        if not target:
            log(f"  ⚠️ {nick} не найден среди свободных за серебро")
            return None

        r, col, t = target
        await self.click_pos(res, r, col, label=f"захватываю {nick}", fast=True)
        scr = await self.wait_for("", min_id=res.id, tries=10)
        scr = await self.refetch(scr.id) if scr else None
        if not scr:
            return None
        txt = scr.message or ""

        # редкий промежуточный экран подтверждения захвата
        if UI["captured_marker"] not in txt:
            for r2, col2, bt in self.flat_buttons(scr):
                if UI["btn_capture"] in bt:
                    if self.has_star(bt) or (not self.allow_star and self.has_star(txt) and UI["silver"] not in bt):
                        log("  ⛔ захват требует звёзд ⭐ — стоп (звёзды не трачу)")
                        return None
                    await self.click_pos(scr, r2, col2, label=f"Захватить {nick}", fast=True)
                    scr2 = await self.wait_for(UI["captured_marker"], min_id=scr.id, tries=8)
                    scr = await self.refetch(scr2.id) if scr2 else scr
                    txt = scr.message or ""
                    break

        if UI["captured_marker"] in (scr.message or ""):
            self.capture_id = scr.id
            prof = parse_profession(scr.message or "")
            log(f"  захватил {nick} → {prof} {PROFESSIONS.get(prof, '')}")
            return prof

        # экран успеха не распознали — проверим списком, вернулся ли холоп
        self.capture_id = None
        lst, prof2, _ = await self.find_on_pages(nick)
        if prof2:
            self.list_id = lst.id
            log(f"  захватил {nick} (профессия из списка) → {prof2}")
            return prof2
        return None

    async def _guard_confirmed(self, nick):
        """Проверить по списку, что на холопе теперь реально стоит охрана (🛡)."""
        await self.pause()
        _, _, guarded = await self.find_on_pages(nick)
        return bool(guarded)

    async def ensure_gold(self, need):
        """Вернуть текущее золото; если меньше need — собрать доход холопов и перечитать."""
        try:
            menu = await self.open_menu()
        except RuntimeError:
            return None
        gold = parse_gold(menu.message or "")
        if gold is not None and gold < need:
            for r, c, t in self.flat_buttons(menu):
                if "Собрать" in t and "золот" in t:
                    await self.click_pos(menu, r, c, label=t)
                    menu = await self.refetch(menu.id) or menu
                    g2 = parse_gold(menu.message or "")
                    if g2 is not None:
                        gold = g2
                    log(f"  собрал доход холопов → {gold}🏅")
                    break
        return gold

    async def guard(self, nick):
        """Поставить охрану на Воина. Сначала кнопка прямо на экране захвата
        («Защитить за 120 золота»), потом — «Охрана (120🏅)» из списка.
        Первые ~30–60с после захвата охрана может быть недоступна — ретраим и ПРОВЕРЯЕМ."""
        # охрана стоит 120🏅 — проверим золото, при нехватке соберём доход холопов
        gold = await self.ensure_gold(GUARD_COST)
        if gold is not None and gold < GUARD_COST:
            log(f"  ❌ НЕ ХВАТАЕТ ЗОЛОТА на охрану: есть {gold}🏅, нужно {GUARD_COST}🏅. "
                f"{nick} уже ВОИН — собери золото и поставь охрану ВРУЧНУЮ (иначе уведут!).")
            return False
        for attempt in range(8):
            # 1) кнопка прямо на экране захвата (самый быстрый путь)
            if self.capture_id:
                scr = await self.refetch(self.capture_id)
                if scr and await self._press(scr, UI["btn_guard_after"]):
                    if await self._guard_confirmed(nick):
                        log(f"  ✅ поставил охрану на {nick} (120 золота)")
                        return True
            # 2) кнопка «Охрана (120🏅)» в списке
            lst = await self.refresh_list(nick)
            btns = self.holop_buttons(lst, nick)
            if btns and "guard" in btns:
                r, col = btns["guard"]
                await self.click_pos(lst, r, col, label=f"Охрана на {nick}")
                if await self._guard_confirmed(nick):
                    log(f"  ✅ поставил охрану на {nick} (120 золота)")
                    return True
            log(f"  охрана пока недоступна (попытка {attempt+1}/8) — жду 8с")
            await asyncio.sleep(8)
        log(f"  ⚠️ НЕ удалось поставить охрану на {nick} — ПОСТАВЬ ВРУЧНУЮ СРОЧНО, иначе уведут!")
        return False

    # ---------- цикл одного холопа ----------
    async def process(self, nick, target):
        nick = nick.strip()
        max_it = int(self.cfg.get("max_iterations", 40))
        self.stats[nick] = {"rerolls": 0, "final": None, "guarded": False, "drops": {}}
        self.list_id = self.search_id = self.capture_id = None
        log(f"═══ {nick} → {target} (макс. {max_it} рероллов, dry_run={self.dry}) ═══")

        lst, prof, guarded = await self.find_on_pages(nick)
        self.list_id = lst.id if prof is not None else None
        if prof is None:
            if self.check_only:
                log(f"  {nick} не найден в твоих холопах.")
                self.stats[nick]["final"] = "не найден"
                return
            log(f"  {nick} не в твоих холопах (возможно увели). Пробую захватить…")
            prof = await self.capture(nick)
            if not prof:
                log(f"  не смог вернуть {nick}. Стоп.")
                self.stats[nick]["final"] = "не найден"
                return
            guarded = False
        log(f"  текущая профессия: {prof} (под охраной: {guarded})")
        if self.check_only:
            self.stats[nick]["final"] = prof + (" 🛡 под охраной" if guarded else "")
            return

        for it in range(max_it + 1):
            self.stats[nick]["drops"][prof] = self.stats[nick]["drops"].get(prof, 0) + 1

            # 🎯 ВЫПАЛ ВОИН (цель) → сразу охрана и СТОП. Больше ничего не делаем.
            if prof == target:
                log(f"  🎯 {nick} — {target}! Ставлю охрану и останавливаюсь.")
                self.stats[nick]["final"] = prof
                self.stats[nick]["guarded"] = guarded or await self.guard(nick)
                return

            # ⛔ под охраной выгонять НЕЛЬЗЯ — не вернём холопа.
            if guarded:
                log(f"  ⛔ {nick} под охраной (🛡) — выгонять нельзя. Сними охрану и запусти снова. Стоп.")
                self.stats[nick]["final"] = "под охраной — не трогаю"
                return

            # реролл: выгнать → захватить → профессию читаем С ЭКРАНА ЗАХВАТА
            self.stats[nick]["rerolls"] += 1
            log(f"  — реролл {self.stats[nick]['rerolls']}: было {prof}, не {target}")
            await self.kick(nick)
            prof = None
            for attempt in range(3):
                # повторный захват — через «Назад» в окне результатов (без нового поиска);
                # на ретраях падаем на полный поиск.
                via_back = (self.capture_id is not None and attempt == 0)
                prof = await self.capture(nick, via_back=via_back)
                if prof:
                    break
                log(f"  повтор захвата {nick} (попытка {attempt+1}/3)")
            if not prof:
                log(f"  ⚠️ не удалось вернуть {nick} после выгона. Стоп — проверь вручную.")
                self.stats[nick]["final"] = "потерян при рероле"
                return
            guarded = False   # свежезахваченный холоп — без охраны

        log(f"  достигнут лимит рероллов ({max_it}) — {target} не выпал.")
        self.stats[nick]["final"] = f"лимит ({prof})"

    async def run(self, nicks, target):
        session_budget = int(self.cfg.get("max_captures_per_session", 0))  # 0 = без лимита
        started = datetime.now()
        captured = 0
        for nick in nicks:
            await self.process(nick, target)
            captured += self.stats[nick]["rerolls"]
            if session_budget and captured >= session_budget:
                log(f"⛔ достигнут лимит захватов за сессию ({session_budget}). Останавливаюсь.")
                break
            if nick != nicks[-1]:
                await self.pause()
        self.report(target, started)

    def report(self, target, started=None):
        log("════════════════ ИТОГ ════════════════")
        for nick, s in self.stats.items():
            mark = "✅" if s["final"] == target else "❌"
            guard = "охрана ✔" if s["guarded"] else "без охраны"
            drops = ", ".join(f"{k}×{v}" for k, v in s["drops"].items())
            log(f"{mark} {nick}: рероллов {s['rerolls']}, итог «{s['final']}», {guard}")
            if drops:
                log(f"     выпадения: {drops}")
        if started is not None:
            n = len(self.stats)
            ok = sum(1 for s in self.stats.values() if s["final"] == target)
            total = sum(s["rerolls"] for s in self.stats.values())
            secs = int((datetime.now() - started).total_seconds())
            log(f"── Готово: {ok}/{n} холопов в «{target}», всего рероллов {total}, "
                f"время {secs // 60}м {secs % 60}с. Список пройден — останавливаюсь. ✅")


# ──────────────────────────────────────────────────────────────────────────
async def main():
    ap = argparse.ArgumentParser(description="Перегон холопа в нужную профессию (@holop)")
    ap.add_argument("nick", nargs="?", help="Ник холопа, например: Яр")
    ap.add_argument("--list", dest="listfile", help="Файл со списком ников (по одному в строке)")
    ap.add_argument("--prof", default=None, help="Целевая профессия (по умолчанию из конфига: Воин)")
    ap.add_argument("--dry-run", action="store_true", help="Не жать кнопки, только показать шаги")
    ap.add_argument("--check", action="store_true", help="Только показать профессию/охрану холопа, ничего не делать")
    args = ap.parse_args()

    nicks = []
    if args.listfile:
        with open(args.listfile, encoding="utf-8") as f:
            nicks = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    elif args.nick:
        nicks = [args.nick]
    if not nicks:
        print("Укажи ник холопа или --list файл. Пример: python holop_reroll.py \"Яр\"")
        sys.exit(1)

    setup_logging()
    cfg = load_config()
    target = args.prof or cfg.get("target_profession", "Воин")
    dry = args.dry_run or bool(cfg.get("dry_run", False))
    if target not in PROF_KEYS:
        log(f"Неизвестная профессия «{target}». Доступно: {', '.join(PROF_KEYS)}")
        sys.exit(1)
    if not cfg.get("api_id") or not cfg.get("api_hash") or "ВСТАВЬ" in str(cfg.get("api_hash")):
        log("Заполни api_id и api_hash в config.json (https://my.telegram.org → API development tools).")
        sys.exit(1)

    # сессия: либо готовая строка-сессия (без логина), либо файл .session (спросит код)
    if cfg.get("session_string"):
        client = TelegramClient(StringSession(cfg["session_string"]), int(cfg["api_id"]), cfg["api_hash"])
    else:
        session = os.path.join(HERE, cfg.get("session_name", "holop_session"))
        client = TelegramClient(session, int(cfg["api_id"]), cfg["api_hash"])
    await client.start()
    me = await client.get_me()
    log(f"Вошёл как: {me.first_name} (@{me.username}).  Цель: {', '.join(nicks)} → {target}")

    rr = Reroller(client, cfg, dry, check_only=args.check)
    try:
        await rr.run(nicks, target)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
