#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOLOP ALARMS — будильники охраны через ОТЛОЖЕННЫЕ сообщения Telegram.

Идея (работает, даже когда твой компьютер выключен — расписание держит сам Telegram):
  • по твоей команде скрипт заходит в @holop, проходит ВСЕ страницы «Мои холопы»;
  • у каждого холопа читает остаток щита с кнопки охраны («19ч 35м»);
  • на момент «щит спадёт минус N минут» ставит ОТЛОЖЕННОЕ сообщение-мигалку
    прямо в чат с ботом, например:
        🚨🚨🚨 Через 5 минут слетит защита у холопа Московия! Срочно ставь охрану!!!
  • когда Telegram сам отправит это сообщение — тебе придёт уведомление.

Скрипт ничего не выгоняет и не захватывает — только читает щиты и планирует напоминания.
Каждый запуск СНАЧАЛА удаляет свои прошлые будильники (с префиксом 🚨) и ставит свежие —
так список всегда актуален, дубликатов не копится.

Запуск:
    python3.11 holop_alarms.py            # прочитать щиты и поставить будильники
    python3.11 holop_alarms.py --dry-run  # только показать план, ничего не ставить
    python3.11 holop_alarms.py --lead 10  # будить за 10 минут до спада (по умолчанию 5)
    python3.11 holop_alarms.py --clear     # снять все ранее поставленные будильники
