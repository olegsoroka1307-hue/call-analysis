#!/usr/bin/env python3
"""Демонстраційний прогін на вигаданій нараді.

Нічого справжнього не торкається: ні Notion, ні Telegram, ні API. Потрібен,
щоб побачити роботу системи до того, як заводити боти й видавати доступи.

Запуск:  python tools/demo_run.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meetings.config import Config  # noqa: E402
from meetings.errorlog import ErrorLog  # noqa: E402
from meetings.extractor import Extractor  # noqa: E402
from meetings.pipeline import handle_callbacks, process_meeting  # noqa: E402
from meetings.registry import Registry  # noqa: E402
from meetings.report import build_rows, render  # noqa: E402
from meetings.telegram import pack_callback  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeAnthropic, FakeNotion, FakeTelegram, callback_update,
    commitment_schema, extraction,
)

OUT = ROOT / "data" / "demo"
TODAY = date(2026, 8, 24)

TEAM = """\
Агенція «Вектор», 8 людей.
Саша Петренко — продажі й комерційні пропозиції.
Марія Коваль — фінанси, рахунки, акти.
Петро Лисенко — сайти й технічна частина.
Коля Гриценко — реклама. «Маркетинг» на нарадах — це він.
"""

TRANSCRIPT = """\
Олег: почнемо. Альфа чекає на пропозицію ще з минулого тижня.
Саша: я скину їм КП до четверга, там лишилось порахувати другий етап.
Олег: добре. Марія, що з актами за серпень?
Марія: підготую до кінця місяця, але мені потрібні цифри від Колі.
Коля: звіт по кампаніях зроблю завтра, тоді Марія матиме все.
Петро: по сайту Дніпро-Буду — форма падає на мобільних, візьму сьогодні.
Олег: це блокує запуск реклами, тож роби першим.
"""

# Що «повернула модель» — у демо це заздалегідь відома відповідь.
MODEL_ANSWER = extraction(
    title="Планірка",
    commitments=[
        commitment_schema(
            responsible="Саша", task="Надіслати КП для Альфи",
            deadline="2026-08-27", priority="Высокий", project="Продажі",
            quote="я скину їм КП до четверга",
        ),
        commitment_schema(
            responsible="Коля", task="Зробити звіт по рекламних кампаніях",
            deadline="2026-08-25", priority="Высокий", project="Реклама",
            quote="звіт по кампаніях зроблю завтра",
        ),
        commitment_schema(
            responsible="Марія", task="Підготувати акти за серпень",
            deadline="2026-08-31", priority="Средний", project="Фінанси",
            quote="підготую до кінця місяця",
        ),
        commitment_schema(
            responsible="Петро", task="Полагодити форму на мобільних",
            deadline="2026-08-24", priority="Высокий", project="Дніпро-Буд",
            quote="форма падає на мобільних, візьму сьогодні",
        ),
    ],
)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        team_context=TEAM,
        notion_database_id="demo-db",
        registry_db=str(OUT / "registry.db"),
        error_log=str(OUT / "errors.jsonl"),
    )
    cfg.validate()
    for stale in (OUT / "registry.db", OUT / "errors.jsonl"):
        stale.unlink(missing_ok=True)

    registry = Registry(cfg.registry_db)
    registry.add("Саша Петренко", 101, "sasha_p")
    registry.add("Марія Коваль", 102)
    registry.add("Коля Гриценко", 103)
    # Петра свідомо немає в реєстрі — так видно, як система про це попереджає.

    errlog = ErrorLog(cfg.error_log)
    notion, telegram = FakeNotion(), FakeTelegram()
    extractor = Extractor(cfg, client=FakeAnthropic(script=[MODEL_ANSWER]))

    print("─" * 62)
    print("ТРАНСКРИПЦІЯ")
    print("─" * 62)
    print(TRANSCRIPT)

    result = process_meeting(
        cfg, TRANSCRIPT, extractor, registry, errlog,
        notion=notion, telegram=telegram, today=TODAY,
    )

    print("─" * 62)
    print("ЗНАЙДЕНІ ДОМОВЛЕНОСТІ")
    print("─" * 62)
    print(f"{'Хто':<16} {'Задача':<38} {'Термін':<12} Пріоритет")
    for c in result.commitments:
        print(f"{c.responsible:<16} {c.task:<38} {c.deadline:<12} {c.priority}")

    print()
    print("─" * 62)
    print("ЩО ЗРОБИЛА СИСТЕМА")
    print("─" * 62)
    for line in result.lines():
        print(line)

    print()
    print("─" * 62)
    print("ПОВІДОМЛЕННЯ, ЯКІ ОТРИМАЛИ ЛЮДИ")
    print("─" * 62)
    for chat_id, text, keyboard in telegram.sent:
        who = next((p.name for p in registry.all() if p.chat_id == chat_id), chat_id)
        print(f"\n[{who}]")
        print(text)
        if keyboard:
            buttons = keyboard["inline_keyboard"][0]
            print("  " + "   ".join(b["text"] for b in buttons))

    # Двоє натиснули кнопки: Саша — «Готово», Коля — «Відкласти».
    done_id = result.commitments[0].page_id
    postponed_id = result.commitments[1].page_id
    telegram.updates = [
        callback_update(1, pack_callback("d", done_id), chat_id=101),
        callback_update(2, pack_callback("p", postponed_id), chat_id=103),
    ]
    handled, _ = handle_callbacks(telegram, notion, errlog, timeout=0)

    print()
    print("─" * 62)
    print(f"НАТИСКАННЯ КНОПОК: оброблено {handled}")
    print("─" * 62)
    for page_id, status in notion.statuses:
        task = next(c.task for c in result.commitments if c.page_id.replace("-", "")
                    == page_id.replace("-", ""))
        print(f"  {task} → {status}")

    # Звіт через тиждень: дедлайни Петра й Колі вже минули.
    statuses = dict(
        (page_id.replace("-", ""), status) for page_id, status in notion.statuses
    )
    tasks = [
        {
            "page_id": c.page_id, "task": c.task, "responsible": c.responsible,
            "status": statuses.get(c.page_id.replace("-", ""), "Не начата"),
            "deadline": c.deadline, "priority": c.priority,
            "project": c.project, "source": c.source,
        }
        for c in result.commitments
    ]
    later = date(2026, 8, 31)
    print()
    print(render(build_rows(tasks, today=later), meeting="Планірка", today=later))

    print()
    print(errlog.summary())
    print(f"Демо-дані: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
