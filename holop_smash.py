#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOLOP SMASH — автономный авто-бой набегов по ФИКСИРОВАННОМУ списку ников (@holop).

Отличие от holop_raid.py: тот сканирует богатых по серебру. Этот — просто долбит
заранее заданный список игроков по кругу, вечно, пока его не остановят.

Тактика (снята вживую с игры 07.07.2026, аккаунт 👑 Vladimir):
  По кругу для каждой цели из TARGETS:
    • Открываю арену (Набеги), читаю СВОИ HP.
      – Если мои HP ≤ my_min_hp (20) → ждать (my_recover_to − hp) × min_per_hp мин
        (реген +1 HP/мин, множитель 2 = запас), потом продолжить.
    • Ищу цель: «Набеги → Поиск → <ник>». Поиск в игре по ПОДСТРОКЕ (!) —
      поэтому беру блок со СТРОГИМ совпадением имени и кнопку того же индекса
      (иначе можно ударить не того — баг «SS» ловил «god bless»).
    • Кнопка «Атаковать <Имя>» → цель бьётся:
        – HP цели ≤ tgt_min_hp (20) → ждать (tgt_recover_to − hp) × min_per_hp мин.
        – иначе УДАР. После удара КД на эту цель = attack_cd (300с) + джиттер 5–15с.
        – Частокол 🪵 / ров — это НЕ отказ (кнопка всё равно «Атаковать»): бьём
          насквозь по КД, пока не пробьём.
    • Кнопка не «Атаковать» (щит):
        – «• Свой клан» / «• ниже N ур.» — надолго не изменится → ретрай через clan_level_retry.
        – 🟢 Полевой щит / 🧱 Стена / 🛡️ Купол / Закрыто / граница → открываю ПРОФИЛЬ
          цели (Территория → Найти → ник → её кнопка) и читаю таймер щита из блока
          «🛡️ Статус • … — 59мин». Ставлю таймер на (время щита + буфер).
          Если таймер не распарсился → ретрай через shield_default_retry.

Управление (пульт) — БЕЗ Telegram-чатов и БЕЗ Избранного:
  Файл smash_control.txt рядом со скриптом. Содержимое:
    run    — работать (по умолчанию, если файла нет)
    pause  — пауза (скрипт крутится вхолостую, ждёт)
    stop   — корректно выйти
  Скрипт опрашивает файл каждые ~3с и между действиями.

Предохранители: звёзды ⭐ не тратим никогда; бьём только строго совпавшую цель;
--dry-run / --selftest ничего не атакуют.

Запуск:
    python3 holop_smash.py --selftest   # разведка: показать состояние всех целей, НЕ бить
    python3 holop_smash.py --dry-run    # крутить цикл, но не жать «Атаковать»
    python3 holop_smash.py              # боевой авто-режим (пульт: smash_control.txt)