"""

import argparse
import asyncio
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.messages import (
    GetScheduledHistoryRequest,
    DeleteScheduledMessagesRequest,
)

# переиспеользуем проверенные строки игры и парсеры из основного скрипта
from holop_reroll import (
    UI, HERE, PROFESSIONS,
    clean_name, parse_profession, parse_pages, load_config,
)

MSK = timezone(timedelta(hours=3))   # московское время для вывода

ALERT_PREFIX = "🚨"        # по нему узнаём СВОИ будильники (чтобы пересоздавать, не трогая чужое)
DEFAULT_LEAD_MIN = 5       # за сколько минут до спада щита будить
GROUP_BUCKET_MIN = 5       # холопов с близким временем спада объединяем в одно сообщение
MIN_FUTURE_SEC = 70        # отложенное сообщение должно быть хотя бы немного в будущем

# кнопка охраны бывает в двух форматах (игра меняла):
#   абсолютный:  «до 15:56»  → щит держится до ЧЧ:ММ (МСК)
#   относительный: «19ч 35м» → остаток времени
TIME_RE = re.compile(r"^\s*(?:(\d+)\s*ч)?\s*(?:(\d+)\s*м)?\s*$")
UNTIL_RE = re.compile(r"^\s*до\s*(\d{1,2}):(\d{2})\s*$")


# ════════════════════════════════════════════════════════════════════════════
#  ПАРСЕРЫ
# ════════════════════════════════════════════════════════════════════════════
def parse_guard_button(text):
    """Распознать кнопку охраны холопа.

    ("until", (h, m)) — щит активен до ЧЧ:ММ МСК («до 15:56»);
    ("relmin", mins)  — щит активен, остаток времени («19ч 35м»);
    ("guard", 0)      — щит спал, кнопка «Охрана (120🏅)»;
    (None, None)      — это не кнопка охраны.
    """
    t = (text or "").strip()
    if UI["btn_guard"] in t and UI["star"] not in t and UI["potion"] not in t:
        return ("guard", 0)
    mu = UNTIL_RE.match(t)
    if mu:
        return ("until", (int(mu.group(1)), int(mu.group(2))))
    m = TIME_RE.match(t)
    if m and (m.group(1) or m.group(2)):
        return ("relmin", int(m.group(1) or 0) * 60 + int(m.group(2) or 0))
    return (None, None)


def name_matches(button_text, name):
    """Совпадает ли кнопка-имя с именем холопа (имя в кнопке может быть обрезано «…»)."""
    b = clean_name(button_text)
    if b == name:
        return True
    if b.endswith("…") and name.startswith(b[:-1].strip()):
        return True
    return False


def holopy_on_page(text):
    """Список (имя, профессия) для всех холопов на странице (без фильтра по профессии)."""
    out = []
    for blk in (text or "").split(UI["block_sep"])[1:]:
        lines = blk.splitlines()
        name = clean_name(lines[0]) if lines else ""
        if name:
            out.append((name, parse_profession(blk)))
    return out


def human_minutes(m):
    h, mm = divmod(int(m), 60)
    if h and mm:
        return f"{h}ч {mm}м"
    if h:
        return f"{h}ч"
    return f"{mm}м"


def prof_part(prof):
    """«⚔️ Воин» или «?», если профессию не распознали."""
    if not prof:
        return "?"
    emoji = PROFESSIONS.get(prof, "")
    return f"{emoji} {prof}".strip()


def build_alarms(shields, now, lead_min):
    """По [(nick, prof, expiry_utc)] вернуть список (send_at_utc, text) будильников.

    expiry_utc — datetime окончания защиты, либо None (щит уже слетел).
    Текст — построчно: «Ник - профессия - HH:MM МСК (когда заканчивается защита)».
    send_at = момент_спада − lead. Холопов с близким временем спада объединяем в одно
    сообщение. Уже слетевшие идут отдельным «срочным» сообщением.
    """
    head = ALERT_PREFIX * 3
    expired = [(n, p) for n, p, exp in shields if exp is None]
    active = [(n, p, exp) for n, p, exp in shields if exp is not None]

    alarms = []

    # уже без щита — будим прямо сейчас (через минуту)
    if expired:
        send_at = now + timedelta(seconds=MIN_FUTURE_SEC)
        lines = [f"{n} - {prof_part(p)} - защита УЖЕ слетела" for n, p in expired]
        alarms.append((send_at,
                       f"{head} Срочно ставь охрану — у этих холопов защита слетела:\n"
                       + "\n".join(lines)))

    # активные щиты — группируем по округлённому времени спада
    buckets = {}
    for nick, prof, expiry in active:
        send_at = expiry - timedelta(minutes=lead_min)
        if (send_at - now).total_seconds() < MIN_FUTURE_SEC:
            send_at = now + timedelta(seconds=MIN_FUTURE_SEC)
        key = round(send_at.timestamp() / (GROUP_BUCKET_MIN * 60))
        b = buckets.setdefault(key, {"send_at": send_at, "items": []})
        b["send_at"] = min(b["send_at"], send_at)
        b["items"].append((nick, prof, expiry))

    for key in sorted(buckets):
        info = buckets[key]
        lines = [
            f"{n} - {prof_part(p)} - {exp.astimezone(MSK):%H:%M} МСК"
            for n, p, exp in sorted(info["items"], key=lambda x: x[2])
        ]
        alarms.append((info["send_at"],
                       f"{head} Срочно ставь охрану — скоро слетит защита:\n"
                       + "\n".join(lines)))

    return sorted(alarms, key=lambda a: a[0])


# ════════════════════════════════════════════════════════════════════════════
#  ЧТЕНИЕ ЩИТОВ
# ════════════════════════════════════════════════════════════════════════════
class ShieldReader:
    def __init__(self, client, cfg):
        self.c = client
        self.cfg = cfg
        self.bot = cfg.get("bot_username", "holop")
        self.lo = float(cfg.get("min_delay", 0.8))
        self.hi = float(cfg.get("max_delay", 1.8))
        self.list_id = None

    async def pause(self):
        await asyncio.sleep(random.uniform(self.lo, self.hi))

    async def _flood(self, coro):
        while True:
            try:
                return await coro
            except FloodWaitError as e:
                wait = e.seconds + random.uniform(1, 3)
                print(f"  ⏳ FloodWait: жду {wait:.0f}с")
                await asyncio.sleep(wait)

    async def recent(self, limit=12):
        return await self.c.get_messages(self.bot, limit=limit)

    async def refetch(self, msg_id):
        return await self.c.get_messages(self.bot, ids=msg_id)

    async def send(self, text):
        return await self._flood(self.c.send_message(self.bot, text))

    def flat_buttons(self, msg):
        out = []
        if msg and msg.buttons:
            for r, row in enumerate(msg.buttons):
                for col, b in enumerate(row):
                    out.append((r, col, (b.text or "")))
        return out

    async def wait_for(self, contains, min_id=0, tries=12, delay=0.5):
        for _ in range(tries):
            for m in await self.recent(12):
                if m.out:
                    continue
                t = m.message or ""
                if contains and contains not in t:
                    continue
                if m.id < min_id:
                    continue
                if m.id == min_id and not contains:
                    continue
                return m
            await asyncio.sleep(delay)
        return None

    async def open_list(self):
        sent = await self.send(UI["cmd_holopy"])
        m = await self.wait_for(UI["menu_marker"], min_id=sent.id, tries=8) \
            or await self.wait_for(UI["list_marker"], min_id=sent.id, tries=8)
        if not m:
            raise RuntimeError("Не дождался ответа на «Холопы»")
        if UI["list_marker"] in (m.message or ""):
            return m
        for r, col, t in self.flat_buttons(m):
            if UI["btn_open_list"] in t:
                await self._flood(m.click(r, col))
                await self.pause()
                break
        lst = await self.wait_for(UI["list_marker"], tries=12)
        if not lst:
            raise RuntimeError("Не открылся список «Мои холопы»")
        return lst

    async def _page_next(self, msg):
        for r, col, t in self.flat_buttons(msg):
            if t.strip() == UI["page_next"]:
                await self._flood(msg.click(r, col))
                await self.pause()
                return True
        return False

    def guard_control_for(self, msg, nick):
        """(kind, minutes) кнопки охраны для холопа nick, либо (None, None)."""
        flat = self.flat_buttons(msg)
        name_idx = None
        for i, (r, col, t) in enumerate(flat):
            if name_matches(t, nick):
                name_idx = i
                break
        if name_idx is None:
            return (None, None)
        for j in range(name_idx - 1, -1, -1):   # кнопка охраны — ПЕРЕД именем
            kind, val = parse_guard_button(flat[j][2])
            if kind:
                return (kind, val)
        return (None, None)

    @staticmethod
    def expiry_utc(kind, val, now):
        """Абсолютное время окончания защиты (UTC) или None, если щит уже слетел."""
        if kind == "guard":
            return None
        if kind == "relmin":
            return now + timedelta(minutes=val)
        if kind == "until":
            h, m = val
            now_msk = now.astimezone(MSK)
            cand = now_msk.replace(hour=h, minute=m, second=0, microsecond=0)
            if cand <= now_msk:                 # время уже прошло сегодня → это завтра
                cand += timedelta(days=1)
            return cand.astimezone(timezone.utc)
        return None

    async def read_all_shields(self, now):
        """Вернуть [(nick, prof, expiry_utc)] по всем холопам (None = щит уже слетел)."""
        shields = []
        seen = set()
        lst = await self.open_list()
        self.list_id = lst.id
        _, total = parse_pages(lst.message or "")
        print(f"  список открыт: страниц {total}")

        for page in range(1, total + 1):
            lst = await self.refetch(self.list_id)
            cur, _ = parse_pages(lst.message or "")
            found = 0
            for nick, prof in holopy_on_page(lst.message or ""):
                if nick in seen:
                    continue
                kind, val = self.guard_control_for(lst, nick)
                if kind is None:
                    continue            # нет кнопки охраны (в клане/кандалах) — пропускаем
                seen.add(nick)
                shields.append((nick, prof, self.expiry_utc(kind, val, now)))
                found += 1
            print(f"  стр. {cur}/{total}: холопов с кнопкой охраны {found}")
            if page < total:
                lst = await self.refetch(self.list_id)
                if not await self._page_next(lst):
                    break
        return shields


# ════════════════════════════════════════════════════════════════════════════
#  ОТЛОЖЕННЫЕ СООБЩЕНИЯ
# ════════════════════════════════════════════════════════════════════════════
async def clear_old_alarms(client, bot):
    """Удалить ранее поставленные нами будильники (по префиксу 🚨). Вернуть, сколько удалено."""
    try:
        sched = await client(GetScheduledHistoryRequest(peer=bot, hash=0))
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ не смог прочитать отложенные сообщения: {e}")
        return 0
    ids = [m.id for m in sched.messages if (m.message or "").startswith(ALERT_PREFIX)]
    if ids:
        await client(DeleteScheduledMessagesRequest(peer=bot, id=ids))
    return len(ids)


async def run(cfg, dry, lead_min, clear_only):
    if cfg.get("session_string"):
        client = TelegramClient(StringSession(cfg["session_string"]),
                                int(cfg["api_id"]), cfg["api_hash"])
    else:
        session = os.path.join(HERE, cfg.get("session_name", "holop_session"))
        client = TelegramClient(session, int(cfg["api_id"]), cfg["api_hash"])
    await client.start()
    bot = cfg.get("bot_username", "holop")
    try:
        if clear_only:
            n = await clear_old_alarms(client, bot)
            print(f"Снято будильников: {n}")
            return

        now = datetime.now(timezone.utc)
        reader = ShieldReader(client, cfg)
        shields = await reader.read_all_shields(now)
        if not shields:
            print("Не нашёл холопов с кнопкой охраны — нечего планировать.")
            return

        alarms = build_alarms(shields, now, lead_min)

        print("\n──── ПЛАН БУДИЛЬНИКОВ ────")
        for send_at, text in alarms:
            mins = max(0, (send_at - now).total_seconds() / 60)
            print(f"  ⏰ отправится {send_at.astimezone(MSK):%d.%m %H:%M} МСК "
                  f"(через {human_minutes(mins)}):")
            for line in text.splitlines():
                print(f"     {line}")
            print()
        print("──────────────────────────\n")

        if dry:
            print("DRY-RUN: ничего не поставлено. Убери --dry-run, чтобы реально запланировать.")
            return

        removed = await clear_old_alarms(client, bot)
        if removed:
            print(f"  снял старых будильников: {removed}")
        for send_at, text in alarms:
            await client.send_message(bot, text, schedule=send_at)
            await asyncio.sleep(random.uniform(0.3, 0.7))
        print(f"✅ Поставлено будильников: {len(alarms)}. "
              f"Telegram отправит их сам — даже если комп выключен.")
    finally:
        await client.disconnect()


async def main():
    ap = argparse.ArgumentParser(description="Будильники охраны холопов через отложенные сообщения (@holop)")
    ap.add_argument("--dry-run", action="store_true", help="только показать план, ничего не ставить")
    ap.add_argument("--lead", type=int, default=DEFAULT_LEAD_MIN,
                    help=f"за сколько минут до спада щита будить (по умолчанию {DEFAULT_LEAD_MIN})")
    ap.add_argument("--clear", action="store_true", help="снять все ранее поставленные будильники и выйти")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        print("Заполни api_id и api_hash в config.json.")
        sys.exit(1)

    await run(cfg, args.dry_run, args.lead, args.clear)


if __name__ == "__main__":
    asyncio.run(main())
