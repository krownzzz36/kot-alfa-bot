#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Юнит-тесты парсеров на РЕАЛЬНЫХ строках бота @holop (сняты вживую 25.06.2026).
Запуск:  python test_parsers.py      (или: python -m pytest test_parsers.py)

Эти тесты позволяют безопасно править регэкспы/тексты кнопок в UI{} главного
файла и сразу видеть, не сломался ли разбор профессии/охраны/результатов поиска.
"""
from holop_reroll import (
    parse_holop_block, parse_profession, parse_pages,
    result_is_free, result_nick, result_name_matches, clean_name, parse_gold,
)

# ── реальный текст экрана «📋 Мои холопы», страница 6/6 ──
LIST_P6 = (
    "📋 Мои холопы (27)\n💪 Общая сила: 8084\n📄 Страница 6/6 | 💪 Сила ↓\n\n"
    "👤 Яр \n🎭 🔨 Ремесленник (+30% золота)\n   💪 Сила: 194 | 🏅 +30/час\n\n"
    "👤 Malk 🛡\n🎭 ⚔️ Воин (+20% золота, +10 атаки)\n   💪 Сила: 156 | 🏅 +22/час"
)

# ── страница 1/6 ──
LIST_P1 = (
    "📋 Мои холопы (27)\n💪 Общая сила: 8084\n📄 Страница 1/6 | 💪 Сила ↓\n\n"
    "👤 Миру мир 🛡\n🎭 ⚔️ Воин (+20% золота, +10 атаки)\n   💪 Сила: 1442\n\n"
    "👤 Мирон 🛡\n🎭 🧙 Волхв (+50% золота)\n   💪 Сила: 294"
)


def check(name, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")
    assert ok, f"{name}: {got!r} != {want!r}"


def test_all():
    # профессия + статус охраны из списка
    check("Яр profession",      parse_holop_block(LIST_P6, "Яр"),      ("Ремесленник", False))
    check("Malk guarded",       parse_holop_block(LIST_P6, "Malk"),    ("Воин", True))
    check("Миру мир воин",      parse_holop_block(LIST_P1, "Миру мир"), ("Воин", True))
    check("Мирон волхв",        parse_holop_block(LIST_P1, "Мирон"),   ("Волхв", True))
    check("отсутствующий ник",  parse_holop_block(LIST_P6, "НетТакого"), (None, None))

    # страницы
    check("pages 6/6", parse_pages(LIST_P6), (6, 6))
    check("pages 1/6", parse_pages(LIST_P1), (1, 6))
    check("pages none", parse_pages("без страниц"), (1, 1))

    # отдельный парсер профессии — из списка
    check("prof Воин",        parse_profession("🎭 ⚔️ Воин (+20%)"),        "Воин")
    check("prof Волхв",       parse_profession("🎭 🧙 Волхв (+50% золота)"), "Волхв")
    check("prof Лазутчик",    parse_profession("🎭 🗡️ Лазутчик"),           "Лазутчик")
    # из ЭКРАНА ЗАХВАТА — слово «Профессия» НЕ должно мешать (это и был риск пропустить Воина)
    check("захват: Воин",  parse_profession("✅ Ты сделал X своим холопом!\n\n🎭 Профессия: ⚔️ Воин (+20%)"), "Воин")
    check("захват: Пахарь", parse_profession("🎭 Профессия: 👨🌾 Пахарь ( (+50% золота))"),                  "Пахарь")

    # чистка имени
    check("clean щит",    clean_name("Malk 🛡"), "Malk")
    check("clean пробел", clean_name("Яр "),     "Яр")

    # результаты поиска: ник из кнопки
    check("nick Яр",    result_nick("⁨Яр⁩ — охрана 23ч 59м · купи 🧪"),      "Яр")
    check("nick Ярск",  result_nick("⁨Ярск⁩ — охрана до 16:00 · купи 🧪"),   "Ярск")
    check("nick свободен", result_nick("⁨Гридя⁩ — свободен"),                "Гридя")

    # результаты поиска: можно ли захватить бесплатно
    check("под охраной нельзя",  result_is_free("⁨Яр⁩ — охрана 23ч 59м · купи 🧪"), False)
    check("соклановец нельзя",   result_is_free("⁨Trapazoid⁩ — соклановец"),        False)
    check("княжий щит нельзя",   result_is_free("⁨SS⁩ — княжий щит 15ч · снять 💣"), False)
    check("звёздный нельзя",     result_is_free("⁨Цезарь⁩ · ⭐134"),                 False)
    check("свободный можно",     result_is_free("⁨Земля Слав…⁩ — захватить"),       True)

    # совпадение ника с учётом обрезки «…» (это и был баг с «Земля Славянина»)
    check("обрезанный ник matches", result_name_matches("⁨Земля Слав…⁩ — захватить", "Земля Славянина"), True)
    check("короткий ник matches",   result_name_matches("⁨Яр⁩ — свободен", "Яр"),              True)
    check("Ярск ≠ Яр",              result_name_matches("⁨Ярск⁩ — захватить", "Яр"),           False)

    # баланс золота (охрана стоит 120🏅) — не путать с «К сбору»
    check("золото 150",  parse_gold("🏅 Золото: 150\n⛓️ Холопов: 27/27"),          150)
    check("золото 21",   parse_gold("🏅 Золото: 21\n🏅 К сбору: 3246 золота"),      21)
    check("золото 750",  parse_gold("🪙 Серебро: 487.1K\n🏅 Золото: 750\n❤️ HP"),   750)
    check("золото 1.2K", parse_gold("🏅 Золото: 1.2K"),                            1200)
    check("золота нет",  parse_gold("нет баланса"),                               None)

    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")


if __name__ == "__main__":
    test_all()
