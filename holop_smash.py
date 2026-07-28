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
TARGETS = []  # пусто: свежая установка не бьёт никого, пока не впишешь

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
    "defended_retry": 30,       # мин, на сколько отложить цель с ров/частокол, если «не пробивать»
    "clan_level_retry": 120,    # мин для «свой клан» / «ниже ур.» (само не изменится)
    "inter_hit_lo": 4,          # пауза между разными целями (низ), сек
    "inter_hit_hi": 10,         # пауза между разными целями (верх), сек
    "control_file": "smash_control.txt",
    "heartbeat_min": 15,        # как часто писать в лог сводку, минут
    "heal_recheck": 180,        # базовый интервал перечитывания HP в лечении, сек (было 90 — с запасом)
    "heal_recheck_jitter": 40,  # ± случайные секунды к интервалу перечитывания HP
    # ── защита от бочки (динамита) ──
    # 0 = НЕ опрашивать «Дружину» по таймеру (не палиться): ловим бочку по пушу
    #     «скоро взорвётся» + один чек при старте. >0 = ещё и опрос раз в N сек (для ночи).
    "bomb_poll_interval": 0,
    "bomb_check_interval": 75,  # (устар.) старый таймер опроса — не используется при poll=0
    "bomb_fuse": "красн",       # какой фитиль резать (подстрока, регистр не важен)
    "ognivo_cost": 900,         # цена Огнива, золото
    "heal_cost": 100000,        # лечение территории после взрыва, серебро
    "kazna_gold_buffer": 3000,  # сколько золота снимать с казны с запасом
    "kazna_silver_buffer": 50000,  # запас серебра при снятии
    "bomb_max_gold": 12000,     # ЖЁСТКИЙ потолок трат золота на ОДИН взрыв-инцидент
    "bomb_max_silver": 250000,  # ЖЁСТКИЙ потолок трат серебра на один инцидент
}


# ⚔️ РЕЖИМ «ВОЙНА» — держать цели прижатыми: бить сразу, как только можно.
# Переопределяет тайминги на агрессивные. ВНИМАНИЕ: это максимальное палево —
# запросов к игре кратно больше. Включать осознанно, галочкой.
WAR_OVERRIDES = {
    "jitter_lo": 1, "jitter_hi": 4,          # бьём почти ровно по истечении КД
    "inter_hit_lo": 1.5, "inter_hit_hi": 4,  # короткая пауза между целями
    "shield_default_retry": 3,               # щит могут снять раньше — перепроверяем часто
    "weak_retry": 3,                         # цель «слаба» — вернуться быстро
    "notfound_retry": 2,
    "defended_retry": 5,
}
WAR_NAP_CAP = 30.0        # максимум сна, когда все цели на КД (обычный режим — 120с)
WAR_NAP_FLOOR = 2.0       # минимум сна (обычный — 5с)
WAR_WEAK_CAP = 90.0       # максимум ожидания регена цели, сек — дальше перепроверим вживую
WAR_SHIELD_PAD = 3        # через сколько сек после конца щита пробовать (обычный — 30)

# 🎯 СВОБОДНАЯ ОХОТА — бить слабейших по защите с арены, без фикс-списка.
# Каждый проход: сортируем арену «Защита ▲» (слабые первыми), листаем несколько
# страниц и собираем ники доступных к атаке целей — а бьём их обычным do_target()
# (то же лечение/статистика/КД/ров-частокол). Ограничения — чтобы проход был конечным.
HUNT_MAX_PAGES = 5        # сколько страниц арены пролистать за проход (соберём пул атакуемых)
HUNT_MAX_HITS = 10        # сколько целей набрать за проход (потом новый цикл, свежая выборка)


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
BOMB_NOTIF = "ЗАМИНИРОВАН"                    # ловит и «ЗАМИНИРОВАНА», и «💣 ЗАМИНИРОВАНО!»
BOMB_WARN = "скоро взорвётся"                 # поздний пуш «Бочка ... скоро взорвётся»
ATTACK_MARKER = "НА ТЕБЯ НАПАЛИ"              # пуш о набеге на меня → ров/частокол могли сгореть


async def rsleep(base, spread=0.35):
    """Пауза с рандомом ±spread (деф ±35%). Антипалево: ровные машинные интервалы —
    первое, что видно со стороны игры. Везде, где раньше был фиксированный sleep."""
    lo, hi = base * (1.0 - spread), base * (1.0 + spread)
    await asyncio.sleep(random.uniform(max(0.05, lo), hi))
# ⚠️ НЕ добавлять сюда «правильный фитиль»! Текст ВЗРЫВА — «Ты выбрал НЕправильный фитиль»
# СОДЕРЖИТ подстроку «правильный фитиль» → взрыв ловился как обезврежено (ложный успех,
# восстановление не запускалось). Успех определяем по «обезврежена»/«в безопасности».
DEFUSED_WORDS = ("обезврежена", "в безопасности")
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
# донатная физическая защита (Железный Купол / Стена) — жрёт требушеты, бить бессмысленно
DONATE_RE = re.compile(r"железн\w*\s+купол|требушет\w*\s+остал", re.IGNORECASE)


def _is_duplicate_ip(e):
    """Сессия закрыта из-за ДВУХ IP разом (VPN сменил IP/страну на ходу). ЧАСТО транзиентно —
    после переподключения с одного IP снова работает. НЕ убивать бота сразу (боль Ксюши)."""
    n = type(e).__name__.lower()
    s = str(e).lower()
    return "authkeyduplicated" in n or "two different ip" in s


def _is_dead_session(e):
    """Сессия убита Telegram (ключ отозван / дубль IP) — во ВНУТРЕННИХ обработчиках
    пробрасываем наверх. В главном цикле дубль-IP ловится ОТДЕЛЬНО (переподключение),
    а по-настоящему мёртвая (ключ отозван) — остановка."""
    n = type(e).__name__.lower()
    s = str(e).lower()
    return (_is_duplicate_ip(e) or "authkeyunregistered" in n
            or "sessionrevoked" in n or "sessionexpired" in n
            or "userdeactivated" in n or "authorization key" in s)


def parse_shop_gold(text):
    """Золото с экрана Магазина: «🏅 1663 золота» → 1663. None если не нашли."""
    m = re.search(r"🏅\s*([\d\s]+?)\s*золот", text or "")
    if m:
        try:
            return int(m.group(1).replace(" ", ""))
        except ValueError:
            return None
    return None


# предметы обороны: держим активными + запас (покупка за золото 🏅, не за ⭐)
# 🐴 ОБОЗ — +% серебра с набегов на время. Берём «+50% · 50м — 400».
# ВНИМАНИЕ: на кнопке нарисовано серебро 🪙, но игра списывает ЗОЛОТО 🏅 (баг игры,
# проверено вживую). Поэтому баланс проверяем и добираем ЗОЛОТОМ.
OBOZ_PICK = ("+50%", "50м")      # кнопку ищем по ОБЕИМ подстрокам
OBOZ_COST = 400                  # 🏅 ЗОЛОТА (не серебра, несмотря на иконку)
OBOZ_MINUTES = 50                # сколько живёт

DEFENSE_ITEMS = [
    {"name": "Частокол", "price": 350, "reserve": 2},   # блок 3 набегов / 24ч
    {"name": "Ров", "price": 100, "reserve": 2},          # блок 1 набег
]


def parse_regen_bonus(text):
    """Суммарный бонус регена HP (%) с экрана «Территория». База = 1 HP/мин (60 с/HP).
    ⚠️ КЛЮЧЕВОЕ (Максим, инфа 100%): «❤️‍🩹 Регенерация: +50%» и «🎨 ❤️ Сердце: +50%» —
    это ОДИН И ТОТ ЖЕ бонус от иконки сердца, игра выводит его ДВАЖДЫ. Считать ОДИН РАЗ!
    Раньше складывал оба → врал +127%; на деле +77% (сердце 50 + княжество 6 + герои 21).
    Итого: sec/HP = 60 / (1 + bonus/100). Дубли иконки сердца не удваиваем."""
    t = text or ""
    # бонус от сердца — показан как «Регенерация: +N%» И как «Сердце: +N%». Берём ОДИН раз.
    heart = 0.0
    m = re.search(r"Регенерац\w*[:\s]*\+?(\d+)\s*%", t)
    if m:
        heart = max(heart, float(m.group(1)))
    m = re.search(r"Сердце[:\s]*\+?(\d+)\s*%", t)
    if m:
        heart = max(heart, float(m.group(1)))
    total = heart
    for m in re.finditer(r"\+?(\d+)\s*%\s*реген", t):     # «+6% реген» (княжество), «+21% реген» (герои)
        total += float(m.group(1))
    return total
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


