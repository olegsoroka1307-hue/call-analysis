#!/usr/bin/env python3
"""Точка входу. Розбір наради, звіт, кнопки, реєстр співробітників.

  python run.py analyze narada.txt --name "Планірка 24.08"
  python run.py report
  python run.py listen
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from meetings.config import (
    Config, ConfigError, api_key, load_dotenv, notion_key, telegram_token,
)
from meetings.errorlog import ErrorLog
from meetings.extractor import Extractor
from meetings.notion import NotionClient, NotionError
from meetings.pipeline import handle_callbacks, process_meeting
from meetings.registry import Registry
from meetings.report import build_rows, render
from meetings.telegram import TelegramClient, TelegramError


def _notion(cfg: Config) -> NotionClient:
    return NotionClient(
        notion_key(), cfg.notion_database_id, timeout=cfg.request_timeout_seconds
    )


def _telegram(cfg: Config) -> TelegramClient:
    return TelegramClient(telegram_token(), timeout=cfg.request_timeout_seconds)


def cmd_analyze(cfg: Config, args) -> int:
    if args.transcript == "-":
        transcript = sys.stdin.read()
    else:
        path = Path(args.transcript)
        if not path.exists():
            print(f"\n❌ Немає файлу {path}\n", file=sys.stderr)
            return 2
        transcript = path.read_text(encoding="utf-8")

    if not transcript.strip():
        print("\n❌ Транскрипція порожня.\n", file=sys.stderr)
        return 2

    import anthropic

    errlog = ErrorLog(cfg.error_log)
    registry = Registry(cfg.registry_db)
    notion = telegram = None
    try:
        extractor = Extractor(cfg, client=anthropic.Anthropic(api_key=api_key()))
        if cfg.notion_database_id and not args.dry_run:
            notion = _notion(cfg)
        if cfg.send_telegram and not args.dry_run:
            telegram = _telegram(cfg)
        result = process_meeting(
            cfg, transcript, extractor, registry, errlog,
            notion=notion, telegram=telegram, meeting=args.name,
            force=args.force, dry_run=args.dry_run,
        )
    finally:
        registry.close()

    print()
    for line in result.lines():
        print(line)
    if result.commitments:
        print()
        print(f"{'Хто':<14} {'Задача':<44} {'Термін':<12} Пріоритет")
        for c in result.commitments:
            print(
                f"{c.responsible[:13]:<14} {c.task[:43]:<44} "
                f"{(c.deadline or '—'):<12} {c.priority}"
            )
    print()
    print(errlog.summary())
    return 0


def cmd_report(cfg: Config, args) -> int:
    if not cfg.notion_database_id:
        print("\n❌ У config.yaml не заданий notion_database_id.\n", file=sys.stderr)
        return 2
    try:
        tasks = _notion(cfg).query_tasks(args.meeting)
    except NotionError as exc:
        print(f"\n❌ {exc}\n", file=sys.stderr)
        return 1
    print()
    print(render(build_rows(tasks), meeting=args.meeting))
    return 0


def cmd_listen(cfg: Config, args) -> int:
    if not cfg.notion_database_id:
        print("\n❌ У config.yaml не заданий notion_database_id.\n", file=sys.stderr)
        return 2
    errlog = ErrorLog(cfg.error_log)
    telegram, notion = _telegram(cfg), _notion(cfg)
    offset = None
    print("Слухаю натискання кнопок. Ctrl+C — зупинити.")
    try:
        while True:
            handled, offset = handle_callbacks(telegram, notion, errlog, offset=offset)
            if handled:
                print(f"оброблено натискань: {handled}")
            if args.once:
                return 0
    except KeyboardInterrupt:
        print("\nЗупинено.")
        return 0


def cmd_contacts(cfg: Config, args) -> int:
    try:
        contacts = _telegram(cfg).collect_contacts()
    except TelegramError as exc:
        print(f"\n❌ {exc}\n", file=sys.stderr)
        return 1
    if not contacts:
        print(
            "\nБоту ще ніхто не написав. Попросіть співробітників знайти його "
            "в Telegram і надіслати /start, потім запустіть цю команду знову.\n"
        )
        return 0
    print("\nХто написав боту:\n")
    for contact in contacts:
        username = f"@{contact['username']}" if contact["username"] else "—"
        print(f"  {contact['name']:<24} {username:<20} chat_id={contact['chat_id']}")
    print("\nДодати в реєстр:  python run.py add \"Імʼя\" --chat-id 12345\n")
    return 0


def cmd_add(cfg: Config, args) -> int:
    registry = Registry(cfg.registry_db)
    try:
        employee = registry.add(args.name, args.chat_id, args.username or "")
    finally:
        registry.close()
    print(f"✅ Додано: {employee.name} (chat_id={employee.chat_id})")
    return 0


def cmd_people(cfg: Config, args) -> int:
    registry = Registry(cfg.registry_db)
    try:
        people = registry.all()
    finally:
        registry.close()
    if not people:
        print("\nРеєстр порожній. Спершу: python run.py contacts\n")
        return 0
    print("\nУ реєстрі:\n")
    for person in people:
        username = f"@{person.telegram_username}" if person.telegram_username else "—"
        print(f"  {person.name:<24} {username:<20} chat_id={person.chat_id}")
    print()
    return 0


def cmd_setup(cfg: Config, args) -> int:
    """Створює базу «Домовленості» на вказаній сторінці Notion."""
    client = NotionClient(notion_key(), "", timeout=cfg.request_timeout_seconds)
    try:
        database_id = client.create_database(args.page_id)
    except NotionError as exc:
        print(f"\n❌ {exc}\n", file=sys.stderr)
        return 1
    print(f"\n✅ База створена. Впишіть у config.yaml:\n\n  notion_database_id: \"{database_id}\"\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Наради → задачі. Розбирає транскрипцію, створює задачі, "
                    "розсилає їх людям і показує, що з обіцяного зроблено."
    )
    parser.add_argument("--config", default="config.yaml", help="шлях до config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="розібрати нараду")
    p.add_argument("transcript", help="файл із транскрипцією, або - для stdin")
    p.add_argument("--name", default="", help="назва наради")
    p.add_argument("--force", action="store_true", help="розібрати повторно")
    p.add_argument("--dry-run", action="store_true",
                   help="лише показати домовленості, нічого не створювати")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("report", help="звіт «що обіцяли — що зробили»")
    p.add_argument("--meeting", default="", help="лише по одній нараді")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("listen", help="обробляти натискання кнопок у Telegram")
    p.add_argument("--once", action="store_true", help="один прохід і вихід")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("contacts", help="хто написав боту /start")
    p.set_defaults(func=cmd_contacts)

    p = sub.add_parser("add", help="додати співробітника в реєстр")
    p.add_argument("name")
    p.add_argument("--chat-id", type=int, required=True)
    p.add_argument("--username", default="")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("people", help="показати реєстр")
    p.set_defaults(func=cmd_people)

    p = sub.add_parser("setup", help="створити базу «Домовленості» в Notion")
    p.add_argument("page_id", help="id сторінки Notion, де створити базу")
    p.set_defaults(func=cmd_setup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    try:
        cfg = Config.load(args.config)
        return args.func(cfg, args)
    except ConfigError as exc:
        print(f"\n❌ Проблема з налаштуваннями:\n   {exc}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
