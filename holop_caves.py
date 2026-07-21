#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOLOP CAVES — автопроход пещер @holop по порядку + отложенное напоминание.

Что делает (проверено вживую 07.07.2026 на Пещере Стража):
  1) заходит в меню «Пещеры»;
  2) по очереди берёт все три пещеры (Тёмная → Славы → Стража);
     — пещеру на кулдауне («· ⏳ 23ч 55м») пропускает;
     — доступную проходит: жмёт АТАКОВАТЬ → Спуститься глубже до 10-го уровня
       (Чернобог), пока шанс победы и HP выше порогов; потом Забрать добычу →
       Забрать награды;
  3) после прохода читает кулдауны всех трёх пещер и ставит ОТЛОЖЕННОЕ сообщение
     (напоминалку), чтобы ты не забыл вернуться, когда пещеры снова откроются.

Механика пещеры (одинаковая во всех трёх):
  • 10 уровней, лут удваивается с каждым уровнем; на 10-м — босс Чернобог + предмет.
  • Экран боя: «⚔️ БОЙ В ПЕЩЕРЕ | Уровень N», «📊 Шанс победы: ~95%», «❤️ HP 60/100».
    Кнопки: [АТАКОВАТЬ, (Забрать добычу с ур.2), Отступить].
  • После победы: «✅ ПОБЕДА», кнопки [Спуститься глубже, Забрать добычу (X)].
  • После босса — только [Забрать добычу]. Дальше «🎉 УСПЕШНЫЙ ПОХОД»
    [Забрать награды, Вернуться в бой]. Забрать награды → назад в меню, кулдаун 24ч.

Скрипт НЕ тратит звёзды. Отступает/забирает лут, если следующий бой рискован
(шанс победы < --min-win или HP < --min-hp) — банкует, а не сливает поход.

Запуск:
    python3.11 holop_caves.py                 # пройти все доступные пещеры + напоминалка
    python3.11 holop_caves.py --dry-run        # только показать статус пещер и план, ничего не жать
    python3.11 holop_caves.py --min-win 90     # спускаться глубже только пока шанс ≥ 90%
    python3.11 holop_caves.py --min-hp 30      # не лезть глубже, если HP < 30
    python3.11 holop_caves.py --max-level 8    # банкить лут не доходя до босса (стоп на 8)
    python3.11 holop_caves.py --only Стража     # пройти только одну пещеру по подстроке имени
    python3.11 holop_caves.py --no-reminder     # не ставить отложенное напоминание
    python3.11 holop_caves.py --clear-reminders # снять ранее поставленные напоминалки и выйти
