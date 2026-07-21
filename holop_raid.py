#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOLOP RAID — автобот набегов по самым ЖИРНЫМ соперникам (@holop).

Тактика (снято вживую с игры 28.06.2026):
  ФАЗА 1 — разведка богатых:
    Магазин → Расходуемые ресурсы → «ПОДЛОЖИТЬ БОЧКУ ПОРОХА».
    ⚠️ БОЧКУ НЕ ЗАКЛАДЫВАЕМ. Экран нужен только ради сортировки по серебру.
    Жмём «Серебро ↓», листаем N страниц, выписываем всех с 🪙 серебром
    и уровнем ≥ 10 (ниже 10 бить нельзя — снимут репутацию).

  ФАЗА 2 — пробивка (через «Набеги» = ⚔️ АРЕНА БИТВ):
    На арене сверху показана МОЯ эффективная атака (⚔️ Атака: X→ЭФФ),
    а у каждой цели — её эффективная защита (🛡️ X→ЭФФ) и кнопка-статус:
      • «Атаковать <Имя>»  — можно бить;
      • «<Имя> • Свой клан» / «<Имя> • Закрыто» — нельзя (соклан / щит / граница).
    Для каждой богатой цели делаем Поиск по названию, читаем защиту и статус.
    Оставляем тех, кого можно ударить и у кого  моя_атака ≥ защита × margin
    (по умолчанию margin = 1.1, т.е. атака минимум на 10% выше защиты).

  ФАЗА 3 — набеги по КД:
    Покупаем обоз (Расходники → 🐴 Обоз → «+50% · 50м») — бонус серебра с набегов.
    Бьём цели из итогового списка: Поиск → «Атаковать». КД на ЦЕЛЬ ~5 минут,
    поэтому крутим список по кругу, добивая тех, у кого КД истёк, пока жив обоз.

Жёсткие предохранители (как «звёзды не трогать» в реролле):
  • НИКОГДА не жмём кнопку-имя на экране бочки (это закладка бочки).
  • НИКОГДА не бьём уровень < min_level (по умолчанию 10).
  • НИКОГДА не жмём кнопки со ⭐ (платные). Обоз берём только за 🪙 серебро.
  • --dry-run / --scan-only: только читаем и строим список, ничего не жмём боевого.

Запуск:
    python3.11 holop_raid.py                 # полный цикл: разведка → обоз → набеги
    python3.11 holop_raid.py --scan-only     # только собрать и показать список целей
    python3.11 holop_raid.py --dry-run       # пройтись по экранам, ничего не бить
    python3.11 holop_raid.py --pages 5       # сколько страниц бочки сканировать (деф. 5)
    python3.11 holop_raid.py --margin 1.1    # порог: моя атака ≥ защита × margin
    python3.11 holop_raid.py --min-level 10  # минимальный уровень цели (деф. 10)
    python3.11 holop_raid.py --duration 50   # сколько минут крутить набеги (деф. 50)
    python3.11 holop_raid.py --no-oboz       # не покупать обоз

Конфиг и сессия — общие с holop_reroll.py (config.json, holop_session).