Конфиг и сессия — общие с holop_reroll.py (config.json, holop_session).
"""

import argparse
import asyncio
import json
import logging
import os
import random
import re
import signal
import sys
import time
from datetime import datetime

from telethon import TelegramClient
from telethon.tl.custom import Message
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

# переиспользуем проверенный фундамент и парсеры
from holop_reroll import HERE, load_config
from holop_raid import (
    NOISE, norm, parse_my_hp, parse_my_attack, parse_arena_targets,
    exact_target, button_attackable, classify_result, _is_control_btn,
)

# ════════════════════════════════════════════════════════════════════════════
#  ЦЕЛИ И НАСТРОЙКИ ПО УМОЛЧАНИЮ (правится тут или в config.json → "smash")
# ════════════════════════════════════════════════════════════════════════════
# Пусто по умолчанию: свежая установка НИ ПО КОМУ не бьёт, пока ты сам
# не впишешь ников в панели (вкладка «Набеги» → «Цели» → «Сохранить список»).
TARGETS = []

DEFAULTS = {
    "attack_cd": 300,            # базовый КД на цель, секунд (5 минут)
    "jitter_lo": 5,             # + случайные секунды к КД (низ)
    "jitter_hi": 15,            # + случайные секунды к КД (верх)
    "my_min_hp": 25,            # HP-порог: на ≤25 уходим лечиться (запас над игровым мин.20 — не слетать с защиты замка)
    "my_recover_to": 50,        # до скольки HP лечиться перед продолжением
    "tgt_min_hp": 20,           # цель бьём только если у неё HP выше
    "tgt_recover_to": 50,       # до скольки ждём реген цели
    "min_per_hp": 1.0,          # минут на 1 HP при ожидании регена (в игре реген ~1 HP/мин)
    "shield_default_retry": 30,  # мин, если таймер щита не распарсился
    "weak_retry": 30,           # мин, если цель «Слаб» и HP прочитать не удалось
    "notfound_retry": 10,       # мин, если цель не нашлась / нет точного совпадения
    "clan_level_retry": 120,    # мин для «свой клан» / «ниже ур.» (само не изменится)
    "inter_hit_lo": 4,          # пауза между разными целями (низ), сек
    "inter_hit_hi": 10,         # пауза между разными целями (верх), сек
    "control_file": "smash_control.txt",
    "heartbeat_min": 15,        # как часто писать в лог сводку, минут
    "heal_recheck": 180,        # базовый интервал перечитывания HP в лечении, сек (было 90 — с запасом)
    "heal_recheck_jitter": 40,  # ± случайные секунды к интервалу перечитывания HP
    # ── защита от бочки (динамита) ──
    "bomb_check_interval": 75,  # как часто опрашивать «Дружина» на бочку, сек
    "bomb_fuse": "красн",       # какой фитиль резать (подстрока, регистр не важен)
    "ognivo_cost": 900,         # цена Огнива, золото
    "heal_cost": 100000,        # лечение территории после взрыва, серебро
    "kazna_gold_buffer": 3000,  # сколько золота снимать с казны с запасом
    "kazna_silver_buffer": 50000,  # запас серебра при снятии
    "bomb_max_gold": 12000,     # ЖЁСТКИЙ потолок трат золота на ОДИН взрыв-инцидент
    "bomb_max_silver": 250000,  # ЖЁСТКИЙ потолок трат серебра на один инцидент
}


def heal_recheck_secs(s):
    """Интервал перечитывания HP при лечении с небольшим рандомом (не долбить ровно по таймеру)."""
    j = s.get("heal_recheck_jitter", 0)
    base = s.get("heal_recheck", 180)
    return max(30.0, base + random.uniform(-j, j))

# маркеры экранов (снято вживую 07.07.2026)
ARENA_MARKER = "АРЕНА БИТВ"
SEARCH_PROMPT = "Введи название"
FIND_PROMPT = "Введи имя территории"
STAR = "⭐"

# ── бочка/динамит (тексты сняты из истории 13.07.2026) ──
MINED_MARKER = "ЗАМИНИРОВАНА"                 # «⚠️ ТВОЯ ТЕРРИТОРИЯ ЗАМИНИРОВАНА!»
BOMB_WARN = "скоро взорвётся"                 # поздний пуш «Бочка ... скоро взорвётся»
DEFUSED_WORDS = ("обезврежена", "в безопасности", "правильный фитиль")
EXPLODED_WORDS = ("взорвал", "взрыв", "неправильн", "не тот фитиль", "уничтож", "разрушен",
                  "территория взорвана")


def parse_amount(s):
    """«142.5K» / «1.9M» / «2 077 391 304» / «622» → int (штук/монет)."""
    t = (s or "").strip().replace(" ", "").replace(" ", "").replace("\xa0", "")
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*([kmbкмбKMBКМБ]?)", t)
    if not m:
        return 0
    val = float(m.group(1).replace(",", "."))
    mult = {"k": 1e3, "к": 1e3, "m": 1e6, "м": 1e6, "b": 1e9, "б": 1e9}.get(m.group(2).lower(), 1)
    return int(val * mult)


def parse_mine_seconds(text):
    """«⏰ Осталось: 9м 44с» → секунды до взрыва (None если нет)."""
    m = re.search(r"Осталось:\s*([^\n]+)", text or "")
    return parse_duration(m.group(1)) if m else None


def parse_ognivo_count(text):
    """Сколько Огнива на руках из строки «🔥 Огниво: N шт.» (0 если нет)."""
    m = re.search(r"Огниво:\s*(\d+)", text or "")
    return int(m.group(1)) if m else 0


# ════════════════════════════════════════════════════════════════════════════
#  ЛОГИРОВАНИЕ (отдельный файл smash.log, чтобы не мешать run.log реролла)
# ════════════════════════════════════════════════════════════════════════════
logger = logging.getLogger("holop_smash")


def setup_logging():
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(os.path.join(HERE, "smash.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
    logger.addHandler(fh)


def log(msg):
    logger.info(msg)


# ════════════════════════════════════════════════════════════════════════════
#  ДОП. ПАРСЕРЫ (чистые функции)
# ════════════════════════════════════════════════════════════════════════════
def parse_duration(seg):
    """«59мин» / «1ч 20мин» / «2ч» / «45сек» / «1д 3ч» → секунды (int) или None."""
    total = 0
    for num, unit in re.findall(r"(\d+)\s*(дн|дня|дней|д|час[а-яё]*|ч|мин[а-яё]*|м|сек[а-яё]*|с)", seg or ""):
        n = int(num)
        if unit.startswith("д"):
            total += n * 86400
        elif unit.startswith("ч") or unit.startswith("час"):
            total += n * 3600
        elif unit.startswith("мин") or unit == "м":
            total += n * 60
        else:
            total += n
    return total or None


def parse_shield_seconds(profile_text):
    """Из профиля вытащить остаток щита из блока «🛡️ Статус • … — <время>». None если нет."""
    m = re.search(r"Статус[\s\S]{0,120}?—\s*([^\n]+)", profile_text or "")
    if not m:
        return None
    return parse_duration(m.group(1))


# маркеры исхода боя (словарь classify_result из holop_raid неполон — уточняем локально)
ABSORB_WORDS = ("частокол", "поглотил", "выдержал", "заряд")   # защита поглотила удар — НЕ проигрыш, HP цел
LOSS_WORDS = ("отступ", "героическая оборона", "вынуждены", "поражение", "разбит",
              "отброшен", "не смогл", "провалил", "неудач", "отбит", "устоял")
WIN_WORDS = ("вотчина пала", "растоптан", "молниеносн", "победу", "празднова",
             "контрибуц", "захвачено", "награблено", "сорваны и")


def refine_outcome(text, base):
    """Уточнить исход: пробивка защиты (blocked) vs настоящее поражение (loss) vs победа."""
    low = (text or "").lower()
    if any(w in low for w in ABSORB_WORDS):
        return "blocked"                 # частокол/ров поглотили — бьём насквозь, HP цел
    if base in ("win", "cooldown"):
        return base
    if any(w in low for w in WIN_WORDS):
        return "win"
    if any(w in low for w in LOSS_WORDS):
        return "loss"
    return base


def parse_result_my_hp(result_text):
    """Мои HP из итога боя (первый ❤️ X/MAX после разделителя ━). None если нет.
    MAX может быть не 100 (амулет жизни +20 → 120), поэтому знаменатель любой."""
    part = result_text.split("━")[-1] if "━" in (result_text or "") else (result_text or "")
    m = re.search(r"❤️\s*(\d+)\s*/\s*\d+", part)
    return int(m.group(1)) if m else None


# отказ игры «Твоя территория слишком слаба для атаки! Здоровье: 16/100» — мои HP < 20
TOO_WEAK_MARKERS = ("слишком слаба для атаки", "минимум для атаки")


def is_too_weak_refusal(text):
    low = (text or "").lower()
    return any(m in low for m in TOO_WEAK_MARKERS)


def parse_my_low_hp(text):
    """Мои HP из «Здоровье: 16/100» / «Жизни: 54/120» и т.п. Знаменатель любой (амулет→120)."""
    m = re.search(r"(?:Здоровье|Жизни)\s*:?\s*(\d+)\s*/\s*\d+", text or "")
    return int(m.group(1)) if m else None


def parse_rep(text):
    """Заработанная репутация из итога боя: «📈 +2.0 репутации» → 2.0 (0.0 если нет)."""
    m = re.search(r"([+\-]?\d+(?:[.,]\d+)?)\s*репутаци", text or "")
    return float(m.group(1).replace(",", ".")) if m else 0.0


CD_BTN_RE = re.compile(r"•\s*\d+\s*(?:ч|мин|м|сек|с)")   # «Имя • 2м 53с» — персональный КД


def classify_block_reason(btn_text):
    """Почему цель нельзя бить (по тексту её кнопки-статуса)."""
    low = (btn_text or "").lower()
    if "свой клан" in low or "соклан" in low:
        return "clan"
    if "ниже" in low and "ур" in low:
        return "level"
    if "слаб" in low or "💤" in (btn_text or ""):
        return "weak"   # соперник слишком слаб / мало HP — не щит, ждём его реген
    if CD_BTN_RE.search(btn_text or ""):
        return "cooldown"   # цель на нашем 5-мин КД — ждём по таймеру с кнопки
    if any(w in low for w in ("полев", "щит", "стена", "купол", "закрыт", "границ", "требуш")):
        return "shield"
    return "shield"   # неизвестный блок трактуем как щит (откроем профиль, поставим таймер)


def target_positions(flat_buttons):
    """Ведущие кнопки целей (r, col, text) до первой управляющей (сортировки/пагинация)."""
    out = []
    for r, c, t in flat_buttons:
        if _is_control_btn(t):
            break
        out.append((r, c, t))
    return out


def fmt_secs(s):
    s = int(max(0, s))
    if s >= 3600:
        return f"{s // 3600}ч {(s % 3600) // 60}м"
    if s >= 60:
        return f"{s // 60}м {s % 60}с"
    return f"{s}с"


# ════════════════════════════════════════════════════════════════════════════
#  БОТ
# ════════════════════════════════════════════════════════════════════════════
class Smasher:
    def __init__(self, client, cfg, args):
        self.c = client
        self.bot = cfg.get("bot_username", "holop")
        self.dry = args.dry_run
        s = dict(DEFAULTS)
        s.update(cfg.get("smash", {}) or {})
        self.s = s
        self.control_path = os.path.join(HERE, s["control_file"])
        self.bench_path = os.path.join(HERE, "smash_bench.txt")     # снятые с ротации после поражения
        self.targets_path = os.path.join(HERE, "smash_targets.txt")  # редактируемый список целей
        self.settings_path = os.path.join(HERE, "smash_settings.json")  # живые настройки из панели
        self.apply_live_settings()   # подхватить настройки из панели на старте
        self._default_targets = list(cfg.get("smash_targets") or TARGETS)
        self.targets = self.load_targets()
        self.ensure_targets_file()
        self.lo = float(cfg.get("min_delay", 0.8))
        self.hi = float(cfg.get("max_delay", 1.8))
        # состояние
        self.next_ok = {}    # norm-имя? нет: имя -> epoch, когда цель снова доступна
        self.stats = {"hits": 0, "wins": 0, "blocked": 0, "loss": 0, "loot": 0, "rep": 0.0}
        self._paused_note = False
        self._last_heartbeat = 0.0
        self._started = 0.0      # время старта боевой сессии (для итогового отчёта)
        self.peer = None   # кэш entity бота (резолвим один раз)
        self._healing = False    # режим лечения: не атакуем, перечитываем реальное HP
        self._heal_start = 0.0   # когда ушли на лечение (для аварийного потолка)
        self._last_bomb_check = 0.0   # когда последний раз опрашивали «Дружину» на бочку
        self.stats.update({"bombs": 0, "defused": 0, "exploded": 0,
                           "spent_gold": 0, "spent_silver": 0})

    # ---------- список целей (файл smash_targets.txt) ----------
    def load_targets(self):
        """Читать список целей из файла (по нику в строке, # — комментарий).
        Читаем каждый цикл — правки из панели подхватываются на лету. Пусто → дефолт."""
        out = []
        try:
            with open(self.targets_path, "r", encoding="utf-8") as f:
                for line in f:
                    n = line.split("#", 1)[0].strip()
                    if n and n not in out:
                        out.append(n)
        except OSError:
            pass
        return out or list(self._default_targets)

    def apply_live_settings(self):
        """Подхватить настройки боя из панели (smash_settings.json) — применяется на лету."""
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        ints = {"my_min_hp", "my_recover_to"}
        for k in ("my_min_hp", "my_recover_to", "min_per_hp",
                  "attack_cd", "jitter_lo", "jitter_hi"):
            if k in data:
                try:
                    self.s[k] = int(data[k]) if k in ints else float(data[k])
                except (TypeError, ValueError):
                    pass

    def ensure_targets_file(self):
        """Если файла целей нет — создать с текущим списком (чтобы панель могла его показать)."""
        if not os.path.exists(self.targets_path):
            try:
                with open(self.targets_path, "w", encoding="utf-8") as f:
                    f.write("# Недоброжелатели: по одному нику в строке. # — комментарий.\n")
                    f.write("\n".join(self.targets) + "\n")
            except OSError:
                pass

    # ---------- низкоуровневые помощники ----------
    def _spread(self, t_target, name, gap=8.0):
        """Развести время атак: чтобы удары по разным целям не приходились на один момент.
        Пока новое время ближе gap секунд к чьему-то — добавляем ещё случайные секунды."""
        s = self.s
        others = [v for k, v in self.next_ok.items() if k != name]
        guard = 0
        while others and any(abs(t_target - o) < gap for o in others) and guard < 25:
            t_target += random.uniform(s["jitter_lo"], s["jitter_hi"])
            guard += 1
        return t_target

    async def pause(self):
        await asyncio.sleep(random.uniform(self.lo, self.hi))

    async def inter_hit(self):
        await asyncio.sleep(random.uniform(self.s["inter_hit_lo"], self.s["inter_hit_hi"]))

    async def _ensure_conn(self):
        """Гарантировать живое соединение и разрезолвленный entity бота."""
        if not self.c.is_connected():
            await self.c.connect()
        if self.peer is None:
            self.peer = await self.c.get_input_entity(self.bot)

    async def _net(self, factory, tries=8):
        """Выполнить сетевое действие (factory→свежая корутина) с переподключением на обрыве."""
        delay = 3
        last = None
        for _ in range(tries):
            try:
                await self._ensure_conn()
                return await factory()
            except FloodWaitError as e:
                wait = e.seconds + random.uniform(1, 3)
                log(f"  ⏳ FloodWait: жду {wait:.0f}с")
                await asyncio.sleep(wait)
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                last = e
                log(f"  🔌 связь потеряна ({type(e).__name__}) — переподключаюсь через {delay:.0f}с")
                try:
                    await self.c.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        raise last or ConnectionError("сеть недоступна после ретраев")

    async def recent(self, limit=8):
        # @holop иногда шлёт сообщения с TL-объектом, который старый Telethon не парсит
        # (спец-анимации боя) → get_messages падает TypeNotFoundError. Не валимся: вернём
        # пустое, вызвавший подождёт/повторит (битое сообщение вытеснится новыми).
        try:
            return await self._net(lambda: self.c.get_messages(self.peer, limit=limit)) or []
        except Exception as e:
            if type(e).__name__ == "TypeNotFoundError":
                log("  🧩 нераспознанный TL-объект в ленте — пропускаю это чтение")
                return []
            raise

    async def refetch(self, msg_id):
        try:
            return await self._net(lambda: self.c.get_messages(self.peer, ids=msg_id))
        except Exception as e:
            if type(e).__name__ == "TypeNotFoundError":
                return None
            raise

    async def send(self, text):
        return await self._net(lambda: self.c.send_message(self.peer, text))

    def flat_buttons(self, msg: Message):
        out = []
        if msg and msg.buttons:
            for r, row in enumerate(msg.buttons):
                for col, b in enumerate(row):
                    out.append((r, col, (b.text or "")))
        return out

    async def click(self, msg: Message, r, col, *, label=""):
        if self.dry:
            log(f"  [dry] клик: {label or (r, col)}")
            return None
        res = await self._net(lambda: msg.click(r, col))
        await self.pause()
        return res

    async def click_text(self, msg: Message, substr, *, label=""):
        for r, col, t in self.flat_buttons(msg):
            if substr.lower() in (t or "").lower():
                if STAR in t:
                    log(f"  ⛔ пропускаю платную кнопку «{t}»")
                    return False
                await self.click(msg, r, col, label=label or t)
                return True
        return False

    async def wait_text(self, contains, tries=12, delay=0.5):
        for _ in range(tries):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                if contains in (m.message or ""):
                    return m
            await asyncio.sleep(delay)
        return None

    # ---------- СКАМЕЙКА (файл smash_bench.txt) — снятые после поражения ----------
    def load_benched(self):
        """Множество norm(ник) целей, снятых с ротации. Читаем файл каждый раз — чтобы
        твоё распоряжение (убрать ник из файла) подхватывалось на лету."""
        out = set()
        try:
            with open(self.bench_path, "r", encoding="utf-8") as f:
                for line in f:
                    n = line.strip()
                    if n:
                        out.add(norm(n))
        except OSError:
            pass
        return out

    def bench_add(self, name):
        """Занести цель на скамейку (после поражения). Не дублируем."""
        if norm(name) in self.load_benched():
            return
        try:
            with open(self.bench_path, "a", encoding="utf-8") as f:
                f.write(name + "\n")
        except OSError as e:
            log(f"  (не смог записать скамейку: {e})")

    # ---------- ПУЛЬТ (файл smash_control.txt) ----------
    def control_state(self):
        try:
            with open(self.control_path, "r", encoding="utf-8") as f:
                v = f.read().strip().lower()
        except OSError:
            return "run"   # нет файла — работаем
        if v.startswith("stop") or v.startswith("стоп") or "выключ" in v or "kill" in v:
            return "stop"
        if v.startswith("pause") or v.startswith("пауза") or v.startswith("стой"):
            return "pause"
        return "run"

    async def gate(self):
        """Дождаться состояния run. Вернуть 'run' или 'stop'. Во время pause крутимся вхолостую."""
        while True:
            st = self.control_state()
            if st == "run":
                if self._paused_note:
                    log("▶️  СТАРТ — продолжаю набеги.")
                    self._paused_note = False
                return "run"
            if st == "stop":
                log("⏹  STOP — останавливаюсь.")
                return "stop"
            if not self._paused_note:
                log("⏸  ПАУЗА — жду 'run' в пульте (smash_control.txt).")
                self._paused_note = True
            await asyncio.sleep(3)

    async def sleep_gated(self, seconds):
        """Спать, но просыпаться рано, если пульт переключили (pause/stop). Вернуть состояние."""
        end = time.time() + seconds
        while time.time() < end:
            st = self.control_state()
            if st != "run":
                return st
            await asyncio.sleep(min(3, end - time.time()))
        return "run"

    # ---------- арена ----------
    async def open_arena(self):
        """Открыть полную арену (с шапкой «Жизни/Атака»). Вернуть сообщение."""
        await self.send("Набеги")
        for _ in range(16):
            for m in sorted(await self.recent(8), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                t = m.message or ""
                if ARENA_MARKER in t and "Жизни:" in t:
                    return m
            await asyncio.sleep(0.5)
        return None

    async def my_current_hp(self):
        """Надёжно прочитать МОИ HP с экрана «Территория» (❤️ Здоровье: X/100). None если не смог."""
        await self.send("Территория")
        for _ in range(12):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                t = m.message or ""
                if "ТЕРРИТОРИЯ" in t and ("Здоровье" in t or "Жизни" in t):
                    return parse_my_low_hp(t)
            await asyncio.sleep(0.5)
        return None

    async def press_search(self):
        """Нажать «Поиск» на самом свежем сообщении, где эта кнопка есть; иначе открыть арену."""
        msgs = [m for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True) if not m.out]
        newest = msgs[0] if msgs else None
        if newest and any((t or "").strip().lower() == "поиск" for _, _, t in self.flat_buttons(newest)):
            for r, c, t in self.flat_buttons(newest):
                if (t or "").strip().lower() == "поиск":
                    await self.click(newest, r, c, label="Поиск")
                    return True
        a = await self.open_arena()
        if not a:
            return False
        for r, c, t in self.flat_buttons(a):
            if (t or "").strip().lower() == "поиск":
                await self.click(a, r, c, label="Поиск")
                return True
        return False

    async def arena_search(self, name):
        """Найти цель на арене. Вернуть сообщение-результат (с блоками и кнопками) или None."""
        if not await self.press_search():
            return None
        await self.wait_text(SEARCH_PROMPT, tries=8)
        await self.send(name)
        want = norm(name)
        for _ in range(16):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                t = m.message or ""
                if ARENA_MARKER in t and "Поиск:" in t and want in norm(t):
                    return m
            await asyncio.sleep(0.5)
        return None

    # ---------- профиль цели (для таймера щита) ----------
    async def shield_seconds(self, name):
        """Территория → Найти → ник → кнопка точного совпадения → профиль → остаток щита (сек) или None."""
        await self.send("Территория")
        terr = None
        for _ in range(12):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                if any((t or "").strip().lower() == "найти" for _, _, t in self.flat_buttons(m)):
                    terr = m
                    break
            if terr:
                break
            await asyncio.sleep(0.5)
        if not terr or not await self.click_text(terr, "Найти", label="Найти"):
            return None
        await self.wait_text(FIND_PROMPT, tries=8)
        await self.send(name)
        want = norm(name)
        lst = None
        for _ in range(14):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                if "Результаты" in (m.message or "") and self.flat_buttons(m):
                    lst = m
                    break
            if lst:
                break
            await asyncio.sleep(0.5)
        if not lst:
            return None
        # строгое совпадение имени среди кнопок списка
        pos = None
        for r, c, t in self.flat_buttons(lst):
            if norm(t) == want:
                pos = (r, c)
                break
        if not pos:
            return None
        await self.click(lst, pos[0], pos[1], label=f"Профиль {name}")
        for _ in range(14):
            m = await self.refetch(lst.id)   # профиль приходит правкой того же сообщения
            t = m.message or ""
            if "Статус" in t or "БОЕВАЯ СТАТИСТИКА" in t:
                return parse_shield_seconds(t)
            await asyncio.sleep(0.5)
        return None

    # ---------- удар ----------
    def _is_result(self, text, msg):
        low = (text or "").lower()
        if any(w in low for w in ("потери в бою", "вотчина", "урон:", "контрибуц", "доблестн",
                                  "репутаци", "частокол", "поглотил", "выдержал", "устоял",
                                  "заряд", "отбит", "разгром", "провалил", "неудач")):
            return True
        if any((t or "").strip().lower() in ("к списку целей", "профиль жертвы")
               for _, _, t in self.flat_buttons(msg)):
            return True
        outcome, _ = classify_result(text or "")
        return outcome in ("win", "loss", "cooldown", "blocked")

    def _btn_went_cooldown(self, msg):
        """После удара кнопка цели превратилась в «Имя • Xм Yс» → удар засчитан."""
        return any(CD_BTN_RE.search(t or "") for _, _, t in self.flat_buttons(msg))

    async def attack(self, search_msg, pos, name):
        """Ударить цель (кнопка pos на search_msg). Вернуть (outcome, loot, my_hp_after)."""
        if self.dry:
            log(f"  [dry] ударил бы «{name}»")
            return "dry", 0, None
        before_id = (await self.recent(1))[0].id
        r, c, _ = pos
        await self.click(search_msg, r, c, label=f"Атаковать {name}")
        # результат: правка того же сообщения ЛИБО новое сообщение
        result = None
        landed = False
        for _ in range(20):
            m = await self.refetch(search_msg.id)
            mt = (m.message or "") if m else ""
            # игра отказала: «Твоя территория слишком слаба для атаки! Здоровье: 16/100»
            if is_too_weak_refusal(mt):
                return "myweak", 0, parse_my_low_hp(mt)
            if m and self._is_result(mt, m):
                result = mt
                break
            # кнопка цели ушла в КД → удар точно засчитан (частокол мог просто поглотить)
            if m and self._btn_went_cooldown(m):
                landed = True
                result = mt
                break
            for mm in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if mm.out or mm.id <= before_id:
                    continue
                mmt = mm.message or ""
                if any(nz in mmt for nz in NOISE):
                    continue
                if is_too_weak_refusal(mmt):
                    return "myweak", 0, parse_my_low_hp(mmt)
                if self._is_result(mmt, mm):
                    result = mmt
                    break
            if result:
                break
            await asyncio.sleep(0.6)
        if not result:
            # Молчаливый ответ (частокол/ров): само сообщение не меняется. Проверяем
            # по земле — свежий поиск покажет, ушла ли цель в КД (значит удар прошёл).
            try:
                await asyncio.sleep(1.0)
                verify = await self.arena_search(name)
                if verify:
                    vpos = target_positions(self.flat_buttons(verify))
                    vidx = exact_target(parse_arena_targets(verify.message or ""), name)
                    if vidx is not None and vidx < len(vpos) and CD_BTN_RE.search(vpos[vidx][2] or ""):
                        secs = parse_duration(vpos[vidx][2]) or 0
                        log(f"  🧱 {name}: удар прошёл молча (частокол/ров), цель в КД {fmt_secs(secs)}")
                        return "blocked", 0, None
                    if vidx is not None and vidx < len(vpos):
                        log(f"  📋 noresult, кнопка цели сейчас: «{vpos[vidx][2].strip()}»")
            except Exception:
                pass
            return "noresult", 0, None
        outcome, loot = classify_result(result)
        outcome = refine_outcome(result, outcome)
        self.stats["rep"] += parse_rep(result)   # 📈 репутация с этого боя (если есть)
        if outcome == "unknown" and landed:
            outcome = "blocked"   # удар прошёл (кнопка в КД), но текст не разобрали — частокол/ров
        elif outcome == "unknown":
            log("  📋 сырой ответ на набег: " + " ".join(result.split())[:200])
        return outcome, loot, parse_result_my_hp(result)

    # ---------- один проход по всем целям ----------
    async def do_target(self, name):
        """Обработать одну цель: ударить / поставить таймер. Обновляет self.next_ok[name]."""
        s = self.s
        res = await self.arena_search(name)
        if not res:
            self.next_ok[name] = time.time() + s["notfound_retry"] * 60
            log(f"  ⁇ {name}: поиск не дал экрана — ретрай через {s['notfound_retry']}м")
            return
        blocks = parse_arena_targets(res.message or "")
        positions = target_positions(self.flat_buttons(res))
        idx = exact_target(blocks, name)
        if idx is None or idx >= len(positions):
            self.next_ok[name] = time.time() + s["notfound_retry"] * 60
            log(f"  ⁇ {name}: нет строгого совпадения на арене — ретрай через {s['notfound_retry']}м")
            return
        b = blocks[idx]
        btn = positions[idx][2]

        if button_attackable(btn):
            hp = b.get("hp")
            if hp is not None and hp <= s["tgt_min_hp"]:
                goal = s["tgt_min_hp"] + 1        # бить можно уже с 20+ HP — ждём только до этого
                wait_min = max(1.0, (goal - hp) * s["min_per_hp"])
                self.next_ok[name] = time.time() + wait_min * 60
                log(f"  💤 {name}: HP {hp} ≤ {s['tgt_min_hp']} — жду до {goal}+ ~{wait_min:.0f}м")
                return
            outcome, loot, my_after = await self.attack(res, positions[idx], name)
            # игра отказала по низкому HP — уходим на лечение, удар НЕ засчитан
            if outcome == "myweak":
                hp = my_after if isinstance(my_after, int) else 0
                self._healing = True
                self._heal_start = time.time()
                log(f"🩸 Игра: HP {hp} < {s['my_min_hp']} — мало для атаки. Ухожу на лечение "
                    f"до {s['my_recover_to']} (перечитываю HP ~каждые {s['heal_recheck']}с).")
                return hp
            # ПОРАЖЕНИЕ — снимаем цель с ротации до распоряжения (чтобы не сливать HP)
            if outcome == "loss":
                self.stats["hits"] += 1
                self.stats["loss"] += 1
                self.bench_add(name)
                log(f"  ❌ {name}: ПОРАЖЕНИЕ в бою — СНЯЛ С РОТАЦИИ до твоего распоряжения "
                    f"(чтобы не сливать HP). Скажи «верни {name}», чтобы вернуть.")
                return my_after
            cd = s["attack_cd"] + random.uniform(s["jitter_lo"], s["jitter_hi"])
            self.next_ok[name] = self._spread(time.time() + cd, name)
            cd = self.next_ok[name] - time.time()   # для лога — реальный КД после развода
            self.stats["hits"] += 1
            if outcome == "win":
                self.stats["wins"] += 1
                self.stats["loot"] += loot
                log(f"  ⚔️ {name}: ПОБЕДА +{loot:,}🪙, КД {fmt_secs(cd)}".replace(",", " "))
            elif outcome == "blocked":
                self.stats["blocked"] += 1
                log(f"  🧱 {name}: частокол/ров — бьём насквозь, КД {fmt_secs(cd)}")
            elif outcome == "cooldown":
                log(f"  ⌛ {name}: рано (КД у бота) — жду {fmt_secs(cd)}")
            elif outcome == "dry":
                self.stats["hits"] -= 1
                log(f"  [dry] {name}: удар пропущен, КД {fmt_secs(cd)}")
            elif outcome == "noresult":
                log(f"  ⚠️ {name}: результат не распознан — повтор через {fmt_secs(cd)}")
            else:
                log(f"  ⁇ {name}: непонятный исход «{outcome}» — см. сырой лог выше")
            return my_after

        # НЕ атакуется — КД/щит/клан/уровень/слаб
        reason = classify_block_reason(btn)
        if reason == "cooldown":
            secs = parse_duration(btn) or s["attack_cd"]
            self.next_ok[name] = time.time() + secs + 5
            log(f"  ⌛ {name}: на КД ещё {fmt_secs(secs)} — жду по таймеру кнопки")
            return
        if reason in ("clan", "level"):
            self.next_ok[name] = time.time() + s["clan_level_retry"] * 60
            why = "свой клан" if reason == "clan" else "ниже уровня"
            log(f"  🚫 {name}: {why} — ретрай через {s['clan_level_retry']}м")
            return
        if reason == "weak":
            hp = b.get("hp")
            if hp is not None and hp <= s["tgt_min_hp"]:
                goal = s["tgt_min_hp"] + 1        # ждём только до 20+ HP, а не до 50
                wait_min = max(1.0, (goal - hp) * s["min_per_hp"])
                self.next_ok[name] = time.time() + wait_min * 60
                log(f"  💤 {name}: слаб (HP {hp}) — жду до {goal}+ ~{wait_min:.0f}м")
            else:
                self.next_ok[name] = time.time() + s["weak_retry"] * 60
                log(f"  💤 {name}: слаб — ретрай через {s['weak_retry']}м")
            return
        # щит → в профиль за таймером
        secs = await self.shield_seconds(name)
        if secs and secs > 0:
            self.next_ok[name] = time.time() + secs + 30
            log(f"  🛡️ {name}: под щитом ещё ~{fmt_secs(secs)} — таймер поставлен")
        else:
            self.next_ok[name] = time.time() + s["shield_default_retry"] * 60
            log(f"  🛡️ {name}: щит, таймер не прочитан — ретрай через {s['shield_default_retry']}м")

    # ═══════════ ЗАЩИТА ОТ БОЧКИ (динамита) ═══════════
    async def open_druzhina(self):
        """Открыть экран «Дружина» (там разминирование). Ищем по кнопке с «Огниво»."""
        await self.send("Дружина")
        for _ in range(14):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                if any("огниво" in (bt or "").lower() for _, _, bt in self.flat_buttons(m)):
                    return m
            await asyncio.sleep(0.5)
        return None

    async def my_balance(self):
        """Свободные (на балансе) золото и серебро с экрана «Территория» → (gold, silver)."""
        await self.send("Территория")
        for _ in range(14):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                t = m.message or ""
                if "ТЕРРИТОРИЯ" in t and ("Золото" in t or "Серебро" in t):
                    g = re.search(r"Золото:\s*([^\n]+)", t)
                    sv = re.search(r"Серебро:\s*([^\n]+)", t)
                    return (parse_amount(g.group(1)) if g else 0,
                            parse_amount(sv.group(1)) if sv else 0)
            await asyncio.sleep(0.5)
        return (0, 0)

    async def kazna_withdraw(self, kind, amount):
        """Снять из «Личная казна» сумму (kind: 'gold'/'silver'). Многошаговый флоу."""
        amount = int(max(1, amount))
        section = "Золото" if kind == "gold" else "Серебро"
        log(f"  🏦 Снимаю из казны {amount} {'золота' if kind == 'gold' else 'серебра'}")
        await self.send("Личная казна")
        km = None
        for _ in range(12):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if not m.out and "Личная казна" in (m.message or "") and self.flat_buttons(m):
                    km = m
                    break
            if km:
                break
            await asyncio.sleep(0.5)
        if not km or not await self.click_text(km, section, label=section):
            log(f"  ⚠️ казна: не открыл раздел {section}")
            return False

        async def _find(pred, tries=10):
            for _ in range(tries):
                for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                    if not m.out and pred(m):
                        return m
                await asyncio.sleep(0.5)
            return None

        dep = await _find(lambda m: any((bt or "").strip().lower() == "снять"
                                        for _, _, bt in self.flat_buttons(m)))
        if not dep or not await self.click_text(dep, "Снять", label="Снять"):
            log("  ⚠️ казна: нет кнопки «Снять»")
            return False
        amt = await _find(lambda m: any("ввести сумму" in (bt or "").lower()
                                        for _, _, bt in self.flat_buttons(m)))
        if not amt or not await self.click_text(amt, "Ввести сумму", label="Ввести сумму"):
            log("  ⚠️ казна: нет кнопки «Ввести сумму»")
            return False
        await asyncio.sleep(0.6)
        await self.send(str(amount))
        await asyncio.sleep(1.2)
        log(f"  🏦 Запрошено снятие {amount} {section}")
        return True

    async def ensure_gold(self, need):
        g, _ = await self.my_balance()
        if g >= need:
            return True
        await self.kazna_withdraw("gold", (need - g) + self.s["kazna_gold_buffer"])
        g2, _ = await self.my_balance()
        return g2 >= need

    async def ensure_silver(self, need):
        _, sv = await self.my_balance()
        if sv >= need:
            return True
        await self.kazna_withdraw("silver", (need - sv) + self.s["kazna_silver_buffer"])
        _, sv2 = await self.my_balance()
        return sv2 >= need

    async def check_and_handle_bomb(self):
        """Вернуть True, если бочка найдена и обработана (тогда обычный цикл пропускаем)."""
        now = time.time()
        push = False
        try:
            for m in await self.recent(6):
                if not m.out and BOMB_WARN in (m.message or ""):
                    push = True
                    break
        except Exception:
            pass
        if not push and (now - self._last_bomb_check < self.s["bomb_check_interval"]):
            return False
        self._last_bomb_check = now
        dr = await self.open_druzhina()
        if not dr or MINED_MARKER not in (dr.message or ""):
            return False
        await self.handle_bomb(dr)
        return True

    async def handle_bomb(self, dr):
        txt = dr.message or ""
        who = re.search(r"Атаковал:\s*([^\n]+)", txt)
        secs = parse_mine_seconds(txt)
        self.stats["bombs"] += 1
        log("💣💣💣 БОЧКА! Заминировал: {} | осталось {} — РАЗМИНИРОВАНИЕ".format(
            who.group(1).strip() if who else "?", fmt_secs(secs) if secs else "?"))
        sp = {"gold": 0, "silver": 0}
        if not await self.ensure_ognivo(sp):
            log("  ⛔ нечем разминировать (Огниво/золото) — бочка может взорваться!")
            return
        outcome = await self.use_ognivo_red()
        if outcome == "defused":
            self.stats["defused"] += 1
            log("  ✅ БОЧКА ОБЕЗВРЕЖЕНА — территория цела.")
            return
        if outcome == "exploded":
            self.stats["exploded"] += 1
            log("  💥 ВЗРЫВ (фитиль не тот) — восстанавливаю территорию и холопов.")
            await self.recover_after_explosion(sp)
            return
        log(f"  ⁇ разминирование: непонятный исход «{outcome}» — проверь лог сырых экранов выше")

    async def ensure_ognivo(self, sp):
        s = self.s
        dr = await self.open_druzhina()
        if not dr:
            return False
        have = parse_ognivo_count(dr.message or "")
        if have == 0:
            for _, _, bt in self.flat_buttons(dr):
                m = re.search(r"огниво\s*x\s*(\d+)", (bt or "").lower())
                if m:
                    have = int(m.group(1))
                    break
        if have >= 1:
            return True
        cost = s["ognivo_cost"]
        if sp["gold"] + cost > s["bomb_max_gold"]:
            log("  ⛔ лимит золота на инцидент — Огниво не покупаю")
            return False
        if not await self.ensure_gold(cost):
            log("  ⛔ не хватает золота на Огниво даже после казны")
            return False
        dr = await self.open_druzhina()
        if not dr:
            return False
        for r, c, bt in self.flat_buttons(dr):
            low = (bt or "").lower()
            if "огниво" in low and "🏅" in (bt or "") and STAR not in (bt or "") \
                    and not re.search(r"огниво\s*x", low):
                await self.click(dr, r, c, label=bt)
                sp["gold"] += cost
                self.stats["spent_gold"] += cost
                log(f"  🛒 Куплено Огниво за {cost}🏅")
                await asyncio.sleep(0.8)
                return True
        log("  ⚠️ не нашёл кнопку покупки «Огниво 900🏅»")
        return False

    async def _read_defuse_result(self):
        for _ in range(14):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                low = (m.message or "").lower()
                if any(w in low for w in DEFUSED_WORDS):
                    return "defused"
                if any(w in low for w in EXPLODED_WORDS):
                    log("  📋 сырой ответ (взрыв): " + " ".join((m.message or "").split())[:200])
                    return "exploded"
            await asyncio.sleep(0.5)
        dr = await self.open_druzhina()
        if dr and MINED_MARKER not in (dr.message or ""):
            return "defused"      # мины больше нет и явного взрыва не видели → считаем обезврежено
        return "unknown"

    async def use_ognivo_red(self):
        dr = await self.open_druzhina()
        if not dr:
            return "noscreen"
        used = False
        for r, c, bt in self.flat_buttons(dr):
            if re.search(r"огниво\s*x\s*\d", (bt or "").lower()):
                await self.click(dr, r, c, label=bt)
                used = True
                break
        if not used:
            log("  ⚠️ нет кнопки «Огниво xN». Сырые кнопки: "
                + " | ".join(bt for _, _, bt in self.flat_buttons(dr)))
            return "no_use_button"
        await asyncio.sleep(0.9)
        fuse = None
        for _ in range(12):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                btns = self.flat_buttons(m)
                if not btns:
                    continue
                if "фитил" in (m.message or "").lower() or \
                        any(self.s["bomb_fuse"] in (bt or "").lower() or "🔴" in (bt or "")
                            for _, _, bt in btns):
                    fuse = m
                    break
            if fuse:
                break
            await asyncio.sleep(0.5)
        if not fuse:
            return await self._read_defuse_result()   # вдруг сразу результат
        log("  🎲 экран фитиля, кнопки: " + " | ".join(bt for _, _, bt in self.flat_buttons(fuse)))
        clicked = False
        for r, c, bt in self.flat_buttons(fuse):
            low = (bt or "").lower()
            if self.s["bomb_fuse"] in low or "🔴" in (bt or ""):
                await self.click(fuse, r, c, label=bt)
                clicked = True
                break
        if not clicked:      # красный не нашли — режем первый «не служебный» фитиль
            for r, c, bt in self.flat_buttons(fuse):
                low = (bt or "").lower()
                if bt and not any(w in low for w in ("назад", "отмена", "закрыть")):
                    await self.click(fuse, r, c, label=bt)
                    log(f"  ⚠️ красный фитиль не найден — резал «{bt}»")
                    clicked = True
                    break
        if not clicked:
            return "no_fuse_button"
        await asyncio.sleep(1.0)
        return await self._read_defuse_result()

    async def recover_after_explosion(self, sp):
        await self.heal_territory(sp)
        await self.protect_holops(sp)
        log("  🏁 Восстановление после взрыва завершено — возвращаюсь к набегам.")

    async def heal_territory(self, sp):
        s = self.s
        cost = s["heal_cost"]
        if sp["silver"] + cost > s["bomb_max_silver"]:
            log("  ⛔ лимит серебра — лечение территории пропускаю")
            return
        await self.ensure_silver(cost)
        await self.send("Территория")
        tmsg = None
        for _ in range(12):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if not m.out and "ТЕРРИТОРИЯ" in (m.message or "") and self.flat_buttons(m):
                    tmsg = m
                    break
            if tmsg:
                break
            await asyncio.sleep(0.5)
        if not tmsg:
            log("  ⚠️ территория не открылась для лечения")
            return
        heal = None
        for r, c, bt in self.flat_buttons(tmsg):
            low = (bt or "").lower()
            if ("лечи" in low or "восстанов" in low or "100" in (bt or "")) and STAR not in (bt or ""):
                heal = (r, c, bt)
                break
        if not heal:
            log("  ⚠️ кнопку лечения территории не нашёл. Сырые кнопки: "
                + " | ".join(bt for _, _, bt in self.flat_buttons(tmsg)))
            return
        await self.click(tmsg, heal[0], heal[1], label=heal[2])
        sp["silver"] += cost
        self.stats["spent_silver"] += cost
        log(f"  ❤️ Территория вылечена (кнопка «{heal[2]}»).")

    async def protect_holops(self, sp):
        s = self.s
        await self.send("Холопы")
        hub = None
        for _ in range(12):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if not m.out and any("холопы (" in (bt or "").lower()
                                     for _, _, bt in self.flat_buttons(m)):
                    hub = m
                    break
            if hub:
                break
            await asyncio.sleep(0.5)
        if not hub or not await self.click_text(hub, "Холопы (", label="список холопов"):
            log("  ⚠️ не открыл список холопов для защиты")
            return
        await asyncio.sleep(0.8)
        lst = None
        for _ in range(12):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if not m.out and "холоп" in (m.message or "").lower() and self.flat_buttons(m):
                    lst = m
                    break
            if lst:
                break
            await asyncio.sleep(0.5)
        if not lst:
            log("  ⚠️ список холопов не открылся")
            return
        prot = None
        for r, c, bt in self.flat_buttons(lst):
            low = (bt or "").lower()
            if (("защитить всех" in low) or ("охрана всем" in low)
                    or ("охран" in low and "всех" in low)) and STAR not in (bt or ""):
                prot = (r, c, bt)
                break
        if not prot:
            log("  ⚠️ кнопки «Защитить всех» за золото нет (охрана могла уцелеть). Сырые кнопки: "
                + " | ".join(bt for _, _, bt in self.flat_buttons(lst)))
            return
        cost = parse_amount(prot[2])
        if cost and sp["gold"] + cost > s["bomb_max_gold"]:
            log(f"  ⛔ защита всех стоит {cost}🏅 — превышает лимит инцидента, пропускаю")
            return
        if cost:
            await self.ensure_gold(cost)
        await self.click(lst, prot[0], prot[1], label=prot[2])
        sp["gold"] += cost
        self.stats["spent_gold"] += cost
        log(f"  🛡️ Холопы защищены (кнопка «{prot[2]}»).")

    def heartbeat(self):
        now = time.time()
        if now - self._last_heartbeat < self.s["heartbeat_min"] * 60:
            return
        self._last_heartbeat = now
        st = self.stats
        log(f"❤️‍🔥 Сводка: ударов {st['hits']}, побед {st['wins']}, "
            f"пробиваю {st['blocked']}, отбито {st['loss']}, "
            f"🪙 награблено {st['loot']:,}, 📈 репутация +{st['rep']:.0f}".replace(",", " ")
            + (f", 💣 бочек {st['bombs']} (разминир. {st['defused']}, взрывов {st['exploded']})"
               if st['bombs'] else ""))

    def report(self):
        """Итоговый общий отчёт (пишется при остановке)."""
        st = self.stats
        dur = time.time() - self._started if self._started else 0
        benched = self.load_benched()
        on_bench = [t for t in self.load_targets() if norm(t) in benched]
        log("═════════ ИТОГОВЫЙ ОТЧЁТ ═════════")
        log(f"  ⏱️ Длительность: {fmt_secs(dur)}")
        log(f"  ⚔️ Ударов: {st['hits']}  |  🏆 Побед: {st['wins']}  |  "
            f"🧱 Пробивал: {st['blocked']}  |  ❌ Поражений: {st['loss']}")
        log(f"  🪙 Награблено серебра: {st['loot']:,}".replace(",", " "))
        log(f"  📈 Репутация заработана: +{st['rep']:.1f}")
        if st.get("bombs"):
            log(f"  💣 Бочек прилетело: {st['bombs']}  |  🔧 Разминировано: {st['defused']}  |  "
                f"💥 Взрывов: {st['exploded']}")
            log(f"  💸 Потрачено на оборону: {st.get('spent_gold', 0)}🏅 золота, "
                f"{st.get('spent_silver', 0):,}🪙 серебра".replace(",", " "))
        if on_bench:
            log(f"  🪑 На скамейке (снятые за поражения): {', '.join(on_bench)}")
        log("══════════════════════════════════")

    # ---------- главный вечный цикл ----------
    async def run(self):
        s = self.s
        self._started = time.time()
        self.targets = self.load_targets()
        # скамейка действует ТОЛЬКО в пределах одной сессии — на старте чистим.
        # (проиграл в этот запуск → снял; стоп/старт → снова пробуем этих же)
        try:
            open(self.bench_path, "w", encoding="utf-8").close()
        except OSError:
            pass
        log(f"🎯 Цели ({len(self.targets)}): " + ", ".join(self.targets))
        log(f"⚙️  КД {s['attack_cd']}с +{s['jitter_lo']}–{s['jitter_hi']}с, "
            f"мой стоп-HP {s['my_min_hp']}, лечусь до {s['my_recover_to']}, "
            f"реген {s['min_per_hp']}м/HP")
        log(f"🎛️  Пульт: {self.control_path}  (run / pause / stop)")
        benched = self.load_benched()
        if benched:
            on_bench = [t for t in self.targets if norm(t) in benched]
            log(f"🪑 На скамейке (после поражений, не бью): {', '.join(on_bench) or '—'} "
                f"— вернуть: убрать из {os.path.basename(self.bench_path)}")
        while True:
            if await self.gate() == "stop":
                break
            try:
                if await self._one_cycle() == "stop":
                    break
            except Exception as e:
                log(f"  ⚠️ сбой в цикле: {type(e).__name__}: {e} — продолжаю через 15с")
                try:
                    if not self.c.is_connected():
                        await self.c.connect()
                except Exception:
                    pass
                if await self.sleep_gated(15) == "stop":
                    break

    async def _one_cycle(self):
        """Один проход главного цикла. Вернуть 'stop' если пульт попросил остановиться, иначе None."""
        self.apply_live_settings()   # подхватываем настройки боя из панели на лету
        s = self.s
        self.heartbeat()
        # 💣 ПРИОРИТЕТ №1: проверка на бочку (динамит). Важнее набегов и лечения.
        try:
            if await self.check_and_handle_bomb():
                return None
        except Exception as e:
            log(f"  ⚠️ сбой в проверке бочки: {type(e).__name__}: {e}")
        # РЕЖИМ ЛЕЧЕНИЯ: не атакуем, но КАЖДЫЙ РАЗ читаем реальное HP (Территория).
        # Просыпаемся сразу, как только HP дорос до recover_to (в т.ч. после эликсира).
        if self._healing:
            hp = await self.my_current_hp()
            cap = (s["my_recover_to"] * s["min_per_hp"] * 60) + 600   # аварийный потолок сна
            if hp is not None and hp >= s["my_recover_to"]:
                self._healing = False
                log(f"❤️ HP восстановлено ({hp}) — продолжаю набеги.")
            elif time.time() - self._heal_start > cap:
                self._healing = False
                log("❤️ Потолок лечения истёк — пробую продолжить (проверю HP в бою).")
            else:
                rem = max(1.0, (s["my_recover_to"] - (hp or 0)) * s["min_per_hp"])
                shown = str(hp) if hp is not None else "?"
                nap = heal_recheck_secs(s)
                log(f"🩶 Лечусь: HP {shown}, до {s['my_recover_to']} ~{rem:.0f}м — перечитаю через {nap:.0f}с")
                return await self.sleep_gated(nap)
        arena = await self.open_arena()
        if not arena:
            log("  ⚠️ арена не открылась — пробую снова через 20с")
            return await self.sleep_gated(20)
        my_hp = parse_my_low_hp(arena.message or "")   # берём Жизни/Здоровье
        if my_hp is None:
            my_hp = await self.my_current_hp()          # не бомбим вслепую — читаем с Территории
        if my_hp is not None and my_hp <= s["my_min_hp"]:
            self._healing = True
            self._heal_start = time.time()
            log(f"🩸 Мои HP {my_hp} ≤ {s['my_min_hp']} — ухожу на лечение до {s['my_recover_to']} "
                f"(буду перечитывать HP ~каждые {s['heal_recheck']}с).")
            return None

        self.targets = self.load_targets()   # подхватываем правки списка из панели на лету
        benched = self.load_benched()
        active = [t for t in self.targets if norm(t) not in benched]
        if not active:
            log("🪑 Все цели на скамейке / список пуст — жду распоряжения (верни кого-то или добавь цель)")
            return await self.sleep_gated(60)

        now = time.time()
        eligible = [t for t in active if self.next_ok.get(t, 0.0) <= now]
        if not eligible:
            soonest = min(self.next_ok.get(t, 0.0) for t in active)
            # потолок сна 120с — чтобы проверка на бочку срабатывала не реже ~2 мин
            nap = max(5.0, min(soonest - now, 120.0))
            log(f"⏳ Все цели на КД — сплю {fmt_secs(nap)} (мои HP {my_hp})")
            return await self.sleep_gated(nap)

        log(f"── Проход: доступно целей {len(eligible)}, мои HP {my_hp}")
        for t in eligible:
            if self.control_state() != "run":
                return None   # пульт переключили — уходим на gate() в начале цикла
            my_after = await self.do_target(t)
            if isinstance(my_after, int) and my_after <= s["my_min_hp"]:
                log(f"🩸 После удара мои HP {my_after} ≤ {s['my_min_hp']} — прерываю проход на лечение")
                return None
            await self.inter_hit()
        return None

    # ---------- разовая разведка (ничего не бьёт) ----------
    async def selftest(self):
        log("🔎 SELFTEST — читаю состояние целей, НЕ атакую.")
        arena = await self.open_arena()
        if not arena:
            log("  ⚠️ арена не открылась")
            return
        my_hp = parse_my_hp(arena.message or "")
        my_atk = parse_my_attack(arena.message or "")
        log(f"  Я: ⚔️ атака →{my_atk}, ❤️ HP {my_hp}/100")
        for name in self.targets:
            res = await self.arena_search(name)
            if not res:
                log(f"  ⁇ {name}: не найден")
                continue
            blocks = parse_arena_targets(res.message or "")
            positions = target_positions(self.flat_buttons(res))
            idx = exact_target(blocks, name)
            if idx is None or idx >= len(positions):
                log(f"  ⁇ {name}: нет строгого совпадения (блоков {len(blocks)}, кнопок {len(positions)})")
                continue
            b = blocks[idx]
            btn = positions[idx][2]
            if button_attackable(btn):
                verdict = "БЬЁТСЯ" if (b.get("hp") or 0) > self.s["tgt_min_hp"] else f"HP низкий ({b.get('hp')})"
                log(f"  ✅ {name}: {verdict} — HP {b.get('hp')}, защ.→{b.get('defense')}, ур.{b.get('level')}")
            else:
                reason = classify_block_reason(btn)
                extra = ""
                icon = "🛡️"
                if reason == "shield":
                    secs = await self.shield_seconds(name)
                    extra = f", щит ещё ~{fmt_secs(secs)}" if secs else ", таймер щита не прочитан"
                elif reason == "weak":
                    icon = "💤"
                    extra = f" (HP {b.get('hp')})"
                elif reason in ("clan", "level"):
                    icon = "🚫"
                log(f"  {icon} {name}: не атакуется («{btn.strip()}» → {reason}){extra}")
            await self.inter_hit()
        log("🔎 SELFTEST завершён.")


# ════════════════════════════════════════════════════════════════════════════
async def main():
    ap = argparse.ArgumentParser(description="Авто-бой набегов по фикс-списку (@holop)")
    ap.add_argument("--dry-run", action="store_true", help="крутить цикл, но не жать «Атаковать»")
    ap.add_argument("--selftest", action="store_true", help="разово показать состояние целей и выйти")
    args = ap.parse_args()

    setup_logging()
    cfg = load_config()
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        log("Заполни api_id/api_hash в config.json.")
        sys.exit(1)

    if cfg.get("session_string"):
        client = TelegramClient(StringSession(cfg["session_string"]), int(cfg["api_id"]), cfg["api_hash"])
    else:
        session = os.path.join(HERE, cfg.get("session_name", "holop_session"))
        client = TelegramClient(session, int(cfg["api_id"]), cfg["api_hash"])
    await client.start()
    me = await client.get_me()
    mode = "SELFTEST" if args.selftest else ("DRY-RUN" if args.dry_run else "БОЕВОЙ")
    log(f"[{datetime.now():%H:%M:%S}] Вошёл как {me.first_name}. Режим: {mode}")

    bot = Smasher(client, cfg, args)

    # Мягкая остановка по SIGTERM/SIGINT: пишем 'stop' в пульт, чтобы бот доиграл
    # текущее действие, корректно вышел через gate() и НАПЕЧАТАЛ итоговый отчёт.
    def _soft_stop():
        log("📴 Получен сигнал остановки — доигрываю и печатаю отчёт…")
        try:
            with open(bot.control_path, "w", encoding="utf-8") as f:
                f.write("stop")
        except OSError:
            pass
    loop = asyncio.get_running_loop()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(_sig, _soft_stop)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        if args.selftest:
            await bot.selftest()
        else:
            await bot.run()
    except KeyboardInterrupt:
        log("⏹  Прервано с клавиатуры.")
    finally:
        if not args.selftest:
            bot.report()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