"""

import argparse
import asyncio
import json
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

HERE = os.path.dirname(os.path.abspath(__file__))
MSK = timezone(timedelta(hours=3))

# Windows-консоль (cp1251) рушит эмодзи в выводе — переводим в UTF-8 с заменой.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── хрупкие строки игры (правь только тут, если игра обновится) ───────────────
CMD_CAVES        = "Пещеры"
MENU_MARKER      = "ПЕЩЕРЫ"            # маркер экрана меню пещер
BATTLE_MARKER    = "БОЙ В ПЕЩЕРЕ"      # маркер экрана боя
VICTORY_MARKER   = "ПОБЕДА"           # «✅ ПОБЕДА!»
MARCH_MARKER     = "ПОХОД"            # «🎉 УСПЕШНЫЙ ПОХОД!» / итог похода
BTN_ATTACK       = "АТАКОВАТЬ"
BTN_DEEPER       = "Спуститься глубже"
BTN_TAKE_LOOT    = "Забрать добычу"
BTN_CLAIM        = "Забрать награды"
BTN_RETREAT      = "Отступить"

# порядок и опознание трёх пещер (по подстроке в тексте кнопки)
CAVES = [
    ("Тёмная пещера", "Тёмная"),
    ("Пещера славы",  "славы"),
    ("Пещера Стража", "Стража"),
]
COOLDOWN_MARK = "⏳"                   # если в кнопке есть — пещера на кулдауне

REMINDER_PREFIX = "🕳️"                # по нему узнаём/пересоздаём СВОИ напоминалки
                                       # (охранные будильники используют 🚨 — их не трогаем)
MIN_FUTURE_SEC  = 70                   # отложенное сообщение должно быть заметно в будущем

WIN_RE   = re.compile(r"Шанс победы:\s*~?(\d+)")
HP_RE    = re.compile(r"(\d+)\s*/\s*100")   # первым в тексте идёт HP игрока «60/100»
LEVEL_RE = re.compile(r"Уровень\s*(\d+)")
# кулдаун в кнопке: «Тёмная пещера · ⏳ 23ч 52м» / «⏳ 45м» / «⏳ 2ч» — часы и минуты по отдельности
CD_H_RE  = re.compile(r"(\d+)\s*ч")
CD_M_RE  = re.compile(r"(\d+)\s*м")


def load_config():
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        print("Нет config.json — скопируй config.example.json в config.json.")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def human_minutes(m):
    m = int(round(m))
    h, mm = divmod(m, 60)
    if h and mm:
        return f"{h}ч {mm}м"
    if h:
        return f"{h}ч"
    return f"{mm}м"


def parse_cooldown_minutes(button_text):
    """Из текста кнопки пещеры вернуть остаток кулдауна в минутах, либо 0 если доступна."""
    if COOLDOWN_MARK not in button_text:
        return 0
    tail = button_text.split(COOLDOWN_MARK, 1)[1]
    mh = CD_H_RE.search(tail)
    mm = CD_M_RE.search(tail)
    total = (int(mh.group(1)) if mh else 0) * 60 + (int(mm.group(1)) if mm else 0)
    # на кулдауне, но время не распозналось — считаем «занята» (1 мин), чтобы не входить
    return total if total > 0 else 1


def first_int(regex, text, default=None):
    m = regex.search(text or "")
    return int(m.group(1)) if m else default


# ════════════════════════════════════════════════════════════════════════════
#  РАБОТА С ИГРОЙ
# ════════════════════════════════════════════════════════════════════════════
class CaveRunner:
    def __init__(self, client, cfg, args):
        self.c = client
        self.bot = cfg.get("bot_username", "holop")
        self.lo = float(cfg.get("min_delay", 0.8))
        self.hi = float(cfg.get("max_delay", 1.8))
        self.args = args

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

    async def recent(self, limit=6):
        return await self._flood(self.c.get_messages(self.bot, limit=limit))

    async def send(self, text):
        return await self._flood(self.c.send_message(self.bot, text))

    def flat_buttons(self, msg):
        out = []
        if msg and msg.buttons:
            for r, row in enumerate(msg.buttons):
                for col, b in enumerate(row):
                    out.append((r, col, (b.text or "")))
        return out

    def find_button(self, msg, substr):
        for r, col, t in self.flat_buttons(msg):
            if substr.lower() in t.lower():
                return (r, col, t)
        return None

    async def click(self, msg, substr):
        b = self.find_button(msg, substr)
        if not b:
            return False
        await self._flood(msg.click(b[0], b[1]))
        await self.pause()
        return True

    async def latest(self):
        """Самое свежее (макс. id) ВХОДЯЩЕЕ сообщение бота, либо None.

        Каждое действие в пещере удаляет старый экран и постит новый с бóльшим id,
        поэтому «текущий экран» = сообщение бота с максимальным id.
        """
        bot = [m for m in await self.recent(8) if not m.out]
        return max(bot, key=lambda m: m.id) if bot else None

    async def wait_screen(self, after_id, want=None, predicate=None, tries=24, delay=0.6):
        """Дождаться нового экрана бота (id > after_id).

        Игнорирует ПОСТОРОННИЕ сообщения бота (уведомления об атаках, «Захват!»,
        сборе на замок и т.п.), которые прилетают посреди похода и иначе были бы
        приняты за «самое свежее» сообщение. Среди подходящих берём макс. id.
        """
        for _ in range(tries):
            cands = [m for m in await self.recent(8) if not m.out and m.id > after_id]
            if want is not None:
                cands = [m for m in cands if want in (m.message or "")]
            if predicate is not None:
                cands = [m for m in cands if predicate(m)]
            if cands:
                return max(cands, key=lambda m: m.id)
            await asyncio.sleep(delay)
        return None

    def is_menu(self, msg):
        return bool(msg) and MENU_MARKER in (msg.message or "")

    def is_cave_screen(self, msg):
        """True, если сообщение — экран пещеры (бой/победа/итог), а не постороннее."""
        if not msg:
            return False
        t = msg.message or ""
        if BATTLE_MARKER in t or VICTORY_MARKER in t or MARCH_MARKER in t:
            return True
        return bool(self.find_button(msg, BTN_ATTACK)
                    or self.find_button(msg, BTN_DEEPER)
                    or self.find_button(msg, BTN_TAKE_LOOT)
                    or self.find_button(msg, BTN_CLAIM))

    def in_expedition(self, msg):
        """Экран внутри похода: бой, победа или итоговый экран с наградами."""
        return self.is_cave_screen(msg)

    async def open_menu(self, tries=24):
        """Отправить «Пещеры» и вернуть свежий экран: меню ИЛИ (если идёт поход) экран боя."""
        sent = await self.send(CMD_CAVES)
        for _ in range(tries):
            m = await self.latest()
            if m and m.id > sent.id and (self.is_menu(m) or self.in_expedition(m)):
                return m
            await asyncio.sleep(0.6)
        raise RuntimeError("Не дождался ответа на «Пещеры»")

    def cave_status(self, menu_msg):
        """[(display_name, cooldown_min, (r,col))] для всех трёх пещер из меню."""
        out = []
        flat = self.flat_buttons(menu_msg)
        for display, key in CAVES:
            hit = None
            for r, col, t in flat:
                if key.lower() in t.lower():
                    hit = (r, col, t)
                    break
            if hit:
                out.append((display, parse_cooldown_minutes(hit[2]), (hit[0], hit[1])))
            else:
                out.append((display, None, None))   # кнопки нет вовсе
        return out

    # ── вход в пещеру по имени: открыть меню, кликнуть нужную кнопку ──────────
    async def enter_cave(self, cave_key):
        """Открыть меню, кликнуть пещеру по подстроке cave_key, вернуть (экран_боя, cd_min).

        Возврат: (msg, 0) — вошли в бой; (None, cd) — пещера на кулдауне cd мин;
                 (None, None) — кнопки нет или войти не удалось.
        """
        for attempt in range(3):
            menu = await self.open_menu()
            if self.in_expedition(menu):        # мы уже внутри похода (недобитый прошлый)
                return (menu, 0)
            # найти кнопку этой пещеры в свежем меню
            hit = None
            for r, col, t in self.flat_buttons(menu):
                if cave_key.lower() in t.lower():
                    hit = (r, col, t)
                    break
            if not hit:
                return (None, None)
            cd = parse_cooldown_minutes(hit[2])
            if cd > 0:
                return (None, cd)
            await self._flood(menu.click(hit[0], hit[1]))
            await self.pause()
            msg = await self.wait_screen(menu.id, predicate=self.is_cave_screen, tries=20)
            if msg and self.in_expedition(msg):
                return (msg, 0)
            if attempt < 2:
                print(f"      ↻ вход не зарегистрировался, повтор ({attempt + 1}/2)")
        return (None, None)

    # ── прогон похода до конца (с любого экрана внутри пещеры) ────────────────
    async def run_expedition(self, msg, start_level=0):
        """Довести поход до Забрать награды. Вернуть dict-итог."""
        steps = 0
        top_level = start_level
        while steps < 40:
            steps += 1
            text = msg.message or ""
            cur_id = msg.id   # ждём ответ строго новее текущего экрана

            # 1) экран итога похода — забрать награды и выйти
            if self.find_button(msg, BTN_CLAIM):
                await self.click(msg, BTN_CLAIM)
                await self.pause()
                gold = None
                m = re.search(r"\+([\d\s]+)🪙", text)
                if m:
                    gold = int(m.group(1).replace(" ", ""))
                m2 = re.search(r"уровень:\s*(\d+)", text, re.IGNORECASE)
                if m2:
                    top_level = max(top_level, int(m2.group(1)))
                return {"ok": True, "level": top_level, "gold": gold, "raw": text.strip()}

            # 2) победа посреди пещеры — решаем спускаться или банковать
            if VICTORY_MARKER in text and self.find_button(msg, BTN_DEEPER):
                nxt_win = first_int(WIN_RE, text, 100)
                hp = first_int(HP_RE, text, 100)
                lvl = top_level
                safe = (nxt_win >= self.args.min_win
                        and hp >= self.args.min_hp
                        and (self.args.max_level <= 0 or lvl < self.args.max_level))
                if safe:
                    await self.click(msg, BTN_DEEPER)
                else:
                    reason = ("достигнут стоп-уровень" if self.args.max_level and lvl >= self.args.max_level
                              else f"шанс {nxt_win}% или HP {hp} ниже порога")
                    print(f"      ⤵️ банкую лут ({reason})")
                    await self.click(msg, BTN_TAKE_LOOT)
                msg = await self.wait_screen(cur_id, predicate=self.is_cave_screen, tries=20)
                if not msg:
                    return {"ok": False, "reason": "нет ответа после победы", "level": top_level}
                continue

            # 3) экран боя — оценить риск и атаковать (или отступить/забрать)
            if BATTLE_MARKER in text:
                lvl = first_int(LEVEL_RE, text, top_level)
                top_level = max(top_level, lvl or 0)
                win = first_int(WIN_RE, text, 100)
                hp = first_int(HP_RE, text, 100)
                if win < self.args.min_win or hp < self.args.min_hp:
                    print(f"      🛑 ур.{lvl}: шанс {win}%, HP {hp} — не атакую, выхожу")
                    if not await self.click(msg, BTN_TAKE_LOOT):
                        await self.click(msg, BTN_RETREAT)
                else:
                    print(f"      ⚔️ ур.{lvl}: шанс {win}%, HP {hp} — атакую")
                    await self.click(msg, BTN_ATTACK)
                msg = await self.wait_screen(cur_id, predicate=self.is_cave_screen, tries=20)
                if not msg:
                    return {"ok": False, "reason": "нет ответа после хода", "level": top_level}
                continue

            # 4) босс повержен: только «Забрать добычу», без «Спуститься глубже»
            if self.find_button(msg, BTN_TAKE_LOOT):
                await self.click(msg, BTN_TAKE_LOOT)
                msg = await self.wait_screen(cur_id, predicate=self.is_cave_screen, tries=20)
                if not msg:
                    return {"ok": False, "reason": "нет ответа после боя", "level": top_level}
                continue

            # ничего не распознали
            return {"ok": False, "reason": "не понял экран",
                    "level": top_level, "raw": text[:200]}

        return {"ok": False, "reason": "слишком много шагов", "level": top_level}


# ════════════════════════════════════════════════════════════════════════════
#  ОТЛОЖЕННЫЕ НАПОМИНАНИЯ
# ════════════════════════════════════════════════════════════════════════════
async def clear_reminders(client, bot):
    try:
        sched = await client(GetScheduledHistoryRequest(peer=bot, hash=0))
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ не смог прочитать отложенные: {e}")
        return 0
    ids = [m.id for m in sched.messages if (m.message or "").startswith(REMINDER_PREFIX)]
    if ids:
        await client(DeleteScheduledMessagesRequest(peer=bot, id=ids))
    return len(ids)


def build_reminder(statuses, now, lead_min):
    """По статусам пещер [(name, cd_min, rc)] вернуть (send_at_utc, text) напоминалки.

    Ставим на момент, когда откроется ПОСЛЕДНЯЯ из закрытых пещер (минус lead),
    чтобы к приходу напоминания все были доступны. Если все уже открыты — None.
    """
    locked = [(name, cd) for name, cd, rc in statuses if cd and cd > 0]
    if not locked:
        return None
    ready_at = {name: now + timedelta(minutes=cd) for name, cd in locked}
    fire = max(ready_at.values()) - timedelta(minutes=lead_min)
    if (fire - now).total_seconds() < MIN_FUTURE_SEC:
        fire = now + timedelta(seconds=MIN_FUTURE_SEC)
    lines = [f"• {name} — {ready_at[name].astimezone(MSK):%H:%M} МСК" for name, _ in locked]
    text = (f"{REMINDER_PREFIX} Пещеры снова доступны — пора в поход!\n"
            + "\n".join(lines)
            + "\n\nЗапусти проход: python3.11 holop_caves.py")
    return (fire, text)


# ════════════════════════════════════════════════════════════════════════════
#  ГЛАВНЫЙ СЦЕНАРИЙ
# ════════════════════════════════════════════════════════════════════════════
async def run(cfg, args):
    if cfg.get("session_string"):
        client = TelegramClient(StringSession(cfg["session_string"]),
                                int(cfg["api_id"]), cfg["api_hash"])
    else:
        session = os.path.join(HERE, cfg.get("session_name", "holop_session"))
        client = TelegramClient(session, int(cfg["api_id"]), cfg["api_hash"])
    await client.start()
    bot = cfg.get("bot_username", "holop")
    try:
        if args.clear_reminders:
            n = await clear_reminders(client, bot)
            print(f"Снято напоминалок: {n}")
            return

        runner = CaveRunner(client, cfg, args)
        menu = await runner.open_menu()

        # если застряли в недобитом походе — сначала доводим его до конца
        if runner.in_expedition(menu):
            print("⚠️  Обнаружен незавершённый поход — довожу его до конца…")
            res = await runner.run_expedition(menu)
            g = res.get("gold")
            print(f"   {'✅' if res.get('ok') else '❌'} прошлый поход: "
                  f"{res.get('reason','завершён')}, добыча "
                  f"{(str(g)+'🪙') if g else '?'}")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            menu = await runner.open_menu()

        statuses = runner.cave_status(menu)

        print("──── СТАТУС ПЕЩЕР ────")
        for name, cd, rc in statuses:
            if rc is None:
                print(f"  {name}: кнопки нет")
            elif cd and cd > 0:
                print(f"  {name}: ⏳ кулдаун {human_minutes(cd)}")
            else:
                print(f"  {name}: ✅ доступна")
        print("─────────────────────\n")

        # какие пещеры проходим (имя + подстрока-ключ для поиска кнопки)
        key_of = {display: key for display, key in CAVES}
        todo = []
        for name, cd, rc in statuses:
            if rc is None or (cd and cd > 0):
                continue
            if args.only and args.only.lower() not in name.lower():
                continue
            todo.append((name, key_of.get(name, name)))

        if args.dry_run:
            print("DRY-RUN: пройти сейчас можно:",
                  ", ".join(n for n, _ in todo) or "— нечего")
            rem = build_reminder(statuses, datetime.now(timezone.utc), args.lead)
            if rem and not args.no_reminder:
                fire, text = rem
                print(f"\nПлан напоминалки на {fire.astimezone(MSK):%d.%m %H:%M} МСК:")
                for line in text.splitlines():
                    print(f"   {line}")
            return

        # проходим доступные пещеры по очереди
        for name, key in todo:
            print(f"🕳️  ЗАХОЖУ: {name}")
            msg, cd = await runner.enter_cave(key)
            if msg is None:
                if cd:
                    print(f"   ⏳ {name} уже на кулдауне {human_minutes(cd)}, пропускаю")
                else:
                    print(f"   ⚠️ не смог войти в {name}, пропускаю")
                continue
            result = await runner.run_expedition(msg)
            if result.get("ok"):
                g = result.get("gold")
                gold_s = f"{g:,}🪙".replace(",", " ") if g else "?"
                print(f"   ✅ {name}: поход завершён, ур.{result.get('level','?')}, добыча {gold_s}")
            else:
                print(f"   ❌ {name}: {result.get('reason')} "
                      f"(ур.{result.get('level','?')})")
            await asyncio.sleep(random.uniform(1.0, 2.0))

        if not todo:
            print("Сейчас все пещеры на кулдауне — проходить нечего.")

        # ставим напоминалку по свежему меню
        if not args.no_reminder:
            menu = await runner.open_menu()
            statuses = runner.cave_status(menu)
            now = datetime.now(timezone.utc)
            rem = build_reminder(statuses, now, args.lead)
            if rem:
                fire, text = rem
                removed = await clear_reminders(client, bot)
                if removed:
                    print(f"  снял старых напоминалок: {removed}")
                await client.send_message(bot, text, schedule=fire)
                print(f"\n⏰ Напоминалка поставлена на "
                      f"{fire.astimezone(MSK):%d.%m %H:%M} МСК "
                      f"(через {human_minutes((fire - now).total_seconds() / 60)}). "
                      f"Telegram пришлёт её сам.")
            else:
                print("\nВсе пещеры уже открыты — напоминалку не ставлю.")
    finally:
        await client.disconnect()


async def main():
    ap = argparse.ArgumentParser(description="Автопроход пещер @holop + отложенное напоминание")
    ap.add_argument("--dry-run", action="store_true", help="только показать статус и план")
    ap.add_argument("--min-win", type=int, default=85, help="не лезть глубже, если шанс победы < N%% (по умолчанию 85)")
    ap.add_argument("--min-hp", type=int, default=20, help="не лезть глубже, если HP < N (по умолчанию 20)")
    ap.add_argument("--max-level", type=int, default=10, help="стоп-уровень: банкить лут на нём (0 = без лимита, по умолчанию 10)")
    ap.add_argument("--only", type=str, default=None, help="пройти только одну пещеру по подстроке имени")
    ap.add_argument("--lead", type=int, default=0, help="за сколько минут ДО открытия слать напоминалку (по умолчанию 0)")
    ap.add_argument("--no-reminder", action="store_true", help="не ставить отложенное напоминание")
    ap.add_argument("--clear-reminders", action="store_true", help="снять ранее поставленные напоминалки и выйти")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        print("Заполни api_id и api_hash в config.json.")
        sys.exit(1)

    await run(cfg, args)


if __name__ == "__main__":
    asyncio.run(main())
