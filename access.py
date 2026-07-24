#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔒 ДОСТУП «СВОЙ/ЧУЖОЙ» — бот работает только у участников нашей закрытой группы.

Идея Алексея (Енотоград), поддержана Максимом:
    «У скрипта есть полный доступ к телеге — можно после авторизации чекать,
     состоишь ли в данной группе. Если нет — дальше не работать.»
Сервер не нужен, чужие сессии нигде не хранятся — проверка идёт локально.

ЧЕСТНО: это «защита от дурака» (формулировка Алексея). Кто умеет — вырежет.
Смысл — от «переслал архив кому попало», а не от хакеров.

═══ ПОЛЯРНОСТЬ: FAIL-OPEN (важно!) ═══
Блокируем ТОЛЬКО когда Telegram ЯВНО ответил «ты не участник» (UserNotParticipant).
ЛЮБАЯ другая заминка (нет связи, не резолвится группа, обрыв на середине, чужой
формат id, тайм-аут) → ПУСКАЕМ. Причина: раньше было наоборот («пускаем только
при подтверждении»), и у Ксюши с Кариной на нестабильной сети проверка падала не
с сетевой ошибкой → бот их БЛОКИРОВАЛ («бот не работает у обеих»). Ложно рубить
своих — хуже, чем изредка пропустить чужого.
"""

GROUP_ID = -5160104813                       # закрытая группа тестеров
GROUP_HINT = "Альфа-тестеры бота холопа"      # для понятного сообщения

DENY_MESSAGE = (
    f"🔒 ДОСТУП ЗАКРЫТ. Этот бот работает только у участников «{GROUP_HINT}». "
    f"Твой аккаунт в этой группе не найден. Если это ошибка — напиши владельцу, тебя добавят."
)


async def _is_member(client):
    """Вернуть True/False/None: участник / ТОЧНО не участник / не смогли выяснить.
    None ⇒ пускаем (fail-open)."""
    # get_permissions('me') → ChatPermissions если участник, UserNotParticipantError если нет.
    try:
        from telethon.errors import UserNotParticipantError
    except Exception:
        UserNotParticipantError = ()
    try:
        await client.get_permissions(GROUP_ID, "me")
        return True                       # подтверждённый участник
    except Exception as e:
        if UserNotParticipantError and isinstance(e, UserNotParticipantError):
            pass                           # это ЯВНОЕ «не участник» — но перепроверим диалогами
        # иначе — резолв/сеть/квота: не смогли выяснить, НЕ блокируем
    # запасной, самый надёжный признак: группа есть в списке диалогов участника.
    try:
        async for d in client.iter_dialogs(limit=None):
            try:
                if d.id == GROUP_ID or getattr(d.entity, "id", None) in (GROUP_ID, abs(GROUP_ID)):
                    return True
            except Exception:
                continue
        return False                       # прошли ВСЕ диалоги, группы нет → точно не участник
    except Exception:
        return None                        # обрыв на середине / нет связи → не выяснили → пускаем


async def check_access(client):
    """(allowed, message). Fail-open: блок только при ТОЧНОМ «не участник»."""
    try:
        res = await _is_member(client)
    except Exception as e:
        return True, f"⚠️ проверку доступа пропустил ({type(e).__name__}) — продолжаю работу"
    if res is True:
        return True, ""
    if res is None:
        return True, "⚠️ не смог проверить доступ (нет связи) — продолжаю работу"
    return False, DENY_MESSAGE             # res is False → точно не участник


async def enforce_access(client, log_fn=print):
    """Проверить доступ и вернуть True/False. Сообщение печатаем через log_fn."""
    try:
        allowed, msg = await check_access(client)
    except Exception:
        return True                        # что бы ни случилось — не рубим своих
    if msg:
        log_fn(msg)
    return allowed