def parse_rep_penalty(text):
    """Списание репутации за атаку. Возвращает ОТРИЦАТЕЛЬНОЕ число ТОЛЬКО если игра
    явно сняла репутацию — то есть рядом со словом «репутаци» стоит число с минусом
    («-5 репутации», «Репутация: -5», «репутация −3»). Плюсовая или нулевая репутация
    штрафом НЕ считается (её как раз копим) → 0.0.

    Никаких «ключевых слов без числа»: в тексте победы всегда есть «❤️ Потери в бою: N»,
    и на подстроку «потер» ловились обычные победы (Миру мир, лплудвж). Бенчим строго
    по минусу у самой репутации."""
    t = text or ""
    low = t.lower()
    if "репутаци" not in low:
        return 0.0
    for m in re.finditer(r"репутаци\w*", low):
        # число ВПЛОТНУЮ перед словом: «-5 репутации», «📈 +1 репутация»
        before = low[max(0, m.start() - 12): m.start()]
        mb = re.search(r"([+\-−])\s*(\d+(?:[.,]\d+)?)\s*$", before)
        if mb and mb.group(1) in "-−":
            return -float(mb.group(2).replace(",", "."))
        # число ВПЛОТНУЮ после слова: «Репутация: -8», «репутация −3»
        after = low[m.end(): m.end() + 12]
        ma = re.search(r"^\W{0,4}([+\-−])\s*(\d+(?:[.,]\d+)?)", after)
        if ma and ma.group(1) in "-−":
            return -float(ma.group(2).replace(",", "."))
    return 0.0


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
        self.donate_path = os.path.join(HERE, "smash_donate.txt")   # цели с донат-защитой (Купол/Стена) — не бьём
        self.targets_path = os.path.join(HERE, "smash_targets.txt")  # редактируемый список целей
        self.settings_path = os.path.join(HERE, "smash_settings.json")  # живые настройки из панели
        self.oboz_path = os.path.join(HERE, "oboz_state.json")  # когда истекает обоз (без лишних запросов)
        self._default_targets = list(cfg.get("smash_targets") or TARGETS)
        self.targets = self.load_targets()
        self.ensure_targets_file()
        # пауза между действиями (запросами к игре). Раньше 0.8–1.8с — Максим: машинно
        # быстро, палево («5±2с, я не робот»). Деф человечнее 1.5–3.5с, крутится из панели.
        self.lo = float(cfg.get("min_delay", 1.5))
        self.hi = float(cfg.get("max_delay", 3.5))
        # состояние
        self._cd_cache_path = os.path.join(HERE, "cd_cache.json")  # КД/щиты целей — переживают перезапуск
        self.next_ok = self._load_cd_cache()   # имя -> epoch, когда цель снова доступна (из файла)
        self._cd_saved_at = 0.0
        self.stats = {"hits": 0, "wins": 0, "blocked": 0, "loss": 0, "loot": 0, "rep": 0.0}
        self._paused_note = False
        self._last_heartbeat = 0.0
        self._started = 0.0      # время старта боевой сессии (для итогового отчёта)
        self.peer = None   # кэш entity бота (резолвим один раз)
        self._healing = False    # режим лечения: не атакуем, перечитываем реальное HP
        self._regen_bonus = 0.0   # сумма бонусов регена с Территории (%), обновляется при чтении
        self._heal_start = 0.0   # когда ушли на лечение (для аварийного потолка)
        self._heal_from_hp = 0    # HP на момент последнего РЕАЛЬНОГО чтения при лечении
        self._heal_from_t = 0.0   # когда это чтение было (для оценки HP без запроса)
        self._last_hp_read = 0.0  # когда последний раз слали «Территория» ради HP (анти-спам)
        self._last_raw = ""            # сырой текст последнего ответа набега
        self._last_rep_penalty = 0.0   # < 0, если за последнюю атаку списали репутацию
        self._last_bomb_check = 0.0   # когда последний раз опрашивали «Дружину» на бочку
        self._regen_auto = False      # авто-реген: считать сек/HP по бонусам с главной
        self._auto_kazna = False      # авто-казна: собирать доход + депозит + реинвест
        self._last_bank = 0.0         # когда последний раз собирали казну
        self._next_bank = 0.0         # когда следующий раз по таймеру (раз в ~час)
        self._auto_defense = False    # авто-оборона: ров+частокол активны + запас
        self._next_defense = 0.0      # когда следующий раз проверять оборону
        self._last_attack_id = 0      # id последнего замеченного пуша «НА ТЕБЯ НАПАЛИ»
        self._auto_oboz = False       # авто-покупка обоза (+50% серебра с набегов)
        self._war_mode = False        # ⚔️ режим войны: бить по КД без пауз, держать цели прижатыми
        self._human_mode = False      # 🧑 человеческий ритм: иногда «отходит» на перерыв (не замена 24/7!)
        self._next_human_break = 0.0
        self._notify_dm = False       # 🔔 слать себе в Избранное о критичном (по галочке, деф ВЫКЛ)
        self._notify_sent = {}        # ключ события -> когда слали (троттлинг)
        self._oboz_until = 0.0        # до какого времени действует обоз (из oboz_state.json)
        self._last_hit_name = None    # последняя обработанная цель — продолжить список отсюда
        self._pierce_defenses = True  # пробивать ров/частокол (True) или пропускать (False)
        self._free_hunt = False       # 🎯 свободная охота: бить слабых по защите с арены, без списка
        self._hit_shields = True      # сносить донат-щит требушетом и фармить дальше (True) или беречь требушеты и скипать (False)
        self._last_tl_warn = 0.0      # троттл лога про нераспознанные анимации @holop
        self._bomb_alert_until = 0.0  # до этого времени — тревога бочки: долбим «Дружину» каждый цикл
        self._bomb_done = set()       # id уже обработанных нотификаций бочки (не дублировать)
        self._last_bomb_scan = 0.0    # когда последний раз сканировали бочку внутри прохода
        self._bomb_defense = True     # 🛡️ защищаться от бочек во время набегов (галочка, деф ВКЛ)
        self._defense_only = False    # 🛡️ режим ТОЛЬКО защита от бочек: не фармим, только сторожим бочку
        # ВАЖНО: только ТЕПЕРЬ, когда все флаги проинициализированы, читаем настройки
        # из панели. Раньше вызов стоял ВЫШЕ и блок состояния затирал флаги обратно в
        # False — из-за этого на старте не работал авто-реген (реген оставался 1.0 м/HP).
        self.apply_live_settings()
        self.stats.update({"bombs": 0, "defused": 0, "exploded": 0,
                           "spent_gold": 0, "spent_silver": 0})

    # ---------- список целей (файл smash_targets.txt) ----------
    # ---------- КЭШ КД/ЩИТОВ ЦЕЛЕЙ (переживает перезапуск — анти-палево) ----------
    def _load_cd_cache(self):
        """Загрузить КД целей из файла. Без него после перезапуска бот считал ВСЕ цели
        свободными и делал залповый опрос всего списка (пик палева). Грузим только
        БУДУЩИЕ КД (истёкшие не нужны — цель и так доступна)."""
        try:
            with open(self._cd_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            out = {}
            for name, ts in (data.get("next_ok") or {}).items():
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    continue
                if ts > now:                       # ещё на КД/щите — помним
                    out[name] = min(ts, now + 30 * 86400)   # потолок 30 сут (бенч/донат не тащим)
            if out:
                log(f"⏳ Кэш КД: подхватил {len(out)} целей на кулдауне — не долблю их зря после старта.")
            return out
        except (OSError, ValueError, TypeError):
            return {}

    def _save_cd_cache(self, force=False):
        """Сохранить будущие КД в файл. Троттлинг: не чаще раза в 30с (диск не насилуем)."""
        now = time.time()
        if not force and now - self._cd_saved_at < 30:
            return
        self._cd_saved_at = now
        try:
            fut = {k: round(v, 1) for k, v in self.next_ok.items()
                   if v > now and v < now + 30 * 86400}   # только реальные КД, без бенч-заглушек 10^9
            with open(self._cd_cache_path, "w", encoding="utf-8") as f:
                json.dump({"next_ok": fut, "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False)
        except OSError:
            pass

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
        ints = {"my_min_hp", "my_recover_to", "bomb_poll_interval"}
        for k in ("my_min_hp", "my_recover_to", "min_per_hp",
                  "attack_cd", "jitter_lo", "jitter_hi", "bomb_poll_interval"):
            if k in data:
                try:
                    self.s[k] = int(data[k]) if k in ints else float(data[k])
                except (TypeError, ValueError):
                    pass
        # реген: ползунок «сек на 1 HP» → min_per_hp (мин на 1 HP). Если авто — не перетираем.
        self._regen_auto = bool(data.get("regen_auto", getattr(self, "_regen_auto", False)))
        if not self._regen_auto and "sec_per_hp" in data:
            try:
                self.s["min_per_hp"] = max(1, int(data["sec_per_hp"])) / 60.0
            except (TypeError, ValueError):
                pass
        # флаги авто-казны и авто-обороны
        # пауза между действиями (анти-палево) — крутится из панели на лету
        try:
            lo = float(data.get("req_delay_lo", self.lo))
            hi = float(data.get("req_delay_hi", self.hi))
            if 0.2 <= lo <= hi <= 20:
                self.lo, self.hi = lo, hi
        except (TypeError, ValueError):
            pass
        self._auto_kazna = bool(data.get("auto_kazna", getattr(self, "_auto_kazna", False)))
        self._bank_gold = bool(data.get("bank_gold", getattr(self, "_bank_gold", False)))  # класть ли золото в казну (деф нет — только серебро)
        self._auto_defense = bool(data.get("auto_defense", getattr(self, "_auto_defense", False)))
        self._pierce_defenses = bool(data.get("pierce_defenses", getattr(self, "_pierce_defenses", True)))
        self._bomb_defense = bool(data.get("bomb_defense", getattr(self, "_bomb_defense", True)))
        self._defense_only = bool(data.get("defense_only", getattr(self, "_defense_only", False)))
        was_hunt = getattr(self, "_free_hunt", False)
        self._free_hunt = bool(data.get("free_hunt", getattr(self, "_free_hunt", False)))
        if self._free_hunt and not was_hunt:
            log("🎯 Свободная охота ВКЛ — бью слабейших по защите с арены, фикс-список не нужен.")
        elif not self._free_hunt and was_hunt:
            log("📋 Свободная охота ВЫКЛ — вернулся к списку целей.")
        self._hit_shields = bool(data.get("hit_shields", getattr(self, "_hit_shields", True)))
        self._auto_oboz = bool(data.get("auto_oboz", getattr(self, "_auto_oboz", False)))
        # ⚔️ ВОЙНА: агрессивные тайминги поверх обычных (на лету вкл/выкл)
        was_war = getattr(self, "_war_mode", False)
        self._war_mode = bool(data.get("war_mode", False))
        if self._war_mode:
            self.s.update(WAR_OVERRIDES)
            if not was_war:
                log("⚔️⚔️ РЕЖИМ ВОЙНЫ ВКЛЮЧЁН — бью по КД без пауз, держу цели прижатыми. "
                    "Запросов к игре кратно больше (палевно).")
        elif was_war:
            for k in WAR_OVERRIDES:
                self.s[k] = DEFAULTS[k]      # вернуть спокойные тайминги
            log("🕊️ Режим войны выключен — вернул обычные тайминги.")
        # 🧑 человеческий режим — ДОПОЛНЕНИЕ, не замена: бот так же фармит (в т.ч. ночью),
        # но иногда «отходит» на перерыв, чтобы активность не была машинно-ровной сутками.
        was_human = getattr(self, "_human_mode", False)
        self._notify_dm = bool(data.get("notify_dm", False))
        self._human_mode = bool(data.get("human_mode", False))
        if self._human_mode and not was_human:
            self._next_human_break = time.time() + random.uniform(1800, 4500)  # первый через 30–75 мин
            log("🧑 Человеческий режим ВКЛ — иногда буду делать перерывы (не машинный ритм). "
                "Фармить продолжаю, в т.ч. ночью.")
        elif not self._human_mode and was_human:
            log("🤖 Человеческий режим ВЫКЛ — работаю без перерывов 24/7.")

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
        """Развести время атак: чтобы удары по разным целям не приходились на один момент."""
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
        # @holop шлёт анимации с TL-объектом, который телетон не парсит → get_messages
        # падает TypeNotFoundError на ВСЁМ окне. Обходим: пробуем окна поменьше (битое
        # сообщение часто не самое свежее — меньший лимит его исключит и чтение пройдёт).
        # Лог про это — не чаще раза в 45с, чтобы не спамить.
        for lim in (limit, 4, 2, 1):
            if lim > limit:
                continue
            try:
                return await self._net(lambda l=lim: self.c.get_messages(self.peer, limit=l)) or []
            except Exception as e:
                if type(e).__name__ != "TypeNotFoundError":
                    raise
        now = time.time()
        if now - self._last_tl_warn > 45:
            log("  🧩 @holop прислал анимацию, которую телетон не читает — пропускаю "
                "(это норма, спам подавлен)")
            self._last_tl_warn = now
        return []

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

    def target_button_datas(self, msg: Message):
        """callback-данные ведущих кнопок целей (в порядке target_positions).
        В данных зашита защита: pvp_attack_<id>_def_chastokol / _def_rov → «_def_» = ров/частокол."""
        datas = []
        if msg and msg.buttons:
            for row in msg.buttons:
                for b in row:
                    if _is_control_btn(b.text or ""):
                        return datas
                    datas.append(getattr(b, "data", None))
        return datas

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

    # ---------- ДОНАТ-ЗАЩИТЫ (файл smash_donate.txt) — Купол/Стена, не бьём ----------
    def load_donate(self):
        """Множество norm(ник) целей с донат-защитой (жрут требушеты). Постоянный список."""
        out = set()
        try:
            with open(self.donate_path, "r", encoding="utf-8") as f:
                for line in f:
                    n = line.split("#", 1)[0].strip()
                    if n:
                        out.add(norm(n))
        except OSError:
            pass
        return out

    def donate_add(self, name):
        """Занести цель в список донат-защит навсегда (пока сам не уберёшь из файла)."""
        if norm(name) in self.load_donate():
            return
        try:
            with open(self.donate_path, "a", encoding="utf-8") as f:
                f.write(name + "\n")
        except OSError as e:
            log(f"  (не смог записать донат-список: {e})")

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
            await rsleep(3)

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
            await rsleep(0.5)
        return None

    async def my_current_hp(self):
        """Прочитать МОИ HP с экрана «Территория» (❤️ Здоровье: X/100). None если не смог.
        Берём ТОЛЬКО СВЕЖИЙ ответ (id/правка ПОСЛЕ нашего запроса) — иначе хватали старое
        сообщение и «видели 78, а считали от 75» (замечание Максима)."""
        sent = await self.send("Территория")
        floor = getattr(sent, "id", 0) or 0
        for _ in range(14):
            await rsleep(0.5)
            for m in sorted(await self.recent(8), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                t = m.message or ""
                if "ТЕРРИТОРИЯ" not in t or not ("Здоровье" in t or "Жизни" in t):
                    continue
                edited = bool(getattr(m, "edit_date", None))
                if m.id > floor or edited:          # новое сообщение ИЛИ свежая правка
                    b = parse_regen_bonus(t)        # заодно освежаем бонусы регена (левелап и т.п.)
                    if b > 0 and self._regen_auto and abs(b - (self._regen_bonus or 0)) >= 1:
                        self._recompute_regen(b, quiet=True)   # бонусы сменились → пересчитать
                    return parse_my_low_hp(t)
        # свежий ответ не пришёл — вернём хотя бы последнее прочитанное (лучше, чем None)
        for m in sorted(await self.recent(8), key=lambda x: x.id, reverse=True):
            t = m.message or ""
            if not m.out and "ТЕРРИТОРИЯ" in t and ("Здоровье" in t or "Жизни" in t):
                return parse_my_low_hp(t)
        return None

    # ---------- 🎯 СВОБОДНАЯ ОХОТА (арена по умолчанию, сорт по защите) ----------
    async def _fresh_arena(self):
        """Самое свежее сообщение-арена (с кнопками). @holop на сортировку/пагинацию
        присылает НОВОЕ сообщение (не правит старое) — по старому id кнопки уже
        «протухли» (MessageIdInvalid). Поэтому всегда берём новейшее."""
        for m in sorted(await self.recent(8), key=lambda x: x.id, reverse=True):
            if m.out:
                continue
            t = m.message or ""
            if ("АРЕНА" in t or "🎯 Цели" in t) and m.buttons:
                return m
        return None

    async def _click_data(self, msg, want, *, label="", text_eq=None, wait_for=None, changed_from=None):
        """Нажать кнопку арены по её callback-данным (подстрока `want`) и вернуть
        НОВЕЙШИЙ экран арены (сорт/страница приходят новым сообщением).
        text_eq — доп. фильтр по точному тексту кнопки (для стрелки «›»).
        wait_for — ждём эту подстроку в новом тексте; changed_from — ждём, пока
        текст СТАНЕТ ОТЛИЧНЫМ от переданного (для пагинации)."""
        pos = None
        for r, row in enumerate(msg.buttons or []):
            for c, b in enumerate(row):
                d = getattr(b, "data", None)
                if not d or want not in d:
                    continue
                if text_eq is not None and (b.text or "").strip() != text_eq:
                    continue
                pos = (r, c)
                break
            if pos:
                break
        if pos is None:
            return None
        await self.click(msg, pos[0], pos[1], label=label)
        if self.dry:
            return msg
        for _ in range(14):
            await rsleep(0.4)
            m = await self._fresh_arena()
            if not m:
                continue
            t = m.message or ""
            if wait_for is not None and wait_for not in t:
                continue
            if changed_from is not None and t == changed_from:
                continue
            return m
        return await self._fresh_arena()

    async def pick_hunt_names(self, arena):
        """Выбрать ники для свободной охоты: слабейшие по защите АТАКУЕМЫЕ соперники.
        Тонкость (проверено вживую): игровые сорты «по защите ▲»/«уровень ▲» упираются
        в защищённых новичков «ниже 6 ур.» (их сотни — атаковать нельзя). Поэтому сорт
        «Уровень ▼» (соперники твоего уровня = атакуемые), собираем их со страниц и
        ранжируем по защите ЛОКАЛЬНО — слабейшие первыми. Клан/купол/КД, донат-щиты,
        скамейку и (если «пробивать» выкл) ров/частокол — пропускаем."""
        skip = self.load_donate() | self.load_benched()
        msg = await self._click_data(arena, b"pvp_sort_level_high",
                                     label="сорт: уровень ▼", wait_for="Уровень ▼") or arena
        pool, seen = [], set()                    # (защита, ник) атакуемых
        for page in range(HUNT_MAX_PAGES):
            blocks = parse_arena_targets(msg.message or "")
            positions = target_positions(self.flat_buttons(msg))
            datas = self.target_button_datas(msg)
            for i, b in enumerate(blocks):
                if i >= len(positions):
                    break
                nm = b.get("name") or ""
                key = norm(nm)
                if not nm or key in seen:
                    continue
                if not button_attackable(positions[i][2]):
                    continue                      # клан / купол / «ниже N ур.» / уже в КД
                if key in skip:
                    continue                      # донат-щит / скамейка
                if not self._pierce_defenses and i < len(datas) and datas[i] and b"_def_" in datas[i]:
                    continue                      # ров/частокол, «пробивать» выкл
                hp = b.get("hp")
                if hp is not None and hp <= self.s["tgt_min_hp"]:
                    continue                      # слишком слаба — не набьём лут
                seen.add(key)
                defv = b.get("defense")
                pool.append((defv if defv is not None else 10 ** 9, nm))
            nxt = await self._click_data(msg, b"pvp_page_", label="арена: стр. →",
                                         text_eq="›", changed_from=msg.message)
            if nxt is None:
                break                             # стрелки «›» нет — страницы кончились
            msg = nxt
        pool.sort(key=lambda x: x[0])             # слабейшие по защите — первыми (легче добить, реже блок)
        return [nm for _, nm in pool[:HUNT_MAX_HITS]]

    async def open_territory(self):
        """Открыть «Территория» и вернуть сообщение (со статами и кнопкой «Собрать»). None если не смог."""
        await self.send("Территория")
        for _ in range(12):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                if "ТЕРРИТОРИЯ" in (m.message or ""):
                    return m
            await rsleep(0.5)
        return None

    # ---------- РЕГЕН: простая формула, база ИЗВЕСТНА = 60 с/HP (1 HP/мин) ----------
    # Максим (инфа 100%): база регена = 1 HP за 60с. Бонусы складываются, «Сердце» =
    # «Регенерация» (один бонус, показан дважды) — считаем ОДИН раз (см. parse_regen_bonus).
    # sec/HP = 60 / (1 + бонусы/100). Никакого обучения базы не нужно — всё детерминировано.
    REGEN_BASE = 60.0

    def _apply_sec_per_hp(self, sec):
        """Записать сек/HP в рабочее значение И в поле настроек (видно в панели)."""
        self.s["min_per_hp"] = sec / 60.0
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("sec_per_hp") != int(round(sec)):
                data["sec_per_hp"] = int(round(sec))
                with open(self.settings_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except (OSError, ValueError):
            pass

    def _recompute_regen(self, bonus=None, quiet=False):
        """sec/HP = 60 / (1 + бонусы/100). Вызывается на старте и при смене бонусов
        (левелап/новые герои — их ловим при каждом чтении Территории)."""
        if bonus is not None:
            self._regen_bonus = bonus
        b = self._regen_bonus or 0.0
        sec = max(5.0, min(self.REGEN_BASE / (1.0 + b / 100.0), 600.0))
        self._apply_sec_per_hp(sec)
        if not quiet:
            log(f"🔄 Реген: база 60 с/HP × бонусы +{b:.0f}% → ~{sec:.0f} сек/HP "
                f"(Сердце и Регенерация — один бонус, считаю один раз).")
        return sec

    async def update_regen_from_main(self):
        """Авто-реген на старте: прочитать бонусы с Территории и посчитать сек/HP.
        Хочешь своё число — выключи галочку авто-регена и впиши руками."""
        if not self._regen_auto:
            return
        terr = await self.open_territory()
        bonus = parse_regen_bonus(terr.message or "") if terr else 0.0
        self._recompute_regen(bonus if bonus > 0 else 0.0)

    # ---------- АВТО-КАЗНА (доход → депозит → реинвест) ----------
    async def collect_and_bank(self):
        """Собрать доход (Территория+Холопы) → положить всё в казну → реинвест (серебро+золото)."""
        log("🏦 Авто-казна: собираю доход и несу в казну…")
        terr = await self.open_territory()
        if terr:
            await self.click_text(terr, "Собрать", label="Собрать доход (серебро)")
            await rsleep(1.0)
        await self.send("Холопы")
        hol = await self.wait_text("Холопы")
        if hol:
            await self.click_text(hol, "Собрать", label="Собрать золото")
            await rsleep(1.0)
        await self._bank_currency("Серебро")
        if getattr(self, "_bank_gold", False):
            await self._bank_currency("Золото")   # только если включена галочка «класть золото в казну»
        else:
            log("  💰 золото НЕ кладу в казну (галочка выкл) — оставляю свободным на оборону/покупки")
        self._last_bank = time.time()
        self._next_bank = time.time() + 3600 + random.uniform(-600, 600)   # раз в ~час ± 10 мин
        log("🏦 Авто-казна: готово.")

    async def _bank_currency(self, kind):
        """Положить всё в депозит валюты и реинвестировать. kind: 'Серебро' / 'Золото'."""
        await self.send("Личная казна")
        kazna = await self.wait_text("Личная казна")
        if not kazna:
            log(f"  ⚠️ казна не открылась ({kind})")
            return
        if not await self.click_text(kazna, kind, label=f"Казна: {kind}"):
            log(f"  ⚠️ нет кнопки «{kind}» в казне")
            return
        dep = await self.wait_text("ДЕПОЗИТ")
        if not dep:
            log(f"  ⚠️ экран депозита ({kind}) не открылся")
            return
        if await self.click_text(dep, "Депозит", label=f"Депозит {kind}"):
            sub = await self.wait_text("умму для внесения")   # «Выберите сумму для внесения»
            if sub:
                if not await self.click_text(sub, "Положить всё", label="Положить всё"):
                    await self.click_text(sub, "Назад", label="Назад")
                await rsleep(1.0)
        dep2 = await self.wait_text("ДЕПОЗИТ")
        if dep2:
            await self.click_text(dep2, "Реинвест", label=f"Реинвест {kind}")
            await rsleep(0.8)

    # ---------- АВТО-ОБОРОНА (ров + частокол: держать активными + запас) ----------
    async def _collect_holop_gold(self):
        """Собрать золото с холопов (когда не хватает на оборону)."""
        await self.send("Холопы")
        hol = await self.wait_text("Холопы")
        if hol:
            await self.click_text(hol, "Собрать", label="Собрать золото")
            await rsleep(1.0)

    async def _reopen_defense(self):
        """Магазин → Средства обороны, вернуть сообщение экрана обороны."""
        await self.send("Магазин")
        shop = await self.wait_text("Магазин")
        if not shop:
            return None
        await self.click_text(shop, "Средства обороны", label="Средства обороны")
        return await self.wait_text("Средства обороны")

    def _defense_state(self, dmsg, name):
        """(active, stock, activate_btn, buy_btn) для предмета обороны по имени."""
        active, stock, act_btn, buy_btn = False, 0, None, None
        for r, c, t in self.flat_buttons(dmsg):
            if name.lower() not in (t or "").lower():
                continue
            if "⏱" in (t or ""):
                active = True
            m = re.search(r"x\s*(\d+)", t or "")
            if m:
                stock = int(m.group(1))
                act_btn = (r, c)                                  # кнопка запаса = активировать
            elif "🏅" in (t or "") and STAR not in (t or ""):
                buy_btn = (r, c)                                  # цена в золоте = купить
        return active, stock, act_btn, buy_btn

    async def _ensure_one_defense(self, dmsg, item):
        """Докупить запас до reserve и активировать, если не активен. Вернуть свежий dmsg."""
        name, reserve = item["name"], item["reserve"]
        active, stock, act_btn, buy_btn = self._defense_state(dmsg, name)
        tried_collect, guard = False, 0
        while stock < reserve and buy_btn and guard < 6:
            prev = stock
            await self.click(dmsg, buy_btn[0], buy_btn[1], label=f"Купить {name}")
            await rsleep(0.9)
            dmsg = await self.wait_text("Средства обороны") or dmsg
            active, stock, act_btn, buy_btn = self._defense_state(dmsg, name)
            guard += 1
            if stock <= prev and not self.dry:      # не купилось → скорее всего не хватило золота
                if not tried_collect:
                    log(f"  💰 {name}: не купилось — собираю золото с холопов")
                    await self._collect_holop_gold()
                    tried_collect = True
                    dmsg = await self._reopen_defense() or dmsg
                    active, stock, act_btn, buy_btn = self._defense_state(dmsg, name)
                    continue
                log(f"  💰 {name}: золота не хватает — пропускаю докупку")
                break
        if not active and act_btn and stock > 0:
            await self.click(dmsg, act_btn[0], act_btn[1], label=f"Активировать {name}")
            await asyncio.sleep(random.uniform(0.7, 1.4))
            # ВАЖНО: игра отвечает АЛЕРТОМ, сообщение само не меняется — перечитываем экран
            # и ПРОВЕРЯЕМ, что появился ⏱️. Раньше бот писал «активирован» вслепую.
            dmsg = await self._reopen_defense() or dmsg
            now_active, now_stock, _, _ = self._defense_state(dmsg, name)
            if now_active:
                log(f"  🛡️ {name}: АКТИВИРОВАН ✔ (запас {now_stock})")
            elif now_stock < stock:
                # запас уменьшился — предмет точно потрачен на активацию, просто экран
                # ещё не успел перерисовать ⏱️. Считаем успехом, чтобы не тратить второй.
                log(f"  🛡️ {name}: активирован ✔ (запас {stock}→{now_stock}, таймер ещё не отрисован)")
            else:
                log(f"  ⚠️ {name}: активация НЕ подтвердилась (запас {now_stock}) — повторю в след. заход")
        elif active:
            log(f"  ✅ {name}: уже активен, запас {stock}")
        elif not act_btn:
            log(f"  ⚠️ {name}: нет запаса и не смог купить")
        return dmsg

    # ---------- 🐴 АВТО-ОБОЗ (+50% серебра с набегов на 50 мин) ----------
    def _load_oboz_until(self):
        """До какого времени действует купленный обоз. Держим в ЛОКАЛЬНОМ файле,
        чтобы НЕ лазить в магазин каждый проход (просьба Максима — меньше запросов)."""
        try:
            with open(self.oboz_path, encoding="utf-8") as f:
                return float(json.load(f).get("until", 0))
        except (OSError, ValueError, TypeError):
            return 0.0

    def _save_oboz_until(self, until):
        try:
            with open(self.oboz_path, "w", encoding="utf-8") as f:
                json.dump({"until": until}, f)
        except OSError:
            pass

    async def _ensure_gold(self, need):
        """Добрать ЗОЛОТА до need: сначала собрать с холопов, потом — снять из казны.
        ВАЖНО: на кнопке обоза нарисовано серебро 🪙, но игра списывает ЗОЛОТО 🏅
        (баг игры — проверено Владимиром вживую). Поэтому считаем именно золото."""
        gold, _ = await self.my_balance()
        if gold >= need:
            return gold
        await self._collect_holop_gold()
        gold, _ = await self.my_balance()
        if gold >= need:
            return gold
        log(f"  🏦 обоз: золота {gold} < {need} — снимаю из казны")
        try:
            await self.kazna_withdraw("gold", need - gold + 100)
        except Exception as e:
            log(f"  ⚠️ обоз: не снял золото из казны: {type(e).__name__}: {e}")
        gold, _ = await self.my_balance()
        return gold

    def _oboz_retry(self, secs, why):
        """Отложить следующую попытку и СОХРАНИТЬ в файл. Важно: без записи на диск
        каждый перезапуск бота снова лез бы в магазин (замечание Максима)."""
        self._oboz_until = time.time() + secs + random.uniform(-secs * 0.15, secs * 0.15)
        self._save_oboz_until(self._oboz_until)
        log(f"  ⚠️ обоз: {why} — повторю через ~{secs // 60} мин")

    async def ensure_oboz(self):
        """Купить обоз «+50% · 50м», если прошлый истёк.
        Магазин РЕДАКТИРУЕТ ОДНО сообщение — навигируем по кнопкам и перечитываем
        его же (искать слово в тексте нельзя: «Обоз» есть только в кнопке)."""
        if time.time() < self._oboz_until:
            return                                   # ещё действует — в игру не лезем
        log("🐴 Авто-обоз: прошлый истёк — беру новый (+50% на 50 мин)")
        gold = await self._ensure_gold(OBOZ_COST)
        if gold < OBOZ_COST:
            self._oboz_retry(600, f"не хватает золота ({gold} < {OBOZ_COST}🏅)")
            return
        await self.send("Магазин")
        shop = await self.wait_text("Магазин")
        if not shop:
            self._oboz_retry(600, "магазин не открылся")
            return
        # 1) корень магазина → «Расходуемые ресурсы» (сообщение редактируется на месте)
        if not await self.click_text(shop, "Расходуемые", label="Расходуемые ресурсы"):
            self._oboz_retry(600, "нет кнопки «Расходуемые ресурсы»")
            return
        await rsleep(1.0)
        con = await self.refetch(shop.id) or shop
        # 2) «🐴 Обоз (% серебра набег) ►» — ищем ПО КНОПКАМ, не по тексту
        if not await self.click_text(con, "Обоз", label="Обоз"):
            btns = " | ".join(f"«{t}»" for _, _, t in self.flat_buttons(con))
            log(f"  📋 кнопки экрана: {btns}")
            self._oboz_retry(600, "не нашёл кнопку «Обоз»")
            return
        await rsleep(1.0)
        scr = await self.refetch(con.id) or con
        # 3) выбрать «+50% · 50м»
        a, b = OBOZ_PICK
        for r, c, t in self.flat_buttons(scr):
            if a in (t or "") and b in (t or ""):
                if STAR in (t or ""):
                    self._oboz_retry(900, f"вариант за звёзды «{t}» — не беру")
                    return
                await self.click(scr, r, c, label=f"Купить обоз «{t}»")
                self._oboz_until = time.time() + OBOZ_MINUTES * 60 - 60
                self._save_oboz_until(self._oboz_until)   # ← в файл, чтобы не лезть повторно
                log(f"  🐴 куплен обоз «{t}» — действует ~{OBOZ_MINUTES} мин "
                    f"(следующая проверка по файлу, без запросов)")
                return
        btns = " | ".join(f"«{t}»" for _, _, t in self.flat_buttons(scr))
        log(f"  📋 кнопки обоза: {btns}")
        self._oboz_retry(900, "не нашёл вариант «+50% · 50м»")

    async def check_attacked(self):
        """Заметить пуш «НА ТЕБЯ НАПАЛИ» → сразу перепроверить оборону.
        Ров/частокол — расходники (1 и 3 набега), после атаки часто уже сгорели.
        Событийно = без лишних опросов магазина."""
        if not self._auto_defense:
            return
        try:
            msgs = await self.recent(6)
        except Exception:
            return
        for m in msgs:
            if m.out or m.id <= self._last_attack_id:
                continue
            if ATTACK_MARKER in (m.message or ""):
                self._last_attack_id = m.id
                if self._next_defense > time.time():
                    self._next_defense = time.time()   # проверить оборону немедленно
                    log("⚔️ Заметил набег на меня — сразу перепроверю ров/частокол")
                return

    async def ensure_defenses(self):
        """Держать ров и частокол активными + запас. Нет золота — снять с холопов."""
        log("🛡️ Авто-оборона: проверяю ров/частокол…")
        dmsg = await self._reopen_defense()
        if not dmsg:
            log("  ⚠️ не открыл «Средства обороны»")
            self._next_defense = time.time() + 1200 + random.uniform(-240, 240)
            return
        for item in DEFENSE_ITEMS:
            dmsg = await self._ensure_one_defense(dmsg, item) or dmsg
        # ров блокирует 1 набег, частокол — 3, сгорают быстро → проверяем чаще, чем раньше.
        # Плюс мгновенная перепроверка по пушу «НА ТЕБЯ НАПАЛИ» (см. check_attacked).
        self._next_defense = time.time() + 600 + random.uniform(-180, 180)   # ~10 мин ± 3
        log("🛡️ Авто-оборона: готово.")

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
            await rsleep(0.5)
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
            await rsleep(0.5)
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
            await rsleep(0.5)
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
            await rsleep(0.5)
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
        _rec = await self.recent(1)      # может быть пусто, если @holop прислал битую анимацию
        before_id = _rec[0].id if _rec else 0
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
            await rsleep(0.6)
        if not result:
            # Молчаливый ответ (частокол/ров): само сообщение не меняется. Проверяем
            # по земле — свежий поиск покажет, ушла ли цель в КД (значит удар прошёл).
            try:
                await rsleep(1.0)
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
        if DONATE_RE.search(result):
            return "donate", 0, parse_result_my_hp(result)   # Купол/Стена — жрёт требушеты
        outcome, loot = classify_result(result)
        outcome = refine_outcome(result, outcome)
        self.stats["rep"] += parse_rep(result)   # 📈 репутация с этого боя (если есть)
        self._last_raw = result
        self._last_rep_penalty = parse_rep_penalty(result)   # < 0 → штраф репутации за атаку
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
            # 🧱 ров/частокол видно в данных кнопки атаки («_def_»). Если «не пробивать» — пропускаем.
            if not self._pierce_defenses:
                datas = self.target_button_datas(res)
                data = datas[idx] if idx < len(datas) else None
                if data and b"_def_" in data:
                    dtype = data.split(b"_def_")[-1].decode("ascii", "ignore")
                    self.next_ok[name] = time.time() + s["defended_retry"] * 60
                    log(f"  🧱 {name}: {dtype or 'ров/частокол'} — пропускаю "
                        f"(«пробивать» выкл), ретрай через {s['defended_retry']}м")
                    return
            hp = b.get("hp")
            if hp is not None and hp <= s["tgt_min_hp"]:
                goal = s["tgt_min_hp"] + 1        # бить можно уже с 20+ HP — ждём только до этого
                wait_min = max(1.0, (goal - hp) * s["min_per_hp"])
                wait_s = wait_min * 60
                if self._war_mode:
                    wait_s = min(wait_s, WAR_WEAK_CAP)   # реген цели может быть быстрее расчёта
                self.next_ok[name] = time.time() + wait_s
                log(f"  💤 {name}: HP {hp} ≤ {s['tgt_min_hp']} — жду до {goal}+ ~{wait_min:.0f}м")
                return
            outcome, loot, my_after = await self.attack(res, positions[idx], name)
            # игра отказала по низкому HP — уходим на лечение, удар НЕ засчитан
            if outcome == "myweak":
                hp = my_after if isinstance(my_after, int) else 0
                self._healing = True
                self._heal_start = time.time()
                # засев оценки HP: если знаем hp — с него, иначе форсим чтение (t=0)
                self._heal_from_hp = hp if isinstance(my_after, int) else 0
                self._heal_from_t = time.time() if isinstance(my_after, int) else 0.0
                self._last_hp_read = time.time() if isinstance(my_after, int) else 0.0
                log(f"🩸 Игра: HP {hp} < {s['my_min_hp']} — мало для атаки. Ухожу на лечение "
                    f"до {s['my_recover_to']}.")
                return hp
            # ПОРАЖЕНИЕ — снимаем цель с ротации до распоряжения (чтобы не сливать HP)
            if outcome == "loss":
                self.stats["hits"] += 1
                self.stats["loss"] += 1
                self.bench_add(name)
                log(f"  ❌ {name}: ПОРАЖЕНИЕ в бою — СНЯЛ С РОТАЦИИ до твоего распоряжения "
                    f"(чтобы не сливать HP). Скажи «верни {name}», чтобы вернуть.")
                return my_after
            # ДОНАТ-ЗАЩИТА (Железный Купол/Стена) — жрёт требушеты, снимаем НАВСЕГДА
            if outcome == "donate":
                self.stats["hits"] += 1
                if self._hit_shields:
                    # донат-щит (Купол/Стена) — расходник соперника: требушет сносит его,
                    # дальше цель открыта. Оставляем в ротации и фармим.
                    cd = s["attack_cd"] + random.uniform(s["jitter_lo"], s["jitter_hi"])
                    self.next_ok[name] = self._spread(time.time() + cd, name)
                    log(f"  🏹 {name}: снёс донат-щит ТРЕБУШЕТОМ (щит — расходник, цель открыта) "
                        f"— оставляю в ротации, КД {fmt_secs(self.next_ok[name] - time.time())}")
                    return my_after
                self.donate_add(name)
                self.next_ok[name] = time.time() + 10 ** 9   # не вернётся в этой сессии
                log(f"  🛡️ {name}: донат-щит (Купол/Стена) — «бить щитников» ВЫКЛ, берегу требушеты, "
                    f"убрал из ротации. Включи галочку, чтобы сносить щит и фармить. "
                    f"Вернуть вручную: убери ник из {os.path.basename(self.donate_path)}")
                return my_after
            # РЕПУТАЦИЯ СПИСАНА за атаку (слабый/низкий соперник) — на скамейку, чтобы не терять реп
            if self._last_rep_penalty < 0:
                self.stats["hits"] += 1
                self.bench_add(name)
                self.next_ok[name] = time.time() + s["clan_level_retry"] * 60
                raw = " ".join((self._last_raw or "").split())[:200]
                log(f"  📉 {name}: игра СПИСАЛА репутацию ({self._last_rep_penalty:+.0f}) за атаку — "
                    f"СНЯЛ НА СКАМЕЙКУ (верни: «верни {name}»). Сырой ответ: {raw}")
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
                wait_s = wait_min * 60
                if self._war_mode:
                    wait_s = min(wait_s, WAR_WEAK_CAP)   # реген цели может быть быстрее расчёта
                self.next_ok[name] = time.time() + wait_s
                log(f"  💤 {name}: слаб (HP {hp}) — жду до {goal}+ ~{wait_min:.0f}м")
            else:
                self.next_ok[name] = time.time() + s["weak_retry"] * 60
                log(f"  💤 {name}: слаб — ретрай через {s['weak_retry']}м")
            return
        # щит → в профиль за таймером
        secs = await self.shield_seconds(name)
        if secs and secs > 0:
            self.next_ok[name] = time.time() + secs + (WAR_SHIELD_PAD if self._war_mode else 30)
            log(f"  🛡️ {name}: под щитом ещё ~{fmt_secs(secs)} — таймер поставлен")
        else:
            self.next_ok[name] = time.time() + s["shield_default_retry"] * 60
            log(f"  🛡️ {name}: щит, таймер не прочитан — ретрай через {s['shield_default_retry']}м")

    # ═══════════ ЗАЩИТА ОТ БОЧКИ (динамита) ═══════════
    async def open_druzhina(self):
        """Открыть экран «Дружина». Вернуть сообщение (заминировано → с кнопкой «Огниво»,
        иначе обычный экран дружины/оружия). None — только если прочитать НЕ удалось."""
        await self.send("Дружина")
        for _ in range(14):
            for m in sorted(await self.recent(8), key=lambda x: x.id, reverse=True):
                if m.out:
                    continue
                t = (m.message or "")
                has_ognivo = any("огниво" in (bt or "").lower() for _, _, bt in self.flat_buttons(m))
                if has_ognivo or MINED_MARKER in t or "ДРУЖИН" in t.upper() or "оружие" in t.lower():
                    return m
            await rsleep(0.5)
        return None

    def _is_mined(self, dr):
        """Заминирована ли территория по экрану «Дружина» (текст ЗАМИНИРОВАНА или кнопка Огниво)."""
        if not dr:
            return False
        return (MINED_MARKER in (dr.message or "")
                or any("огниво" in (bt or "").lower() for _, _, bt in self.flat_buttons(dr)))

    def _bomb_log(self, text):
        """Отдельный чистый журнал инцидентов с бочкой — не теряется в шуме набегов."""
        log(text)   # дублируем и в основной лог
        try:
            with open(os.path.join(HERE, "bomb_events.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + text + "\n")
        except OSError:
            pass

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
            await rsleep(0.5)
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
            await rsleep(0.5)
        if not km or not await self.click_text(km, section, label=section):
            log(f"  ⚠️ казна: не открыл раздел {section}")
            return False

        async def _find(pred, tries=10):
            for _ in range(tries):
                for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                    if not m.out and pred(m):
                        return m
                await rsleep(0.5)
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
        await rsleep(0.6)
        await self.send(str(amount))
        await rsleep(1.2)
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

    # ═══════════════ АВТОЗАЩИТА ОТ БОЧКИ (переписано по живому захвату 27.07) ═══════════════
    # Ключевое: вся мини-игра идёт на ОДНОМ сообщении «💣 ЗАМИНИРОВАНО!» — бот РЕДАКТИРУЕТ
    # его на месте. Действуем = жмём кнопки на ЭТОМ message_id (Огниво → красный фитиль).
    # Клик по позиции (r,c), а не по тексту (текст с «—» ломает press). Красный НЕ гарантирован
    # (33%) — на промах отдельный путь восстановления: лечение 100k серебра + защита всех за 10% золота.
    def _btn_pos(self, m, sub, skip_star=True):
        """(r,c,text) первой кнопки с подстрокой sub (без ⭐ если skip_star), иначе None."""
        for r, c, bt in self.flat_buttons(m):
            if sub in (bt or "").lower() and not (skip_star and STAR in (bt or "")):
                return (r, c, bt)
        return None

    def _has(self, m, sub):
        return self._btn_pos(m, sub, skip_star=False) is not None

    async def _click_sub(self, m, sub, label="", skip_star=True):
        p = self._btn_pos(m, sub, skip_star=skip_star)
        if not p:
            return False
        await self.click(m, p[0], p[1], label=label or p[2])   # клик по позиции — устойчив к «—»
        return True

    async def _wait_msg(self, pred, tries=14):
        for _ in range(tries):
            for m in sorted(await self.recent(6), key=lambda x: x.id, reverse=True):
                if not m.out and pred(m):
                    return m
            await rsleep(0.5)
        return None

    async def _refetch_until(self, mid, pred, tries=20):
        """Ждём, пока сообщение бочки (mid) удовлетворит pred. Читаем ДВА канала:
        refetch(mid) (игра правит то же сообщение) и свежую ленту (вдруг экран пришёл
        отдельным сообщением). Оба в try — видео-сообщение бочки иногда роняет чтение."""
        last = None
        for _ in range(tries):
            try:
                m = await self.refetch(mid)
                if m:
                    last = m
                    if pred(m):
                        return m
            except Exception as e:
                if _is_dead_session(e):
                    raise
            try:
                for mm in sorted(await self.recent(5), key=lambda x: x.id, reverse=True):
                    if not mm.out and pred(mm):
                        return mm
            except Exception:
                pass
            await rsleep(0.5)
        return last

    async def check_and_handle_bomb(self, force=False):
        """Найти нотификацию бочки «💣 ЗАМИНИРОВАНО!» (на ней кнопка «огниво») и обработать.
        Вернуть True, если бочка найдена и обработана. Работает ВСЕГДА, пока включена защита."""
        mined = None
        try:
            for m in sorted(await self.recent(25), key=lambda x: x.id, reverse=True):
                if m.out or m.id in self._bomb_done:
                    continue
                t = (m.message or "").upper()
                # триггер — кнопка «огниво» (она есть только на нотификации бочки),
                # либо явный текст «ЗАМИНИРОВАН…»
                if self._has(m, "огниво") or (BOMB_NOTIF in t and self.flat_buttons(m)):
                    mined = m
                    break
        except Exception as e:
            if _is_dead_session(e):
                raise
            return False
        if not mined:
            return False
        try:
            await self.handle_bomb(mined)
        finally:
            self._bomb_done.add(mined.id)
            if len(self._bomb_done) > 300:
                self._bomb_done = set(sorted(self._bomb_done)[-150:])
        self._bomb_alert_until = 0.0
        return True

    async def _bomb_guard_tick(self):
        """Проверка бочки ВНУТРИ прохода набегов (между целями), не чаще ~раз в 20с.
        Раньше бочка проверялась только в начале цикла, а проход по целям (особенно
        свободная охота — до 10 целей) длился до ~5 минут → бот реагировал на бочку
        с задержкой в минуты (подтверждено скринами Макса: фитиль на «осталось 4м57с»).
        Вернуть True, если бочка была обработана — тогда проход надо прервать."""
        if not (self._bomb_defense or self._defense_only):
            return False
        now = time.time()
        if now - self._last_bomb_scan < 20:
            return False
        self._last_bomb_scan = now
        try:
            return await self.check_and_handle_bomb()
        except Exception as e:
            if _is_dead_session(e):
                raise
            log(f"  ⚠️ сбой проверки бочки в проходе: {type(e).__name__}: {e}")
            return False

    async def handle_bomb(self, mined):
        """Разминировать бочку на её сообщении: Огниво → красный фитиль → итог.
        Промах (33%) → восстановление. Огниво НЕ покупаем (Владимир держит запас вручную)."""
        txt = mined.message or ""
        who = re.search(r"[Нн]ападающ\w*:\s*([^\n]+)", txt) or re.search(r"Атаковал:\s*([^\n]+)", txt)
        who_s = who.group(1).strip() if who else "?"
        self.stats["bombs"] += 1
        self._bomb_log("💣💣💣 БОЧКА! Нападающий: {} — РАЗМИНИРОВАНИЕ. Кнопки: {}".format(
            who_s, " | ".join(bt for _, _, bt in self.flat_buttons(mined))))
        await self.notify_me(f"💣 На тебя заложили бочку ({who_s}). Разминирую (Огниво → красный фитиль).",
                             key="bomb", throttle=300)
        # 1) ОГНИВО — по подстроке, клик по позиции. Нет кнопки → Огниво кончилось: НЕ покупаем.
        if not await self._click_sub(mined, "огниво", label="Огниво"):
            self._bomb_log("  ⛔ на нотификации нет кнопки «Огниво» — запас кончился. Не покупаю "
                           "(держи Огниво вручную). Жду итог, при взрыве — восстановлю.")
            return await self._finish_bomb(mined.id)
        await rsleep(1.0)
        # 2) ВЫБОР ФИТИЛЯ на ТОМ ЖЕ сообщении → красный (простой текст «Красный фитиль», без 🔴)
        fuse = await self._refetch_until(mined.id, lambda m: self._has(m, "фитил") or self._has(m, "красн"))
        if fuse and (self._has(fuse, "фитил") or self._has(fuse, "красн")):
            self._bomb_log("  🎲 фитили: " + " | ".join(bt for _, _, bt in self.flat_buttons(fuse)))
            if not await self._click_sub(fuse, "красн", label="Красный фитиль"):
                self._bomb_log("  ⚠️ кнопки «красный фитиль» нет — сырые: "
                               + " | ".join(bt for _, _, bt in self.flat_buttons(fuse)))
        else:
            cur = await self.refetch(mined.id)
            self._bomb_log("  ⚠️ экран фитилей не пришёл. Текущий: "
                           + (" ".join((cur.message or "").split())[:160] if cur else "(нет)"))
        await rsleep(1.0)
        return await self._finish_bomb(mined.id)

    async def _finish_bomb(self, mid):
        """Прочитать итог на сообщении бочки: обезврежено / взрыв → восстановление.
        ВЗРЫВ проверяем ПЕРВЫМ: текст взрыва «…НЕправильный фитиль» содержит подстроку
        «правильный фитиль» — раньше это ловилось как успех (ложно). Читаем и само
        сообщение (refetch), и свежую ленту — итог мог прийти отдельным сообщением."""
        res = "unknown"
        for _ in range(20):
            texts = []
            try:
                m = await self.refetch(mid)
                if m:
                    texts.append((m.message or "").lower())
            except Exception as e:
                if _is_dead_session(e):
                    raise
            try:
                for mm in sorted(await self.recent(5), key=lambda x: x.id, reverse=True):
                    if not mm.out:
                        texts.append((mm.message or "").lower())
            except Exception:
                pass
            joined = " ".join(texts)
            if any(w in joined for w in EXPLODED_WORDS):    # ВЗРЫВ — ПЕРВЫМ (см. коммент)
                res = "exploded"
                self._bomb_log("  📋 экран взрыва распознан.")
                break
            if any(w in joined for w in DEFUSED_WORDS):
                res = "defused"
                break
            await rsleep(0.5)
        if res == "defused":
            self.stats["defused"] += 1
            self._bomb_log("  ✅ БОЧКА ОБЕЗВРЕЖЕНА — территория цела.")
            await self.notify_me("✅ Бочку разминировал — территория цела.", key="bombok", throttle=1)
            return
        if res == "exploded":
            self.stats["exploded"] += 1
            self._bomb_log("  💥 ВЗРЫВ (красный не тот — рандом 33%). Восстанавливаю территорию и холопов.")
            await self.notify_me("💥 Бочка взорвалась (не угадал фитиль). Лечу территорию и защищаю "
                                 "холопов из казны.", key="boom", throttle=1)
            await self.recover_after_explosion()
            return
        # итог не распознан — перестрахуемся: если территория взорвана, восстановим
        self._bomb_log("  ⁇ итог бочки не распознан — проверяю, не взорвана ли территория.")
        if await self._territory_exploded():
            self.stats["exploded"] += 1
            await self.recover_after_explosion()

    async def _territory_exploded(self):
        await self.send("Территория")
        m = await self._wait_msg(lambda x: "ТЕРРИТОРИЯ" in (x.message or "").upper())
        low = (m.message or "").lower() if m else ""
        return any(w in low for w in ("взорвана", "взорвано", "взрыв"))

    # ─────────── ВОССТАНОВЛЕНИЕ ПОСЛЕ ВЗРЫВА (по правкам Максима, живой лог 28.07) ───────────
    # Ключевое: подтверждения приходят АЛЕРТОМ (ответ кнопки), а НЕ сообщением — читаем из
    # результата click(). Золото на защиту берём С БАЛАНСА (в казну за золотом НЕ лезем —
    # Максим: «оно на кармане должно быть, казна создаёт ошибки»). Серебро 100k — с ретраями
    # («жать, пока не снимется» — игровой баг). В конце проверяем, что территория не «взорвана».
    async def recover_after_explosion(self):
        self._bomb_log("  🛠️ ВОССТАНОВЛЕНИЕ: лечу территорию (100k🪙 из казны) → защищаю всех "
                       "холопов (золото С БАЛАНСА, в казну не лезу).")
        ok_heal = await self.heal_territory()
        ok_prot = await self.protect_all_holops()
        self._bomb_log("  🏁 Восстановление — лечение: {}, защита: {}.".format(
            "ок" if ok_heal else "НЕ ок", "ок" if ok_prot else "НЕ ок"))
        await self.notify_me("🛠️ После взрыва: территория {}, холопы {}."
                             .format("вылечена" if ok_heal else "НЕ вылечена ⚠️",
                                     "защищены" if ok_prot else "НЕ защищены ⚠️"), key="recov", throttle=1)

    @staticmethod
    def _alert_text(res):
        """Текст всплывающего ответа кнопки (BotCallbackAnswer.message) — там подтверждения."""
        return (getattr(res, "message", None) or "") if res is not None else ""

    async def _press_territory_heal(self):
        """Открыть Территорию, нажать кнопку лечения «100.0K🪙»/«Восстановить». Вернуть текст алерта."""
        await self.send("Территория")
        tmsg = await self._wait_msg(lambda m: "ТЕРРИТОРИЯ" in (m.message or "").upper() and self.flat_buttons(m))
        if not tmsg:
            return None
        heal = None
        for r, c, bt in self.flat_buttons(tmsg):
            if STAR in (bt or ""):
                continue
            low = (bt or "").lower()
            if ("🪙" in (bt or "") and "100" in (bt or "")) or "восстанов" in low or "лечи" in low:
                heal = (r, c, bt)
                break
        if not heal:
            self._bomb_log("  ⚠️ на территории нет кнопки лечения «100.0K🪙». Сырые: "
                           + " | ".join(bt for _, _, bt in self.flat_buttons(tmsg)))
            return None
        res = await self.click(tmsg, heal[0], heal[1], label=heal[2])
        return self._alert_text(res)

    async def _withdraw_silver_100k(self):
        """Снять 100 000 серебра из казны с РЕТРАЯМИ (баг: «жать несколько раз»).
        Аварийный вид (взрыв): «Снять с депозита»→«Снять 100.0K». Обычный: Серебро→Снять→100k/Ввести сумму."""
        for _ in range(3):
            await self.send("Личная казна")
            km = await self._wait_msg(lambda m: any(w in (m.message or "").lower()
                                                    for w in ("казна", "депозит", "снятие", "взорвана"))
                                      and self.flat_buttons(m))
            if not km:
                await rsleep(1.2)
                continue
            # АВАРИЙНЫЙ вид: «Снять с депозита (…🪙)»
            if await self._click_sub(km, "снять с депозита", label="Снять с депозита"):
                step = await self._wait_msg(lambda m: any(("снять" in (bt or "").lower() and "100" in (bt or ""))
                                                          for _, _, bt in self.flat_buttons(m)))
                if step and await self._click_sub(step, "100", label="Снять 100.0K🪙"):
                    await rsleep(1.0)
                    return True
            # ОБЫЧНЫЙ вид: Серебро → Снять → (кнопка «100k» или Ввести сумму → 100000)
            elif await self._click_sub(km, "серебро", label="Серебро"):
                snyat = await self._wait_msg(lambda m: self._has(m, "снять"))
                if snyat and await self._click_sub(snyat, "снять", label="Снять"):
                    scr = await self._wait_msg(lambda m: self._has(m, "ввести")
                                               or any("100" in (bt or "") for _, _, bt in self.flat_buttons(m)))
                    if scr:
                        if await self._click_sub(scr, "100", label="Снять 100k"):
                            await rsleep(1.0)
                            return True
                        if await self._click_sub(scr, "ввести", label="Ввести сумму"):
                            await rsleep(0.6)
                            await self.send("100000")
                            await rsleep(1.2)
                            return True
            await rsleep(1.2)
        self._bomb_log("  ⚠️ не смог снять серебро за 3 попытки — проверь казну.")
        return False

    async def heal_territory(self):
        """Лечение территории 100k серебра. Если свободного серебра мало — снимаем из казны.
        Жмём «100.0K🪙» на территории, подтверждение читаем из АЛЕРТА. Ретраим, пока территория
        не перестанет быть «взорвана» (Максим: «жать, пока не снимется; проверять»)."""
        for attempt in range(4):
            _, silver = await self.my_balance()
            if silver < 100000:
                await self._withdraw_silver_100k()
            alert = await self._press_territory_heal()
            if alert and "восстановлен" in alert.lower():
                self.stats["spent_silver"] += 100000
                self._bomb_log(f"  ❤️ Территория восстановлена ({alert.strip()}).")
                return True
            if not await self._territory_exploded():
                self._bomb_log("  ❤️ Территория больше не взорвана — лечение засчитано.")
                return True
            self._bomb_log(f"  ↻ лечение не подтвердилось (попытка {attempt + 1}/4) — пробую ещё.")
            await rsleep(1.5)
        self._bomb_log("  ⚠️ территорию вылечить не удалось за 4 попытки — проверь вручную.")
        return False

    async def protect_all_holops(self):
        """Защитить ВСЕХ холопов золотом С БАЛАНСА (в казну за золотом НЕ лезем — Максим).
        Подтверждение — из АЛЕРТА. Ретраим (игровой баг «жать несколько раз»)."""
        for attempt in range(4):
            await self.send("Холопы")
            hub = await self._wait_msg(lambda m: self._has(m, "холопы ("))
            if not (hub and await self._click_sub(hub, "холопы (", label="список холопов")):
                await rsleep(1.2)
                continue
            await rsleep(0.8)
            lst = await self._wait_msg(lambda m: self._has(m, "защитить всех"))
            if not lst:
                self._bomb_log("  ✅ кнопки «Защитить всех» нет — охрана, вероятно, уже на месте.")
                return True
            p = self._btn_pos(lst, "защитить всех", skip_star=True)
            if not p:
                return False
            res = await self.click(lst, p[0], p[1], label=p[2])
            alert = self._alert_text(res).lower()
            if "установлен" in alert or "потрачено" in alert:
                self.stats["spent_gold"] += parse_amount(p[2]) or 0
                self._bomb_log(f"  🛡️ Защита установлена ({self._alert_text(res).strip()}).")
                return True
            self._bomb_log(f"  ↻ защита не подтвердилась (попытка {attempt + 1}/4)"
                           + (f", алерт: «{self._alert_text(res).strip()}»" if self._alert_text(res) else "")
                           + ". Пробую ещё.")
            await rsleep(1.5)
        self._bomb_log("  ⚠️ защиту холопов подтвердить не удалось. Проверь, что золото есть на балансе "
                       "(бот в казну за золотом не лезет — держи золото на кармане).")
        return False

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

    def interim_report(self, why=""):
        """Промежуточный отчёт — те же цифры, что в итоговом, но по ходу дела
        (напр. при уходе на лечение). Чтобы видеть прогресс, не дожидаясь остановки."""
        st = self.stats
        dur = time.time() - self._started if self._started else 0
        log("──────── ПРОМЕЖУТОЧНЫЙ ОТЧЁТ ────────" + (f"  ({why})" if why else ""))
        log(f"  ⏱️ Длительность: {fmt_secs(dur)}")
        log(f"  ⚔️ Ударов: {st['hits']}  |  🏆 Побед: {st['wins']}  |  "
            f"🧱 Пробивал: {st['blocked']}  |  ❌ Поражений: {st['loss']}")
        log(f"  🪙 Награблено серебра: {st['loot']:,}".replace(",", " "))
        log(f"  📈 Репутация заработана: +{st['rep']:.1f}")
        if st.get("bombs"):
            log(f"  💣 Бочек: {st['bombs']}  |  🔧 Разминировано: {st['defused']}  |  "
                f"💥 Взрывов: {st['exploded']}")
        log("──────────────────────────────────────")

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
        poll0 = int(self.s.get("bomb_poll_interval", 0) or 0)
        log("💣 Анти-бочка: " + ("опрос «Дружины» раз в %dс + чек при старте" % poll0 if poll0 > 0
            else "по пушу «скоро взорвётся» — «Дружину» НЕ открываю, пока нет бочки"))
        if poll0 > 0:
            try:
                await self.check_and_handle_bomb(force=True)   # старт-чек только в режиме опроса (ночь)
            except Exception as e:
                if _is_dead_session(e):
                    raise
                log(f"  ⚠️ стартовый чек бочки не удался: {type(e).__name__}: {e}")
        if self._regen_auto:
            try:
                await self.update_regen_from_main()
            except Exception as e:
                if _is_dead_session(e):
                    raise
                log(f"  ⚠️ авто-реген не удался: {type(e).__name__}: {e}")
        self._last_bank = time.time()
        self._next_bank = time.time() + 3600 + random.uniform(-600, 600)
        self._next_defense = time.time() + 60 + random.uniform(0, 120)   # первую оборону — скоро
        self._oboz_until = self._load_oboz_until()   # обоз мог остаться живым с прошлого запуска
        if self._auto_oboz:
            left = max(0, self._oboz_until - time.time())
            log(f"🐴 Авто-обоз ВКЛ — {'действует ещё ' + fmt_secs(left) if left else 'куплю в ближайшем проходе'}")
        if self._auto_kazna:
            log(f"🏦 Авто-казна ВКЛ — первый сбор примерно через {(self._next_bank-time.time())/60:.0f} мин, "
                "плюс при уходе на лечение.")
        if self._auto_defense:
            log("🛡️ Авто-оборона ВКЛ — держу ров/частокол активными + запас.")
        while True:
            if await self.gate() == "stop":
                break
            try:
                if await self._one_cycle() == "stop":
                    break
                self._dup_strikes = 0   # цикл прошёл успешно — счётчик дубль-IP обнуляем
                self._save_cd_cache()   # КД целей → в файл (переживёт перезапуск, не долбит после старта)
                if await self._maybe_human_break() == "stop":
                    break
            except Exception as e:
                # ДУБЛЬ-IP (VPN сменил IP на ходу) — НЕ умираем, а переподключаемся.
                # Часто транзиентно; раньше бот тут просто вставал (боль Ксюши).
                if _is_duplicate_ip(e):
                    self._dup_strikes = getattr(self, "_dup_strikes", 0) + 1
                    if self._dup_strikes <= 6:
                        wait = min(10 * self._dup_strikes, 60)
                        log(f"⚠️ Сессия дёрнулась с двух IP (VPN сменил IP?). Переподключаюсь "
                            f"({self._dup_strikes}/6) через {wait}с — НЕ останавливаюсь.")
                        try:
                            await self.c.disconnect()
                        except Exception:
                            pass
                        await asyncio.sleep(wait)
                        try:
                            await self.c.connect()
                        except Exception:
                            pass
                        if await self.sleep_gated(2) == "stop":
                            break
                        continue
                    log("🛑 Сессия рвётся с двух IP слишком часто (6 раз подряд). Зафиксируй "
                        "ОДНУ страну VPN и, если не поможет, войди заново («Сменить аккаунт»). Останавливаюсь.")
                    break
                if _is_dead_session(e):
                    log("🛑 СЕССИЯ БОЛЬШЕ НЕ РАБОТАЕТ — Telegram отозвал ключ. Надо войти "
                        "заново («Сменить аккаунт»). Останавливаюсь.")
                    break
                log(f"  ⚠️ сбой в цикле: {type(e).__name__}: {e} — продолжаю через 15с")
                try:
                    if not self.c.is_connected():
                        await self.c.connect()
                except Exception:
                    pass
                if await self.sleep_gated(15) == "stop":
                    break

    async def notify_me(self, text, key="", throttle=1800):
        """🔔 Прислать себе в «Избранное» (Saved Messages) о важном событии — узнаёшь сразу,
        даже не глядя в пульт. Троттл по ключу, чтоб не спамить. Не игровое действие."""
        if not self._notify_dm:
            return
        now = time.time()
        if key in self._notify_sent and now - self._notify_sent[key] < throttle:
            return                                # это событие слали недавно — не спамим
        self._notify_sent[key] = now
        try:
            await self.c.send_message("me", "🐱 Кот Альфа Бот\n" + text)
        except Exception:
            pass

    async def _maybe_human_break(self):
        """🧑 Человеческий режим: раз в ~40–100 мин «отойти» на 8–30 мин. Это ДОПОЛНЕНИЕ
        к обычной работе (ночью фармит), просто ломает машинно-ровный ритм. Вернуть 'stop',
        если во время перерыва пульт попросил остановиться."""
        if not self._human_mode:
            return None
        now = time.time()
        if self._next_human_break <= 0:
            self._next_human_break = now + random.uniform(1800, 4500)
            return None
        if now < self._next_human_break:
            return None
        mins = random.uniform(8, 30)
        log(f"☕ Человеческий режим: перерыв ~{mins:.0f} мин — чтобы активность не была "
            f"машинно-ровной. Потом продолжу.")
        st = await self.sleep_gated(mins * 60)
        self._next_human_break = time.time() + random.uniform(2400, 6000)   # следующий через 40–100 мин
        return st

    async def _one_cycle(self):
        """Один проход главного цикла. Вернуть 'stop' если пульт попросил остановиться, иначе None."""
        self.apply_live_settings()   # подхватываем настройки боя из панели на лету
        s = self.s
        self.heartbeat()
        # 💣 ПРИОРИТЕТ №1: защита от бочки (если включена галочкой). Важнее набегов и лечения.
        if self._bomb_defense or self._defense_only:
            try:
                if await self.check_and_handle_bomb():
                    return None
            except Exception as e:
                if _is_dead_session(e):
                    raise   # мёртвую сессию обрабатывает главный цикл (остановка)
                log(f"  ⚠️ сбой в проверке бочки: {type(e).__name__}: {e}")
        # 🛡️ РЕЖИМ «ТОЛЬКО ЗАЩИТА ОТ БОЧЕК»: не фармим — просто сторожим бочку короткими циклами.
        if self._defense_only:
            return await self.sleep_gated(random.uniform(20, 40))
        # 🏦 АВТО-КАЗНА по таймеру (раз в ~час ± рандом)
        if self._auto_kazna and self._next_bank and time.time() >= self._next_bank:
            try:
                await self.collect_and_bank()
            except Exception as e:
                if _is_dead_session(e):
                    raise
                log(f"  ⚠️ авто-казна (таймер) сбой: {type(e).__name__}: {e}")
                self._next_bank = time.time() + 1800 + random.uniform(-300, 300)   # ~30 мин ± 5 при сбое
            return None
        # ⚔️ если на меня напали — оборона могла сгореть, проверим немедленно
        await self.check_attacked()
        # 🐴 АВТО-ОБОЗ (+50% серебра с набегов). Сверяется с локальным файлом — без лишних запросов
        if self._auto_oboz:
            try:
                await self.ensure_oboz()
            except Exception as e:
                if _is_dead_session(e):
                    raise
                log(f"  ⚠️ авто-обоз сбой: {type(e).__name__}: {e}")
                self._oboz_retry(600, "сбой при покупке")
        # 🛡️ АВТО-ОБОРОНА по таймеру (ров/частокол активны + запас)
        if self._auto_defense and self._next_defense and time.time() >= self._next_defense:
            try:
                await self.ensure_defenses()
            except Exception as e:
                if _is_dead_session(e):
                    raise
                log(f"  ⚠️ авто-оборона сбой: {type(e).__name__}: {e}")
                self._next_defense = time.time() + 900 + random.uniform(-180, 180)   # ~15 мин ± 3 при сбое
            return None
        # РЕЖИМ ЛЕЧЕНИЯ: не атакуем, но КАЖДЫЙ РАЗ читаем реальное HP (Территория).
        # Просыпаемся сразу, как только HP дорос до recover_to (в т.ч. после эликсира).
        if self._healing:
            now = time.time()
            cap = (s["my_recover_to"] * s["min_per_hp"] * 60) + 600   # аварийный потолок
            # HP читаем (шлём «Территория») НЕ чаще раза в ~4 мин — не спамим (Максим:
            # «он что, сам не видит, обязательно территорией спамить?»). Между чтениями
            # ОЦЕНИВАЕМ HP по времени и скорости регена. Просыпаемся при этом часто —
            # но лишь для проверки бочки (чтение ленты, не команда игре).
            est = (self._heal_from_hp or 0) + (now - self._heal_from_t) / max(1.0, s["min_per_hp"] * 60)
            do_read = (self._heal_from_t == 0.0) or (now - self._last_hp_read >= 240) or (est >= s["my_recover_to"])
            hp = None
            if do_read:
                hp = await self.my_current_hp()
                self._last_hp_read = now
                if hp is not None:
                    self._heal_from_hp, self._heal_from_t, est = hp, now, hp
            cur = hp if hp is not None else est
            if cur >= s["my_recover_to"]:
                self._healing = False
                log(f"❤️ HP восстановлено ({int(cur)}) — продолжаю набеги.")
            elif now - self._heal_start > cap:
                self._healing = False
                log("❤️ Потолок лечения истёк — пробую продолжить (проверю HP в бою).")
            else:
                left_hp = s["my_recover_to"] - cur
                rem_min = max(1.0, left_hp * s["min_per_hp"])            # оценка минут до полного
                rem_s = rem_min * 60.0
                # сон короткий (для бочки), но HP при этом НЕ перечитываем каждый раз.
                cap_nap = 90.0 if (self._bomb_defense or self._defense_only) else 300.0
                base = min(rem_s * random.uniform(0.85, 1.0), cap_nap)
                nap = round(max(45.0, base) * random.uniform(0.9, 1.1))  # ← разброс на потолке
                tag = f"{int(cur)}" + ("" if hp is not None else "~")   # ~ = оценка, без запроса
                log(f"🩶 Лечусь: HP {tag}, до {s['my_recover_to']} ~{rem_min:.0f}м "
                    f"({'читаю' if do_read else 'оценка, не спамлю'}) — след. проверка через {nap}с")
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
            self._heal_from_hp, self._heal_from_t, self._last_hp_read = my_hp, time.time(), time.time()
            log(f"🩸 Мои HP {my_hp} ≤ {s['my_min_hp']} — ухожу на лечение до {s['my_recover_to']}.")
            self.interim_report("ушёл на лечение")   # промежуточный итог по ходу дела
            # 🏦 раз уж не бьём и регенимся — заодно собираем казну (но не чаще раза в 10 мин)
            if self._auto_kazna and time.time() - self._last_bank >= 600:
                try:
                    await self.collect_and_bank()
                except Exception as e:
                    if _is_dead_session(e):
                        raise
                    log(f"  ⚠️ авто-казна (лечение) сбой: {type(e).__name__}: {e}")
            return None

        # 🎯 СВОБОДНАЯ ОХОТА: не фикс-список, а слабейшие по защите прямо с арены.
        if self._free_hunt:
            return await self.free_hunt_cycle(arena, my_hp)

        self.targets = self.load_targets()   # подхватываем правки списка из панели на лету
        benched = self.load_benched()
        donated = self.load_donate()          # цели с донат-Куполом/Стеной — не бьём (требушеты)
        active = [t for t in self.targets if norm(t) not in benched and norm(t) not in donated]
        if not active:
            log("🪑 Все цели на скамейке/донате / список пуст — жду распоряжения (верни кого-то или добавь цель)")
            return await self.sleep_gated(60)
        active = self._rotate_after_last(active)   # продолжаем с места остановки, а не сначала

        now = time.time()
        eligible = [t for t in active if self.next_ok.get(t, 0.0) <= now]
        if not eligible:
            soonest = min(self.next_ok.get(t, 0.0) for t in active)
            # потолок сна 120с — чтобы проверка на бочку срабатывала не реже ~2 мин
            # в ВОЙНЕ просыпаемся почти ровно к освобождению цели (секунды решают)
            cap = WAR_NAP_CAP if self._war_mode else 120.0
            floor = WAR_NAP_FLOOR if self._war_mode else 5.0
            nap = max(floor, min(soonest - now, cap))
            log(f"{'⚔️' if self._war_mode else '⏳'} Все цели на КД — сплю {fmt_secs(nap)} (мои HP {my_hp})")
            return await self.sleep_gated(nap)

        log(f"── Проход: доступно целей {len(eligible)}, мои HP {my_hp}")
        for t in eligible:
            if self.control_state() != "run":
                return None   # пульт переключили — уходим на gate() в начале цикла
            if await self._bomb_guard_tick():
                return None   # 💣 бочка важнее набега — обработали, прерываем проход
            my_after = await self.do_target(t)
            self._last_hit_name = t          # запомнили позицию — после лечения продолжим отсюда
            if isinstance(my_after, int) and my_after <= s["my_min_hp"]:
                log(f"🩸 После удара мои HP {my_after} ≤ {s['my_min_hp']} — прерываю проход на лечение "
                    f"(продолжу список после «{t}»)")
                return None
            await self.inter_hit()
        return None

    async def free_hunt_cycle(self, arena, my_hp):
        """Один проход свободной охоты: набрать слабейших по защите с арены и отдубасить
        их обычным do_target() (лечение/статистика/КД/ров-частокол — всё как в списке)."""
        s = self.s
        names = await self.pick_hunt_names(arena)
        if not names:
            nap = WAR_NAP_CAP if self._war_mode else 90.0
            log(f"🎯 Охота: доступных слабых по защите не видно (все в КД/клан/купол) — "
                f"пробую снова через {fmt_secs(nap)}")
            return await self.sleep_gated(nap)
        log(f"🎯 Охота: набрал {len(names)} слабых по защите, мои HP {my_hp} — "
            f"{', '.join(names)}")
        for t in names:
            if self.control_state() != "run":
                return None   # пульт переключили — уходим на gate() в начале цикла
            if await self._bomb_guard_tick():
                return None   # 💣 бочка важнее охоты — обработали, прерываем проход
            my_after = await self.do_target(t)
            self._last_hit_name = t
            if isinstance(my_after, int) and my_after <= s["my_min_hp"]:
                log(f"🩸 После удара мои HP {my_after} ≤ {s['my_min_hp']} — прерываю охоту на лечение")
                return None
            await self.inter_hit()
        return None

    def _rotate_after_last(self, ordered):
        """Начать список с цели ПОСЛЕ последней обработанной — продолжить, а не сначала.
        Так после лечения бот доходит список с места остановки, потом идёт по кругу."""
        last = self._last_hit_name
        if not last or last not in ordered:
            return ordered
        i = ordered.index(last)
        return ordered[i + 1:] + ordered[:i + 1]

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
    from holop_reroll import quiet_telethon
    quiet_telethon(log)   # шум telethon → одна понятная строка в smash.log
    await client.start()
    # 🔒 доступ только участникам закрытой группы (анти-кража)
    from access import enforce_access
    if not await enforce_access(client, log):
        await client.disconnect()
        return
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
            try:
                bot._save_cd_cache(force=True)   # сохранить КД целей перед выходом
            except Exception:
                pass
        await client.disconnect()


def _write_crash(exc):
    """Пишет полную ошибку смашера в hub_error.log — чтобы её можно было прислать."""
    import traceback
    import platform
    try:
        with open(os.path.join(HERE, "hub_error.log"), "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 64 + "\n")
            f.write("ВЫЛЕТ НАБЕГОВ (holop_smash)  " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            f.write("Python: " + sys.version.replace("\n", " ") + "\n")
            f.write("OS: " + platform.platform() + "\n")
            f.write("-" * 64 + "\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            f.write("=" * 64 + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"🛑 Набеги упали: {type(e).__name__}: {e} — записал в hub_error.log")
        _write_crash(e)
