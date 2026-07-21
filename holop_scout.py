#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOLOP SCOUT — разведка целей: когда у кого спадёт щит/КД и восстановится HP.

Вставляешь список ников → для каждого бот заходит на арену/в профиль, читает:
  • щит (Полевой щит/Стена/Купол/…) и его остаток,
  • персональный КД («Имя • 2м 53с»),
  • HP (если «Слаб» — прикидывает, когда отрастёт для атаки).
На выходе — список «когда освободится», время АБСОЛЮТНОЕ по МСК (напр. «10:59 МСК»),
плюс по желанию ставит ОТЛОЖЕННЫЕ напоминалки «за N минут: готовься к атаке на X!»
(Telegram пришлёт сам, даже если комп выключен).

Навигацию и парсеры переиспользуем из holop_smash (arena_search, shield_seconds).

Запуск:
    python3.11 holop_scout.py --list scout_targets.txt              # только показать
    python3.11 holop_scout.py --list scout_targets.txt --remind 1   # + напомнить за 1 мин
    python3.11 holop_scout.py --clear                               # снять наши напоминалки
"""

import argparse
import asyncio
import os
import random
import sys
import types
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import (GetScheduledHistoryRequest,
                                            DeleteScheduledMessagesRequest)

from holop_reroll import HERE, load_config
from holop_smash import (Smasher, parse_arena_targets, exact_target, target_positions,
                         button_attackable, classify_block_reason, CD_BTN_RE, parse_duration)

MSK = timezone(timedelta(hours=3))
SCOUT_PREFIX = "⚔️🎯"          # по нему находим/снимаем именно НАШИ напоминалки
READY_HP = 21                  # цель бьётся с 20+ HP → готова, как только доросла до 21
MIN_PER_HP = 1.75              # минут на 1 HP регена цели (реально ~1.5–2)
DEFAULT_LIST = os.path.join(HERE, "scout_targets.txt")


def out(msg):
    print(msg, flush=True)


def read_nicks(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [l.split("#", 1)[0].strip() for l in f if l.split("#", 1)[0].strip()]
    except OSError:
        return []


async def scout_one(bot, nick, now):
    """Вернуть (state_text, expiry_utc|None). expiry — когда цель освободится/будет готова."""
    res = await bot.arena_search(nick)
    if not res:
        return "⁇ не найден на арене", None
    blocks = parse_arena_targets(res.message or "")
    positions = target_positions(bot.flat_buttons(res))
    idx = exact_target(blocks, nick)
    if idx is None or idx >= len(positions):
        return "⁇ нет точного совпадения", None
    b = blocks[idx]
    btn = positions[idx][2]

    if button_attackable(btn):
        hp = b.get("hp")
        return f"🟢 бьётся сейчас (HP {hp})" if hp is not None else "🟢 бьётся сейчас", None

    reason = classify_block_reason(btn)
    if reason == "cooldown":
        secs = parse_duration(btn) or 0
        return "⌛ КД", now + timedelta(seconds=secs)
    if reason == "shield":
        secs = await bot.shield_seconds(nick)
        shield_name = btn.split("•", 1)[-1].strip() if "•" in btn else "щит"
        if secs and secs > 0:
            return f"🛡️ {shield_name}", now + timedelta(seconds=secs)
        return f"🛡️ {shield_name} (таймер не прочитан)", None
    if reason == "weak":
        hp = b.get("hp")
        if hp is not None and hp < READY_HP:
            return f"💤 слаб (HP {hp})", now + timedelta(minutes=(READY_HP - hp) * MIN_PER_HP)
        return "💤 слаб", None
    return "🚫 недоступен (клан/уровень)", None


async def clear_reminders(client, bot):
    try:
        sched = await client(GetScheduledHistoryRequest(peer=bot, hash=0))
    except Exception as e:
        out(f"  ⚠️ не смог прочитать отложенные: {e}")
        return 0
    ids = [m.id for m in sched.messages if (m.message or "").startswith(SCOUT_PREFIX)]
    if ids:
        await client(DeleteScheduledMessagesRequest(peer=bot, id=ids))
    return len(ids)


async def main():
    ap = argparse.ArgumentParser(description="Разведка целей: щиты/КД/HP + напоминалки (@holop)")
    ap.add_argument("--list", dest="listfile", default=DEFAULT_LIST,
                    help="файл со списком ников (по одному в строке)")
    ap.add_argument("--remind", type=int, default=0,
                    help="за сколько минут до освобождения напомнить (0 = не ставить)")
    ap.add_argument("--dry-run", action="store_true", help="показать план напоминалок, но не ставить")
    ap.add_argument("--clear", action="store_true", help="снять ранее поставленные напоминалки и выйти")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        out("Заполни api_id/api_hash в config.json.")
        sys.exit(1)
    if cfg.get("session_string"):
        client = TelegramClient(StringSession(cfg["session_string"]),
                                int(cfg["api_id"]), cfg["api_hash"])
    else:
        session = os.path.join(HERE, cfg.get("session_name", "holop_session"))
        client = TelegramClient(session, int(cfg["api_id"]), cfg["api_hash"])
    await client.start()
    botname = cfg.get("bot_username", "holop")

    try:
        if args.clear:
            n = await clear_reminders(client, botname)
            out(f"🧹 Снято напоминалок: {n}")
            return

        nicks = read_nicks(args.listfile)
        if not nicks:
            out("Список ников пуст — вставь ников в поле и запусти снова.")
            return

        bot = Smasher(client, cfg, types.SimpleNamespace(dry_run=False))
        now = datetime.now(timezone.utc)
        out(f"🎯 РАЗВЕДКА ЦЕЛЕЙ ({len(nicks)}) — время по МСК\n" + "─" * 46)

        results = []
        for nick in nicks:
            try:
                state, expiry = await scout_one(bot, nick, now)
            except Exception as e:
                state, expiry = f"⚠️ ошибка: {type(e).__name__}", None
            if expiry:
                mins = max(0, (expiry - now).total_seconds() / 60)
                out(f"  {nick:<20} {state:<26} → {expiry.astimezone(MSK):%H:%M} МСК (через {mins:.0f}м)")
            else:
                out(f"  {nick:<20} {state}")
            results.append((nick, state, expiry))
            await asyncio.sleep(random.uniform(0.4, 0.9))
        out("─" * 46)

        # напоминалки
        if args.remind and args.remind > 0:
            planned = [(n, e) for n, s, e in results if e]
            if not planned:
                out("Некого напоминать — ни у кого нет активного щита/КД.")
                return
            out(f"\n──── ПЛАН НАПОМИНАЛОК (за {args.remind} мин) ────")
            msgs = []
            for nick, expiry in planned:
                send_at = expiry - timedelta(minutes=args.remind)
                if send_at <= now:
                    send_at = now + timedelta(seconds=20)
                text = (f"{SCOUT_PREFIX} Готовься к атаке на {nick}! "
                        f"Освободится в {expiry.astimezone(MSK):%H:%M} МСК.")
                msgs.append((send_at, text))
                out(f"  ⏰ {send_at.astimezone(MSK):%H:%M} МСК → {nick}")
            if args.dry_run:
                out("DRY-RUN: напоминалки не поставлены.")
                return
            removed = await clear_reminders(client, botname)
            if removed:
                out(f"  снял старых напоминалок: {removed}")
            for send_at, text in msgs:
                await client.send_message(botname, text, schedule=send_at)
                await asyncio.sleep(random.uniform(0.3, 0.7))
            out(f"✅ Поставлено напоминалок: {len(msgs)}. Telegram пришлёт их сам.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
