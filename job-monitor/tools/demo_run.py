#!/usr/bin/env python3
"""Демонстрація монітора на вигаданій стрічці замовлень.

Не торкається ні Freelancehunt, ні API Claude, ні Telegram.
Запуск:  python tools/demo_run.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fakes import (  # noqa: E402
    FakeAnthropic, FakeSession, draft_response, fh_item,
)
from monitor.config import Config  # noqa: E402
from monitor.drafter import Drafter  # noqa: E402
from monitor.errorlog import ErrorLog  # noqa: E402
from monitor.freelancehunt import FreelancehuntClient  # noqa: E402
from monitor.notify import ConsoleNotifier, CsvSink  # noqa: E402
from monitor.pipeline import run  # noqa: E402
from monitor.scoring import score_project  # noqa: E402
from monitor.state import State  # noqa: E402

OUT = ROOT / "data" / "demo"

OFFER = """\
Налаштовую AI-автоматизації для малого бізнесу.
1) Розбір зустрічей: транскрипція → задачі в Notion → Telegram співробітникам
   із кнопками статусів → тижневий звіт. 4 000 / 10 000 / 20 000 ₴.
2) Тріаж вхідної пошти: Gmail → важливе/неважливе/на перегляд → Google Таблиця.
   6 000 / 12 000 ₴.
Не роблю: дизайн, тексти, SEO, верстку.
"""

FEED = [
    fh_item(
        301, "Бот для контролю доручень після планірок",
        "Щотижня проводимо планірки на 8 людей. Потрібно, щоб задачі з "
        "транскрипції зустрічі автоматично потрапляли в Notion, а кожен "
        "співробітник отримував свої в Telegram і відмічав виконання.",
        skills=["Python", "Telegram", "Notion"], amount=18000, bid_count=2,
    ),
    fh_item(
        302, "Автоматизація обробки вхідних листів",
        "Приходить 200+ листів на день. Треба сортувати важливі, витягувати "
        "з PDF суми рахунків і зводити в Google Sheets. Gmail.",
        skills=["Python", "Google API"], amount=25000, bid_count=5,
    ),
    fh_item(
        303, "Намалювати логотип і банери для соцмереж",
        "Потрібен логотип, банер, верстк макетів. Дизайн у сучасному стилі.",
        skills=["Photoshop"], amount=6000, bid_count=12,
    ),
    fh_item(
        304, "Інтеграція CRM з сайтом через API",
        "Потрібна автоматизація передачі заявок із форми в CRM. "
        "Можливо через n8n або zapier.",
        skills=["API"], amount=9000, bid_count=6,
    ),
    fh_item(
        305, "Telegram-бот для нагадувань про задачі",
        "Автоматизація нагадувань. Notion. Дуже терміново.",
        skills=["Telegram"], amount=1500, bid_count=3,
    ),
    fh_item(
        306, "Розбір дзвінків notion telegram автоматизація",
        "Транскрипція зустрічей у задачі.",
        skills=["Python"], amount=30000, bid_count=87,
    ),
    fh_item(
        307, "Потрібен розробник Laravel для доопрацювання порталу",
        "Backend на Laravel, є API. Автоматизація звітів.",
        skills=["PHP", "Laravel"], amount=40000, bid_count=4,
    ),
]

DRAFTS = [
    draft_response(
        opening="Вісім людей на планірці — це приблизно шість домовленостей "
                "на тиждень, і зривається зазвичай та, про яку ніхто не "
                "згадав до наступної зустрічі.",
        package="Команда", price="10 000 ₴, 4 дні",
        question="Задачі мають розлітатись команді автоматично, чи ви хочете "
                 "спершу переглядати список?",
    ),
    draft_response(
        opening="При 200 листах на день проблема не в сортуванні, а в тому, "
                "що рахунок у PDF помічають уже після дедлайну оплати.",
        package="Тріаж пошти, під ключ", price="12 000 ₴, 6 днів",
        question="Рахунки треба лише помічати, чи одразу зводити суми й "
                 "терміни в окрему таблицю?",
        note="Замовник не вказав, чи це Google Workspace — від цього залежить "
             "спосіб видачі доступу.",
    ),
    draft_response(
        opening="Заявки з форми, які треба переносити руками, губляться саме "
                "в години пікових звернень.",
        package="Автоматизація процесів", price="орієнтовно 8 000 ₴, 3 дні",
        question="Скільки заявок на день і яка саме CRM?",
        fits="partly", note="Це не наша готова система, а разова інтеграція.",
    ),
    draft_response(
        opening="—", package="—", price="—", question="—",
        fits="no", note="Просять Laravel-розробника. Автоматизація звітів там "
                        "збоку, основна робота — PHP-бекенд, якого ми не робимо.",
    ),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in ("leads.csv", "state.db", "errors.jsonl"):
        (OUT / stale).unlink(missing_ok=True)

    cfg = Config(
        my_offer=OFFER,
        csv_path=str(OUT / "leads.csv"),
        state_db=str(OUT / "state.db"),
        error_log=str(OUT / "errors.jsonl"),
        # У демо знижений, щоб було видно, як модель ловить чужу задачу.
        # У бою поріг вищий за min_score — щоб не платити за слабкі збіги.
        draft_min_score=6,
    )
    cfg.validate()

    print("\n" + "═" * 70)
    print("ДЕМОНСТРАЦІЯ МОНІТОРА — 7 замовлень у стрічці, жодного реального доступу")
    print("═" * 70)
    print("\nЩо в стрічці і що з ним зробить відсів:\n")
    client_preview = FreelancehuntClient("demo", session=FakeSession([FEED, []]))
    for project in client_preview.projects(pages=2):
        s = score_project(project, cfg)
        mark = "✅ ПОКАЗАТИ" if s.passed else f"✖  {s.blocked_by}"
        print(f"  [{s.total:>3}] {project.title[:52]:<52} {mark}")

    errlog = ErrorLog(cfg.error_log)
    state = State(cfg.state_db)
    client = FreelancehuntClient("demo", session=FakeSession([FEED, []]))
    drafter = Drafter(cfg, client=FakeAnthropic(list(DRAFTS)))
    notifiers = [ConsoleNotifier(), CsvSink(cfg.csv_path)]

    report, leads = run(cfg, client, state, errlog, notifiers, drafter)

    print(report.render())
    print(errlog.summary())
    print(f"\nТаблиця лідів: {cfg.csv_path}")
    print("Монітор нічого не подає й нічого не пише замовникам — тільки вам.\n")

    print("Повторний запуск на тій самій стрічці:")
    report2, _ = run(cfg, FreelancehuntClient("demo", session=FakeSession([FEED, []])),
                     state, errlog, [CsvSink(cfg.csv_path)], drafter)
    print(f"  показано вдруге: {report2.leads} (усі {report2.skipped_seen} вже бачили)\n")
    state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