⚠️ Тот же риск, что и у реролла: автоматизация против ToS. Запускай осознанно.
"""

import argparse
import asyncio
import random
import re
import sys
import time
from datetime import datetime

from telethon import TelegramClient
from telethon.tl.custom import Message
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

# переиспользуем проверенный фундамент из основного скрипта
from holop_reroll import HERE, load_config, setup_logging, log, INVIS

# ════════════════════════════════════════════════════════════════════════════
#  ХРУПКИЕ СТРОКИ ИГРЫ — правь ТОЛЬКО тут, если @holop обновит интерфейс.
#  (тексты кнопок/сообщений сняты вживую 28.06.2026)
# ════════════════════════════════════════════════════════════════════════════
UI = {
    # — арена / набеги —
    "cmd_arena":        "Набеги",            # текстовая команда открыть арену
    "arena_marker":     "АРЕНА БИТВ",        # маркер экрана арены
    "arena_my_atk_re":  r"Атака:\s*\d+\s*→\s*(\d+)",   # моя эфф. атака в шапке
    "arena_my_hp_re":   r"Жизни:\s*(\d+)\s*/\s*100",   # мои HP в шапке арены
    "targets_anchor":   "Цели",              # после этого слова идут блоки целей
    "block_sep":        "🏙️",               # начало блока одной цели
    "tgt_hp_re":        r"(\d+)\s*/\s*100",
    "tgt_lvl_re":       r"Ур\.\s*(\d+)",
    "tgt_atk_re":       r"⚔️\s*\d+\s*→\s*(\d+)",
    "tgt_def_re":       r"🛡️\s*\d+\s*→\s*(\d+)",
    "btn_attack":       "Атаковать",         # кнопка цели, которую можно бить
    "btn_search":       "Поиск",             # поиск территории по названию
    "search_prompt":    "Введи название",    # «🔍 Поиск территории — Введи название…»
    "btn_sort_def_up":  "Защита ▲",          # сортировка по защите по возрастанию
    "blocked_words":    ("свой клан", "закрыт", "щит", "недоступ", "граница", "⛔"),

    # — магазин / расходники / бочка —
    "cmd_shop":         "Магазин",
    "btn_consumables":  "Расходуемые ресурсы",
    "btn_barrel_open":  "ПОДЛОЖИТЬ БОЧКУ",   # открыть список целей (НЕ закладка)
    "barrel_marker":    "ПОДЛОЖИТЬ БОЧКУ ПОРОХА",
    "btn_sort_silver":  "Серебро ↓",         # сортировка богатых сверху
    "brl_lvl_re":       r"Ур\.\s*(\d+)",
    "brl_silver_re":    r"🪙\s*([\d ]+)\s*серебра",
    "page_re":          r"стр\.\s*(\d+)\s*/\s*(\d+)",
    "btn_next":         "›",                  # следующая страница
    "btn_back":         "Назад",

    # — обоз —
    "btn_oboz_open":    "Обоз",               # «🐴 Обоз (% серебра набег) ►»
    "oboz_pick":        ("+50%", "50м"),      # какую кнопку обоза жать (substr И substr)

    # — запрещённое (платное) —
    "star":             "⭐",

    # — итоги атаки (форматы сняты вживую 28.06.2026) —
    # ПОБЕДА: «🏴 ВОТЧИНА ПАЛА! … растоптаны! 🪙 Контрибуция: N серебра … +N монет»
    "win_words":        ("вотчина пала", "контрибуц", "растоптан", "сорваны и", "победа",
                         "награблено", "захвачено"),
    "loss_words":       ("отбит", "провалил", "неудач", "устоял", "отброшен"),
    "cooldown_words":   ("перезарядк", "откат", "подожд", "рано", "кулдаун", "ещё рано",
                         "недавно нападал", "уже нападал"),
    # лут: сначала «Контрибуция: N», иначе «+N монет/серебра/🪙»
    "loot_re":          r"Контрибуц[а-яё]*:?\s*([\d ]+)",
    "loot_re2":         r"\+\s*([\d ]+)\s*(?:монет|серебра|🪙)",
}

# шум — посторонние пуш-сообщения бота (НЕ ответ на наше действие).
# ВАЖНО: сюда НЕЛЬЗЯ класть слова из итогов атаки («Набег», «отбит» и т.п.) —
# иначе потеряем результат собственного набега.
NOISE = (
    "идёт осада", "Замок атакован", "ЗАМОК ОТКРЫТ", "Щит замка", "С возвращением",
    "идёт штурм", "заминир",
)


# ════════════════════════════════════════════════════════════════════════════
#  ПАРСЕРЫ (чистые функции)
# ════════════════════════════════════════════════════════════════════════════
def strip_decor(s):
    """Убрать невидимые символы и обрезать пробелы (эмодзи оставляем как есть)."""
    return (s or "").translate(INVIS).strip()


def norm(s):
    """Нормализовать имя для сравнения: убрать эмодзи/пунктуацию/регистр/пробелы."""
    s = strip_decor(s).lower()
    return re.sub(r"[^0-9a-zа-яё]+", "", s)


def parse_pages(text):
    m = re.search(UI["page_re"], text or "")
    return (int(m.group(1)), int(m.group(2))) if m else (1, 1)


def parse_my_attack(text):
    """Моя эффективная атака из шапки арены, либо None."""
    m = re.search(UI["arena_my_atk_re"], text or "")
    return int(m.group(1)) if m else None


def parse_my_hp(text):
    """Мои HP (X из X/100) из шапки арены, либо None (на экране поиска шапки нет)."""
    m = re.search(UI["arena_my_hp_re"], text or "")
    return int(m.group(1)) if m else None


def _targets_region(text):
    """Часть текста арены ПОСЛЕ слова «Цели» — чтобы не парсить свой блок как цель."""
    i = (text or "").find(UI["targets_anchor"])
    return text[i:] if i >= 0 else (text or "")


def parse_arena_targets(text):
    """Список целей арены: [{name, hp, level, atk, defense}] (defense — эффективная)."""
    out = []
    region = _targets_region(text)
    for chunk in region.split(UI["block_sep"])[1:]:
        first = chunk.splitlines()[0] if chunk.splitlines() else ""
        name = strip_decor(first)
        lvl = re.search(UI["tgt_lvl_re"], chunk)
        df = re.search(UI["tgt_def_re"], chunk)
        if not name or not df:
            continue
        atk = re.search(UI["tgt_atk_re"], chunk)
        hp = re.search(UI["tgt_hp_re"], chunk)
        out.append({
            "name": name,
            "hp": int(hp.group(1)) if hp else None,
            "level": int(lvl.group(1)) if lvl else None,
            "atk": int(atk.group(1)) if atk else None,
            "defense": int(df.group(1)),
        })
    return out


def parse_barrel_targets(text):
    """Список целей с экрана бочки: [{name, level, silver}]."""
    out = []
    for chunk in (text or "").split(UI["block_sep"])[1:]:
        lines = chunk.splitlines()
        if not lines:
            continue
        name = strip_decor(lines[0])
        lvl = re.search(UI["brl_lvl_re"], chunk)
        sv = re.search(UI["brl_silver_re"], chunk)
        if not name or not sv:
            continue
        silver = int(sv.group(1).replace(" ", ""))
        out.append({
            "name": name,
            "level": int(lvl.group(1)) if lvl else None,
            "silver": silver,
        })
    return out


_CTRL_BTN_RE = re.compile(r"[▲▼►◄]|\d+\s*/\s*\d+|^[‹›«»]+$")
_CTRL_BTN_WORDS = ("поиск", "история", "сбросить", "назад", "уровень", "атака", "защита",
                   "серебро", "сортиров")


def _is_control_btn(text):
    low = (text or "").strip().lower()
    if _CTRL_BTN_RE.search(text or ""):
        return True
    return any(w in low for w in _CTRL_BTN_WORDS) and "атаковать" not in low


def target_buttons(buttons):
    """Кнопки целей идут в начале клавиатуры, по одной на блок цели (в том же порядке).
    Берём их подряд от начала, пока не наткнёмся на управляющую (сортировки/пагинация)."""
    out = []
    for _, _, t in buttons:
        if _is_control_btn(t):
            break
        out.append(t)
    return out


def exact_target(blocks, name):
    """Индекс блока с ТОЧНЫМ (нормализованным) совпадением имени, иначе None."""
    want = norm(name)
    for i, b in enumerate(blocks):
        if norm(b["name"]) == want:
            return i
    return None


def button_attackable(btn_text):
    """Кнопка цели разрешает удар? («Атаковать …» — да; «… • Свой клан/Закрыто» — нет)."""
    return (btn_text or "").strip().lower().startswith(UI["btn_attack"].lower())


def classify_result(text):
    """Грубо классифицировать ответ бота на набег: win/loss/blocked/cooldown/unknown + лут."""
    low = (text or "").lower()
    loot = 0
    m = re.search(UI["loot_re"], text or "") or re.search(UI["loot_re2"], text or "")
    if m:
        loot = int(m.group(1).replace(" ", ""))
    if any(w in low for w in UI["cooldown_words"]):
        return "cooldown", loot
    if any(w in low for w in UI["win_words"]) or loot > 0:
        return "win", loot
    # поражение проверяем РАНЬШЕ блока: «провалилась» содержит подстроку «ров»,
    # поэтому короткие слова блока матчим по границе слова, а не как попало.
    if any(w in low for w in UI["loss_words"]):
        return "loss", loot
    if re.search(r"частокол|ополчен|защищ|\bров\b|\bщит\b", low):
        return "blocked", loot
    return "unknown", loot


# ════════════════════════════════════════════════════════════════════════════
#  БОТ-НАБЕЖНИК
# ════════════════════════════════════════════════════════════════════════════
class Raider:
    def __init__(self, client, cfg, args):
        self.c = client
        self.cfg = cfg
        self.bot = cfg.get("bot_username", "holop")
        self.dry = args.dry_run
        self.scan_only = args.scan_only
        self.attack = getattr(args, "attack", False)
        self.pages = args.pages
        self.margin = args.margin
        self.min_level = args.min_level
        self.duration = args.duration
        self.kd = args.kd
        self.want = args.want
        self.min_hp = args.min_hp
        self.max_rounds = args.max_rounds
        self.buy_oboz = not args.no_oboz
        self.lo = float(cfg.get("min_delay", 0.8))
        self.hi = float(cfg.get("max_delay", 1.8))
        self.my_attack = None
        self.stats = {"hits": 0, "wins": 0, "blocked": 0, "loss": 0, "loot": 0, "skipped": 0}

    # ---------- низкоуровневые помощники (как в реролле) ----------
    async def pause(self):
        await asyncio.sleep(random.uniform(self.lo, self.hi))

    async def recent(self, limit=12):
        return await self.c.get_messages(self.bot, limit=limit)

    async def refetch(self, msg_id):
        return await self.c.get_messages(self.bot, ids=msg_id)

    async def _flood(self, coro):
        while True:
            try:
                return await coro
            except FloodWaitError as e:
                wait = e.seconds + random.uniform(1, 3)
                log(f"  ⏳ FloodWait: ждём {wait:.0f}с")
                await asyncio.sleep(wait)

    async def send(self, text):
        return await self._flood(self.c.send_message(self.bot, text))

    def flat_buttons(self, msg: Message):
        out = []
        if msg and msg.buttons:
            for r, row in enumerate(msg.buttons):
                for col, b in enumerate(row):
                    out.append((r, col, (b.text or "")))
        return out

    async def wait_for(self, contains, min_id=0, tries=15, delay=0.5):
        """Дождаться свежего сообщения бота, содержащего contains (шум отсекаем)."""
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

    async def wait_result(self, min_id, tries=12, delay=0.6):
        """Дождаться НОВОГО ответа на набег (итог боя). Шум по NOISE отсекаем,
        но слова боя НЕ трогаем — поэтому ловим первый свежий не-наш месседж."""
        for _ in range(tries):
            msgs = sorted(await self.recent(12), key=lambda m: m.id, reverse=True)
            for m in msgs:
                if m.out or m.id <= min_id:
                    continue
                t = m.message or ""
                if any(n in t for n in NOISE):
                    continue
                return m
            await asyncio.sleep(delay)
        return None

    async def wait_button(self, msg_id, substr, tries=10, delay=0.5):
        """Дождаться, пока в сообщении msg_id (его бот редактирует на месте)
        появится КНОПКА с подстрокой substr. Вернуть свежее сообщение или None."""
        for _ in range(tries):
            m = await self.refetch(msg_id)
            if any(substr.lower() in (t or "").lower() for _, _, t in self.flat_buttons(m)):
                return m
            await asyncio.sleep(delay)
        return None

    async def newest_bot_msg(self, min_id, tries=10, delay=0.5):
        """Вернуть самое свежее НЕ-наше сообщение бота с id > min_id (шум отсекаем)."""
        for _ in range(tries):
            for m in sorted(await self.recent(8), key=lambda x: x.id, reverse=True):
                if m.out or m.id <= min_id:
                    continue
                if any(n in (m.message or "") for n in NOISE):
                    continue
                return m
            await asyncio.sleep(delay)
        return None

    async def click(self, msg: Message, r, col, *, label=""):
        if self.dry:
            log(f"  [dry] клик: {label or (r, col)}")
            return None
        res = await self._flood(msg.click(r, col))
        await self.pause()
        return res

    async def click_text(self, msg: Message, substr, *, label="", forbid_star=True):
        """Кликнуть первую кнопку, содержащую substr. Вернуть True/False.
        Платные (⭐) кнопки не жмём никогда."""
        for r, col, t in self.flat_buttons(msg):
            if substr.lower() in (t or "").lower():
                if forbid_star and UI["star"] in t:
                    log(f"  ⛔ пропускаю платную кнопку «{t}»")
                    return False
                await self.click(msg, r, col, label=label or t)
                return True
        return False

    # ---------- навигация в магазин/расходники ----------
    async def open_consumables(self):
        """Открыть «Расходуемые ресурсы». Вернуть сообщение этого экрана.
        Магазин редактирует ОДНО сообщение на месте — поэтому навигируем по кнопкам
        и каждый раз перечитываем то же сообщение."""
        sent = await self.send(UI["cmd_shop"])
        shop = await self.newest_bot_msg(sent.id, tries=12)
        if not shop:
            raise RuntimeError("Магазин не открылся")
        # уже на расходниках? (есть кнопка бочки) — вернуть как есть
        if any(UI["btn_barrel_open"].lower() in (t or "").lower()
               for _, _, t in self.flat_buttons(shop)):
            return shop
        # иначе на корне — жмём категорию «Расходуемые ресурсы»
        if not await self.click_text(shop, UI["btn_consumables"], label="Расходуемые ресурсы"):
            # вдруг «Магазин» открыл подменю — дойдём до корня кнопкой «В магазин»
            await self.click_text(shop, "В магазин")
            shop = await self.refetch(shop.id)
            if not await self.click_text(shop, UI["btn_consumables"], label="Расходуемые ресурсы"):
                raise RuntimeError("Не нашёл кнопку «Расходуемые ресурсы»")
        con = await self.wait_button(shop.id, UI["btn_barrel_open"], tries=12)
        if not con:
            raise RuntimeError("Экран расходников не открылся")
        return con

    # ---------- ФАЗА 1: разведка богатых через экран бочки ----------
    async def scan_rich(self):
        """Пройти страницы экрана бочки (сорт по серебру) и собрать богатых ур.≥min_level."""
        con = await self.open_consumables()
        if not await self.click_text(con, UI["btn_barrel_open"], label="ПОДЛОЖИТЬ БОЧКУ"):
            raise RuntimeError("Не нашёл кнопку открытия списка бочки")
        brl = await self.wait_for(UI["barrel_marker"], min_id=0, tries=12)
        if not brl:
            raise RuntimeError("Экран бочки не открылся")
        bid = brl.id
        # включить сортировку по серебру (богатые сверху) и дождаться применения
        await self.click_text(brl, UI["btn_sort_silver"], label="Серебро ↓")
        for _ in range(8):
            brl = await self.refetch(bid)
            if "Серебро" in (brl.message or "").split("Цели")[0]:
                break
            await asyncio.sleep(0.5)

        seen, rich = set(), []
        _, total = parse_pages(brl.message or "")
        log(f"📖 Экран бочки: сканирую {min(self.pages, total)} стр. из {total} (сорт. по серебру)")
        for p in range(self.pages):
            for t in parse_barrel_targets(brl.message or ""):
                key = norm(t["name"])
                if key in seen:
                    continue
                seen.add(key)
                if t["silver"] <= 0:
                    continue
                if t["level"] is None or t["level"] < self.min_level:
                    continue
                rich.append(t)
            if p >= self.pages - 1 or p + 1 >= total:
                break
            # следующая страница: жмём «›» и ждём, пока номер страницы вырастет
            cur, _ = parse_pages(brl.message or "")
            if not await self.click_text(brl, UI["btn_next"], label="›"):
                log("  (кнопки страниц нет)")
                break
            for _ in range(8):
                brl = await self.refetch(bid)
                if parse_pages(brl.message or "")[0] > cur:
                    break
                await asyncio.sleep(0.5)
        # уйти с экрана бочки, чтобы случайно не остаться на закладке
        await self.click_text(brl, UI["btn_back"], label="Назад")
        rich.sort(key=lambda x: x["silver"], reverse=True)
        log(f"💰 Богатых (ур.≥{self.min_level}, с серебром): {len(rich)}")
        for t in rich[:30]:
            log(f"   🪙 {t['silver']:>12,} — {t['name']} (ур.{t['level']})".replace(",", " "))
        return rich

    # ---------- арена: открыть и прочитать мою атаку ----------
    async def open_arena(self):
        """Открыть ПОЛНУЮ арену (с шапкой «Жизни/Атака»). Ответ может быть и новым
        сообщением, и правкой старого — поэтому ищем по содержимому, не по id."""
        await self.send(UI["cmd_arena"])
        for _ in range(14):
            for m in sorted(await self.recent(8), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                t = m.message or ""
                if UI["arena_marker"] in t and "Жизни:" in t:
                    atk = parse_my_attack(t)
                    if atk:
                        self.my_attack = atk
                    return m
            await asyncio.sleep(0.5)
        raise RuntimeError("Арена не открылась")

    async def arena_search(self, arena_msg, name):
        """Найти цель по названию. Жмём «Поиск» (если кнопки нет — открываем арену
        заново), шлём имя и ловим экран-результат по содержимому («Поиск:» + имя)."""
        clicked = arena_msg is not None and \
            await self.click_text(arena_msg, UI["btn_search"], label="Поиск")
        if not clicked:
            fresh = await self.open_arena()
            if not await self.click_text(fresh, UI["btn_search"], label="Поиск"):
                return None
        await self.wait_for(UI["search_prompt"], tries=8)
        await self.send(name)
        want = norm(name)
        for _ in range(14):
            for m in sorted(await self.recent(8), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                t = m.message or ""
                if UI["arena_marker"] in t and "Поиск:" in t and \
                        (want in norm(t) or "стр. 1/1" in t.replace("стр.1/1", "стр. 1/1")):
                    return m
            await asyncio.sleep(0.5)
        return None

    # ---------- ФАЗА 2: пробивка ----------
    def beatable(self, defense):
        return self.my_attack is not None and self.my_attack >= defense * self.margin

    async def build_hitlist(self, rich):
        """Для каждой богатой цели — поиск на арене, чтение защиты и статуса.
        Вернуть итоговый список бьющихся, отсортированный по серебру."""
        ar = await self.open_arena()
        if not self.my_attack:
            raise RuntimeError("Не прочитал свою атаку с арены")
        my_hp = parse_my_hp(ar.message or "")
        log(f"⚔️ Моя атака: {self.my_attack}, ❤️ HP: {my_hp}/100. "
            f"Порог: защита × {self.margin:g}. Нужно целей: ~{self.want}")
        hit = []
        for t in rich:
            if len(hit) >= self.want:
                log(f"   (набрали {self.want} целей — хватит пробивать)")
                break
            res = await self.arena_search(ar, t["name"])
            if not res:
                log(f"   ⁇ {t['name']}: поиск не дал экрана — пропуск")
                continue
            ar = res
            blocks = parse_arena_targets(res.message or "")
            tbtns = target_buttons(self.flat_buttons(res))
            idx = exact_target(blocks, t["name"])
            # СТРОГО: нужна цель с точным совпадением имени И кнопка на той же позиции
            if idx is None or idx >= len(tbtns):
                log(f"   ⁇ {t['name']}: нет точного совпадения на арене — пропуск (не рискуем)")
                continue
            b = blocks[idx]
            attackable = button_attackable(tbtns[idx])
            ok_atk = self.beatable(b["defense"])
            ok_lvl = (b["level"] or 0) >= self.min_level
            if attackable and ok_atk and ok_lvl:
                hit.append({**t, "defense": b["defense"], "hp": b["hp"]})
                log(f"   ✅ {t['name']}: защ.{b['defense']} hp{b['hp']} "
                    f"🪙{t['silver']:,}".replace(",", " "))
            else:
                why = ("закрыто/щит/соклан" if not attackable else
                       f"ур.{b['level']}<{self.min_level}" if not ok_lvl else
                       f"защ.{b['defense']} не пробить")
                log(f"   — {t['name']}: {why}")
        hit.sort(key=lambda x: x["silver"], reverse=True)
        return hit

    # ---------- ФАЗА 3: обоз + набеги ----------
    async def purchase_oboz(self):
        con = await self.open_consumables()
        if not await self.click_text(con, UI["btn_oboz_open"], label="Обоз"):
            log("  ⚠️ не нашёл кнопку «Обоз» — пропускаю покупку")
            return
        oboz = await self.wait_for("", min_id=con.id, tries=8) or await self.refetch(con.id)
        a, b = UI["oboz_pick"]
        for r, col, txt in self.flat_buttons(oboz):
            if a in txt and b in txt:
                if UI["star"] in txt:
                    log(f"  ⛔ обоз за звёзды «{txt}» — не беру")
                    return
                if self.dry:
                    log(f"  [dry] купил бы обоз «{txt}»")
                    return
                await self.click(oboz, r, col, label=txt)
                log(f"  🐴 Куплен обоз: «{txt}»")
                return
        log("  ⚠️ не нашёл кнопку обоза +50%/50м")

    async def attack_once(self, arena_msg, target):
        """Найти цель на арене и ударить. Вернуть (исход, лут, свежее_сообщение_арены)."""
        res = await self.arena_search(arena_msg, target["name"])
        if not res:
            return "noscreen", 0, arena_msg
        tgts = parse_arena_targets(res.message or "")
        match = next((x for x in tgts if norm(x["name"]).find(norm(target["name"])) >= 0
                      or norm(target["name"]).find(norm(x["name"])) >= 0), None)
        btn = attack_button_for(self.flat_buttons(res), match["name"]) if match else None
        if not match or not btn:
            return "unavailable", 0, res      # нет кнопки «Атаковать» = щит/граница/соклан
        if (match["level"] or 0) < self.min_level:
            return "lowlevel", 0, res
        if not self.beatable(match["defense"]):
            return "tootough", 0, res
        if self.dry or self.scan_only:
            log(f"  [dry] ударил бы «{target['name']}» (защ.{match['defense']})")
            return "dry", 0, res
        r, col, _ = btn
        before = (await self.recent(1))[0].id
        await self.click(res, r, col, label=f"Атаковать {target['name']}")
        # бывает экран подтверждения — добиваем одним нажатием, если оно есть
        res2 = await self.refetch(res.id)
        for rr, cc, tt in self.flat_buttons(res2):
            low = tt.lower()
            if any(w in low for w in ("подтверд", "да, ", "набег", "ударить")) \
                    and UI["star"] not in tt:
                await self.click(res2, rr, cc, label=tt)
                break
        out = await self.wait_result(min_id=before)
        raw = out.message if out else ""
        outcome, loot = classify_result(raw)
        if outcome == "unknown" and raw:
            # незнакомый формат ответа на набег — покажем, чтобы дописать словари
            log("  📋 сырой ответ на набег: " + " ".join(raw.split())[:200])
        return outcome, loot, (await self.refetch(res.id))

    async def raid_loop(self, hitlist):
        """Крутить список по кругу, бить тех, у кого КД истёк.
        Стоп: вышло окно --duration, мои HP < min_hp, кончились цели, или круг-лимит.
        Частокол/ров — НЕ отказ: долбим по КД, пока не пробьём. Отказ (убрать цель):
        нет кнопки «Атаковать» (щит/граница/соклан) или не пробиваю защиту."""
        deadline = time.time() + self.duration * 60
        last_hit = {}     # norm(name) -> ts последнего удара
        dropped = set()   # norm(name) целей, выбывших из ротации насовсем
        log(f"🔥 Набеги: {len(hitlist)} целей, КД {self.kd}с/цель, окно {self.duration} мин, "
            f"стоп по HP < {self.min_hp}" + (f", кругов ≤ {self.max_rounds}" if self.max_rounds else ""))
        round_no = 0
        while time.time() < deadline:
            round_no += 1
            # начало круга: свежая арена — читаем мои HP и атаку
            full = await self.open_arena()
            my_hp = parse_my_hp(full.message or "")
            if my_hp is not None and my_hp < self.min_hp:
                log(f"🩸 Мои HP {my_hp}/100 < {self.min_hp} — СТОП (нечем атаковать).")
                break
            ar = full
            alive = [t for t in hitlist if norm(t["name"]) not in dropped]
            if not alive:
                log("🏁 Все цели выбыли (щиты/границы/непробиваемы) — СТОП.")
                break
            log(f"── Круг {round_no}: целей в ротации {len(alive)}, мои HP {my_hp}/100")
            did_something = False
            for t in alive:
                if time.time() >= deadline:
                    break
                key = norm(t["name"])
                if key in last_hit and time.time() - last_hit[key] < self.kd:
                    continue
                outcome, loot, ar = await self.attack_once(ar, t)
                if outcome == "win":
                    self.stats["hits"] += 1
                    self.stats["wins"] += 1
                    self.stats["loot"] += loot
                    last_hit[key] = time.time()
                    did_something = True
                    log(f"  ⚔️ {t['name']}: ПОБЕДА +{loot:,}🪙".replace(",", " "))
                elif outcome in ("blocked", "loss"):
                    # частокол/ров/ополчение — НЕ бросаем, долбим дальше по КД
                    self.stats["blocked" if outcome == "blocked" else "loss"] += 1
                    self.stats["hits"] += 1
                    last_hit[key] = time.time()
                    did_something = True
                    log(f"  🧱 {t['name']}: пробиваем дальше (частокол/ров/отбито), ждём КД")
                elif outcome == "cooldown":
                    last_hit[key] = time.time()   # ещё рано — отложим на КД
                elif outcome in ("unavailable", "tootough", "lowlevel"):
                    dropped.add(key)              # ОТКАЗ: щит/граница/соклан/непробиваемо
                    self.stats["skipped"] += 1
                    why = {"unavailable": "щит/граница/соклан", "tootough": "не пробиваю защиту",
                           "lowlevel": "уровень < мин"}[outcome]
                    log(f"  🚫 {t['name']}: убрал из ротации ({why})")
                elif outcome == "noscreen":
                    pass                          # поиск сорвался — попробуем в след. круге
                else:
                    log(f"  ⁇ {t['name']}: непонятный исход «{outcome}» — лог: проверь формат")
            if self.max_rounds and round_no >= self.max_rounds:
                log(f"🏁 Сделано {round_no} кругов (лимит --max-rounds) — СТОП.")
                break
            # если в этом круге всем рано — поспать до ближайшего истечения КД
            if not did_something:
                soonest = min((self.kd - (time.time() - ts) for ts in last_hit.values()
                               if time.time() - ts < self.kd), default=15)
                nap = max(5, min(soonest + 1, deadline - time.time()))
                if nap <= 0:
                    break
                log(f"  ⏳ все на КД — сплю {nap:.0f}с")
                await asyncio.sleep(nap)

    # ---------- оркестрация ----------
    def output_list(self, hitlist):
        """Показать чистый список целей и сохранить ники в raid_targets.txt."""
        log(f"\n🎯 СПИСОК ЦЕЛЕЙ ДЛЯ НАБЕГА (бьются, без щитов/границ/соклана): {len(hitlist)}")
        log("─" * 52)
        for i, t in enumerate(hitlist, 1):
            log(f"  {i:>2}. {t['name']:<22} 🪙{t['silver']:>11,}".replace(",", " ")
                + f"  ур.{t['level']}  защ.{t['defense']}  hp{t['hp']}")
        log("─" * 52)
        # ники в столбик — удобно копировать и бить руками через Поиск
        nicks = "\n".join(t["name"] for t in hitlist)
        log("\nНики (копируй и ищи в «Набеги → Поиск»):\n" + nicks)
        import os
        path = os.path.join(HERE, "raid_targets.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(nicks + "\n")
            log(f"\n💾 Сохранено в {path}")
        except OSError as e:
            log(f"  (не смог записать файл: {e})")

    async def run(self):
        rich = await self.scan_rich()
        if not rich:
            log("Нет богатых целей ур.≥%d — выходим." % self.min_level)
            return
        hitlist = await self.build_hitlist(rich)
        self.output_list(hitlist)
        # по умолчанию — ТОЛЬКО список. Авто-атака включается флагом --attack.
        if not self.attack:
            return
        if self.dry or self.scan_only or not hitlist:
            log("\n(--attack с dry/scan-only или пустым списком — набеги пропущены)")
            return
        if self.buy_oboz:
            await self.purchase_oboz()
        await self.raid_loop(hitlist)
        self.report()

    def report(self):
        s = self.stats
        log("\n══════════ ИТОГ НАБЕГОВ ══════════")
        log(f"  Ударов: {s['hits']}  Побед: {s['wins']}  Отбито: {s['loss']}  "
            f"Заблок.: {s['blocked']}")
        log(f"  🪙 Награблено за сессию: {s['loot']:,}".replace(",", " "))


# ════════════════════════════════════════════════════════════════════════════
async def main():
    ap = argparse.ArgumentParser(description="Автонабеги по жирным соперникам (@holop)")
    ap.add_argument("--pages", type=int, default=6, help="страниц бочки сканировать (деф. 6)")
    ap.add_argument("--want", type=int, default=10, help="сколько бьющихся целей набрать (деф. 10)")
    ap.add_argument("--margin", type=float, default=1.1, help="порог: атака ≥ защита×margin (деф. 1.1)")
    ap.add_argument("--min-level", type=int, default=10, help="мин. уровень цели (деф. 10)")
    ap.add_argument("--min-hp", type=int, default=20, help="стоп, когда мои HP ниже (деф. 20)")
    ap.add_argument("--duration", type=int, default=50, help="минут крутить набеги (деф. 50)")
    ap.add_argument("--max-rounds", type=int, default=0, help="лимит кругов (0=без лимита)")
    ap.add_argument("--kd", type=int, default=300, help="КД на цель, секунд (деф. 300)")
    ap.add_argument("--no-oboz", action="store_true", help="не покупать обоз (только с --attack)")
    ap.add_argument("--attack", action="store_true",
                    help="ВКЛючить авто-набеги (по умолчанию — только список ников)")
    ap.add_argument("--scan-only", action="store_true", help="(совместимость) только список")
    ap.add_argument("--dry-run", action="store_true", help="пройтись по экранам, ничего не жать")
    args = ap.parse_args()

    setup_logging()
    cfg = load_config()
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        log("Заполни api_id/api_hash в config.json.")
        sys.exit(1)

    if cfg.get("session_string"):
        client = TelegramClient(StringSession(cfg["session_string"]), int(cfg["api_id"]), cfg["api_hash"])
    else:
        import os
        session = os.path.join(HERE, cfg.get("session_name", "holop_session"))
        client = TelegramClient(session, int(cfg["api_id"]), cfg["api_hash"])
    await client.start()
    me = await client.get_me()
    mode = "АВТО-АТАКА" if args.attack else "ТОЛЬКО СПИСОК"
    log(f"[{datetime.now():%H:%M:%S}] Вошёл как {me.first_name}. Режим: {mode}")

    raider = Raider(client, cfg, args)
    try:
        await raider.run()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
